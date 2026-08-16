"""端到端测试（本机模拟）：独立 Broker + 服务端 + mock 执行器 + 发送方四进程闭环。

隔离设计: 测试用独立 mosquitto 实例（临时端口），不碰 1883 真实总线——
否则测试进程的 register/retain 会被真实服务端桥写进真实面板（历史踩坑）。
前置: 本机安装 Mosquitto（PATH 或默认安装目录）
运行: python tests/test_e2e.py
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = 8899
BASE = f"http://127.0.0.1:{PORT}"

passed, failed = [], []


def check(name: str, cond: bool, detail: str = ""):
    (passed if cond else failed).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def find_mosquitto() -> str:
    for cand in (shutil.which("mosquitto"),
                 r"C:\Program Files\Mosquitto\mosquitto.exe"):
        if cand and Path(cand).is_file():
            return cand
    return ""


def wait_http(url, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def api(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    mosq = find_mosquitto()
    if not mosq:
        print("SKIP: 找不到 mosquitto（测试需独立 broker 隔离，不碰 1883 真实总线）")
        sys.exit(2)

    BPORT = free_port()
    tmp = Path(tempfile.mkdtemp(prefix="agent_bus_e2e_"))
    procs = []
    try:
        # 0. 独立 Broker（仅回环 + 匿名），与真实总线完全隔离
        conf = tmp / "mosq.conf"
        conf.write_text(f"listener {BPORT} 127.0.0.1\nallow_anonymous true\n",
                        encoding="utf-8")
        procs.append(subprocess.Popen(
            [mosq, "-c", str(conf)],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        ))
        broker_ok = False
        for _ in range(30):
            try:
                s = socket.create_connection(("127.0.0.1", BPORT), timeout=1)
                s.close()
                broker_ok = True
                break
            except OSError:
                time.sleep(0.3)
        check("独立 Broker 启动", broker_ok)

        # 1. 启动服务端（指向测试 broker；面板已全匿名，无需令牌参数）
        procs.append(subprocess.Popen(
            [sys.executable, str(ROOT / "server" / "bus_server.py"),
             "--port", str(PORT), "--db", str(tmp / "bus.db"),
             "--files-dir", str(tmp / "files"), "--broker-port", str(BPORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        ))
        check("服务端启动 (HTTP ready)", wait_http(f"{BASE}/api/health"))

        # 2. 启动 mock 执行器节点
        env = {**os.environ, "BUS_HTTP_BASE": BASE, "BUS_BROKER_PORT": str(BPORT)}
        procs.append(subprocess.Popen(
            [sys.executable, str(ROOT / "executor" / "codebuddy_executor.py"),
             "--agent-id", "mock_worker", "--name", "MockWorker", "--mock",
             "--http-base", BASE, "--workdir", str(tmp / "work")],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, env=env,
        ))
        time.sleep(2)

        from agent_bus import AgentBus, BusConfig

        # 3. 基本任务闭环
        sender = AgentBus("sender_alpha", name="SenderAlpha",
                          config=BusConfig.load(http_base=BASE,
                                                broker_port=BPORT)).connect()
        res = sender.send_task("mock_worker", "你好，请自我介绍", wait=True, wait_timeout=30)
        check("任务发送并收到回传", res is not None)
        check("结果 status=success", bool(res and res["status"] == "success"),
              json.dumps(res, ensure_ascii=False)[:200] if res else "no result")
        check("结果包含任务回显", bool(res and "自我介绍" in res["result"]["output_text"]))
        check("回传带 session_id（可延续会话）", bool(res and res["result"].get("session_id")))

        # 4. 文件链路（Claim-Check）
        src = tmp / "data.csv"
        src.write_text("name,value\nfoo,1\n", encoding="utf-8")
        up = sender.upload(str(src))
        check("文件上传拿到 URL", bool(up.get("url")))
        res2 = sender.send_task("mock_worker", "处理附件数据",
                                attachments=[up["url"]], wait=True, wait_timeout=30)
        check("附件任务执行成功", bool(res2 and res2["status"] == "success"))
        check("执行器确实下载到 1 个附件",
              bool(res2 and "附件 1 个" in res2["result"]["output_text"]),
              json.dumps(res2, ensure_ascii=False)[:300] if res2 else "no result")

        # 5. 在线名单与消息追溯
        agents = sender.list_agents()
        check("在线名单含双方", {a["agent_id"] for a in agents} >= {"sender_alpha", "mock_worker"})
        msgs = api("/api/messages?limit=50")
        check("消息时间线已记录全部来往", len(msgs) >= 4, f"records={len(msgs)}")

        sender.disconnect()
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果: {len(passed)} 通过, {len(failed)} 失败")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
