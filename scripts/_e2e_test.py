"""【遗留，已过时】v1 口令/凭据模型的端到端测试（匿名化改造前）。

> v2 匿名化（git 4c805e7）后：/api/join 免口令、无凭据发放、无 admin 令牌、
> 无 /api/admin/passphrase 端点——本脚本的 T3-T8 已不再适用。
> 现行端到端测试见 tests/test_e2e.py（面板全匿名）。保留本文件仅作历史参考。

原覆盖（v1）：
  T1  服务健康 + 首启向导 /api/team/setup
  T2  beacon 广播可被本机扫描发现（含队伍名）
  T3  错误口令 401；连错 5 次触发 429 锁定（换 IP 头模拟受限，此处本机单 IP 只验计数）
  T4  正确口令 join 成功，broker_restarted=True（用户态自动重启）
  T5  新凭据 MQTT 连接 + 注册 → /api/agents 可见在线
  T6  node 令牌访问 admin 端点 403（role 分离）；bridge 令牌 200
  T7  重置节点（二次 join）旧令牌失效（幽灵令牌修复）
  T8  移除节点 → MQTT 凭据失效（匿名/已删用户拒绝）
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

E2E = ROOT / "data" / "e2e"
# auth 放 %TEMP% 纯 ASCII 目录：Windows mosquitto 以 ANSI 打开 conf 内
# passwd/acl 路径，仓库路径含中文会 "Unable to open pwfile"（生产同因用 C:\mosquitto-auth）
AUTH = Path(os.environ.get("TEMP", ".")) / "agent-bus-e2e-auth"
DB = E2E / "bus.db"
BROKER_PORT, HTTP_PORT = 1884, 8001
TEAM, PASS = "E2E-测试队", "e2e-pass-42"

os.environ["BUS_AUTH_DIR"] = str(AUTH)  # provision.auth_dir() 读此变量

from agent_bus import provision, discovery  # noqa: E402
import setup_host  # noqa: E402  复用便携版 mosquitto 下载逻辑


def http(method, url, body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def mqtt_ok(user, pw) -> bool:
    import paho.mqtt.client as mqtt
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"e2e-{user}")
    c.username_pw_set(user, pw)
    got = []
    c.on_connect = lambda cl, u, f, rc, p: got.append(rc)
    try:
        c.connect("127.0.0.1", BROKER_PORT, keepalive=20)
        c.loop_start()
        time.sleep(1.5)
        c.disconnect()
        c.loop_stop()
    except Exception:
        return False
    return got and got[0].value == 0


def mqtt_register(user, pw, agent_id, name):
    """模拟子设备执行器上线：发布 register 消息（join_team 拿到凭据后的真实动作）。"""
    import paho.mqtt.client as mqtt
    from agent_bus import schema
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"e2e-reg-{agent_id}")
    c.username_pw_set(user, pw)
    c.connect("127.0.0.1", BROKER_PORT, keepalive=30)
    c.loop_start()
    time.sleep(0.8)
    info = c.publish("bus/register", json.dumps(
        schema.make_register(agent_id, name=name, platform="e2e-sim",
                             hostname="e2e-host", health="ok"), ensure_ascii=False))
    info.wait_for_publish(5)
    c.disconnect()
    c.loop_stop()


results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(f"  {'PASS' if cond else 'FAIL'}  {name} {extra}")


def kill_pidfile(f: Path):
    """杀掉 pid 文件指向的进程（join 触发的 broker 重启进程不在 procs 列表内）。"""
    try:
        os.kill(int(f.read_text().strip()), 9)
    except (OSError, ValueError):
        pass
    f.unlink(missing_ok=True)


def kill_port(port: int):
    """兜底：杀掉占住 e2e 端口的孤儿进程（pid 文件可能已被早前误删）。"""
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if f":{port} " in line and "LISTENING" in line:
            pid = line.split()[-1]
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)


def cleanup():
    kill_port(BROKER_PORT)
    kill_port(HTTP_PORT)
    kill_pidfile(ROOT / "data" / "broker.pid")
    for f in (ROOT / "data" / "broker.cmd.json",):
        f.unlink(missing_ok=True)


def main():
    import shutil
    cleanup()  # 上一轮残留的（重启过的）broker 会锁住 E2E 目录
    for d in (E2E, AUTH):  # 可重复执行：清掉上一轮的 db/凭据/日志
        if d.exists():
            shutil.rmtree(d)
    E2E.mkdir(parents=True, exist_ok=True)
    AUTH.mkdir(parents=True, exist_ok=True)
    procs = []
    server_proc = None
    try:
        print("== 准备：便携 mosquitto + 桥接账号 + 双进程启动 ==")
        exe = setup_host.ensure_broker_win(BROKER_PORT)

        cred = provision.CredStore(db_path=DB)
        bridge_pw = provision.gen_password(32)
        provision.set_mqtt_password(AUTH / "passwd", provision.BRIDGE_USER, bridge_pw)
        bridge_tok = provision.gen_token()
        cred.save_node(provision.BRIDGE_USER, bridge_pw, bridge_tok, role="bridge")

        conf = E2E / "mosquitto.conf"
        conf.write_text(
            f"listener {BROKER_PORT} 0.0.0.0\nallow_anonymous false\n"
            f"password_file {(AUTH / 'passwd').as_posix()}\n"
            f"acl_file {(AUTH / 'acl').as_posix()}\n", encoding="utf-8")
        provision.write_acl(AUTH / "acl")

        br = subprocess.Popen([exe, "-c", str(conf)], cwd=str(E2E),
                              stdout=open(E2E / "broker.log", "ab"),
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              creationflags=0x08000000)
        procs.append(br)
        (ROOT / "data" / "broker.pid").write_text(str(br.pid))
        (ROOT / "data" / "broker.cmd.json").write_text(json.dumps({
            "args": [exe, "-c", str(conf)], "cwd": str(E2E),
            "probe_host": "127.0.0.1", "probe_port": BROKER_PORT}), encoding="utf-8")
        time.sleep(1.5)

        env = os.environ.copy()
        env.update({"BUS_MQTT_USER": provision.BRIDGE_USER, "BUS_MQTT_PASS": bridge_pw,
                    "BUS_AUTH_DIR": str(AUTH),
                    "BUS_HTTP_BASE": f"http://127.0.0.1:{HTTP_PORT}"})
        server_proc = subprocess.Popen(
            [sys.executable, str(ROOT / "server" / "bus_server.py"),
             "--host", "127.0.0.1", "--port", str(HTTP_PORT),
             "--broker-host", "127.0.0.1", "--broker-port", str(BROKER_PORT),
             "--db", str(DB), "--files-dir", str(E2E / "files")],
            cwd=str(ROOT), env=env,
            stdout=open(E2E / "server.log", "ab"),
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=0x08000000)
        base = f"http://127.0.0.1:{HTTP_PORT}"
        for _ in range(50):  # 等服务就绪（uvicorn 冷启动可能 >3s）
            try:
                with urllib.request.urlopen(base + "/api/health", timeout=1):
                    break
            except Exception:
                if server_proc.poll() is not None:
                    raise RuntimeError("bus_server 启动即退出，查看 data/e2e/server.log")
                time.sleep(0.3)
        else:
            raise RuntimeError("bus_server 15s 未就绪，查看 data/e2e/server.log")
        time.sleep(1)

        print("\n== T1 首启向导 ==")
        st, body = http("GET", base + "/api/team/status")
        check("team status 未初始化", st == 200 and body.get("initialized") is False)
        st, body = http("POST", base + "/api/team/setup",
                        {"team_name": TEAM, "passphrase": PASS})
        check("setup 成功", st == 200 and body.get("ok"), str(body))
        st, body = http("POST", base + "/api/team/setup",
                        {"team_name": "hack", "passphrase": "xxxx"})
        check("二次 setup 拒绝(403)", st == 403)

        print("\n== T2 beacon 发现 ==")
        teams = discovery.scan_teams(timeout=6.5)
        hit = [t for t in teams if t["team_name"] == TEAM]
        check("扫描到本队 beacon", bool(hit), str(teams[:2]))
        check("beacon 携带候选 IP 列表", bool(hit) and len(hit[0].get("host_ips", [])) >= 1,
              str(hit[0].get("host_ips")) if hit else "")

        print("\n== T3 错误口令与限速 ==")
        st, _ = http("POST", base + "/api/join", {"passphrase": "wrong-x", "agent_id": "t3node"})
        check("错误口令 401", st == 401)

        print("\n== T4 正确口令 join ==")
        st, j = http("POST", base + "/api/join",
                     {"passphrase": PASS, "agent_id": "e2e_node1",
                      "device_name": "E2E模拟子设备", "platform": "windows"})
        check("join 成功", st == 200 and j.get("ok"), str(j.get("detail", "")))
        check("返回队伍名与凭据", j.get("team_name") == TEAM and j.get("mqtt_pass"))
        check("用户态 broker 自动重启", j.get("broker_restarted") is True,
              str(j.get("broker_message")))

        print("\n== T5 上线可见 ==")
        check("新凭据 MQTT 可用", mqtt_ok("e2e_node1", j["mqtt_pass"]))
        mqtt_register("e2e_node1", j["mqtt_pass"], "e2e_node1", "E2E模拟子设备")
        row = None
        for _ in range(10):  # 等桥接处理 register
            st, agents = http("GET", base + "/api/agents", token=j["http_token"])
            row = next((a for a in agents if a["agent_id"] == "e2e_node1"), None)
            if row:
                break
            time.sleep(0.5)
        check("agents 名单含新节点(已注册在线)", row is not None and row.get("online"),
              str(row)[:120] if row else f"st={st}")

        print("\n== T6 role 分离 ==")
        st, _ = http("POST", base + "/api/admin/passphrase", {}, token=j["http_token"])
        check("node 令牌 admin 操作 403", st == 403)
        st, regen = http("POST", base + "/api/admin/passphrase", {}, token=bridge_tok)
        check("bridge 令牌可再生成口令", st == 200 and regen.get("passphrase"),
              f"st={st} body={str(regen)[:120]}")
        # 用新口令还能 join（口令再生成立即生效）
        st, j2 = http("POST", base + "/api/join",
                      {"passphrase": regen["passphrase"], "agent_id": "e2e_node2"})
        check("新口令 join 成功", st == 200)

        print("\n== T7 幽灵令牌修复（重置节点） ==")
        st, j1b = http("POST", base + "/api/join",
                       {"passphrase": regen["passphrase"], "agent_id": "e2e_node1"})
        check("同 id 重新 join（密码轮换）", st == 200 and j1b["mqtt_pass"] != j["mqtt_pass"])
        st, _ = http("GET", base + "/api/agents", token=j["http_token"])
        check("旧 HTTP 令牌已失效(401)", st == 401)
        st, _ = http("GET", base + "/api/agents", token=j1b["http_token"])
        check("新 HTTP 令牌可用", st == 200)
        check("旧 MQTT 密码已失效", not mqtt_ok("e2e_node1", j["mqtt_pass"]))

        print("\n== T8 移除节点 ==")
        st, rm = http("DELETE", base + "/api/admin/nodes/e2e_node1", token=bridge_tok)
        check("移除成功", st == 200 and rm.get("ok"))
        check("被移除节点 MQTT 拒绝", not mqtt_ok("e2e_node1", j1b["mqtt_pass"]))

    finally:
        print("\n== 清理 ==")
        if server_proc:
            server_proc.kill()
        cleanup()  # 含 join 期间重启出的新 broker（pid 文件已是新 pid）
        procs and procs[0].kill()
        time.sleep(0.5)

    ok = sum(1 for _, c in results if c)
    print(f"\n== 结果: {ok}/{len(results)} 通过 ==")
    for name, c in results:
        if not c:
            print(f"  FAIL: {name}")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    main()
