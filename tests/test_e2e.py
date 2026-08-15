"""端到端测试（本机模拟）：服务端 + mock 执行器 + 发送方三进程闭环。

前置: MQTT Broker 运行在 127.0.0.1:1883（回环匿名即可）
运行: python tests/test_e2e.py
"""
import json
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


def wait_http(url: str, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def api(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    # 前置：Broker 可达
    try:
        s = socket.create_connection(("127.0.0.1", 1883), timeout=3)
        s.close()
    except OSError:
        print("SKIP: MQTT Broker (127.0.0.1:1883) 未运行，请先启动 Mosquitto")
        sys.exit(2)

    tmp = Path(tempfile.mkdtemp(prefix="agent_bus_e2e_"))
    procs = []
    try:
        # 1. 启动服务端
        procs.append(subprocess.Popen(
            [sys.executable, str(ROOT / "server" / "bus_server.py"),
             "--port", str(PORT), "--db", str(tmp / "bus.db"),
             "--files-dir", str(tmp / "files")],
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        ))
        check("服务端启动 (HTTP ready)", wait_http(f"{BASE}/api/health"))

        # 2. 启动 mock 执行器节点
        env = {**__import__("os").environ, "BUS_HTTP_BASE": BASE}
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
                          config=BusConfig.load(http_base=BASE)).connect()
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
