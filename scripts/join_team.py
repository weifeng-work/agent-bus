"""子设备一键加入队伍：发现主机 → 输口令 → 拿凭据 → 验证上线。

一句提示词流程（子设备）:
  python scripts/join_team.py                    # 自动发现 + 交互选择
  python scripts/join_team.py --host 192.168.31.186   # 广播不可达时的手动回退

做了什么:
  1. 扫描局域网 beacon（5 秒；多队伍列出供选择；AP 隔离时可 --host 指定）
  2. 输入队伍口令 → POST /api/join 核对 → 服务端自动发凭据（并自动重启用户态 broker）
  3. 凭据写入 ~/.config/agent-bus/bus.env（Windows 同时 setx 持久化用户环境变量）
  4. 设备身份存 ~/.config/agent-bus/device.json（agent_id 稳定，重跑即重新加入/重置）
  5. 立即连 MQTT 注册验证 → 主机面板可见本机在线

之后启动任意执行器（自动读 bus.env）:  python executor/codebuddy_executor.py --agent-id <id> ...
"""
import argparse
import getpass
import json
import os
import platform as _platform
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_bus import discovery  # noqa: E402

CONFIG_DIR = Path.home() / ".config" / "agent-bus"
ENV_FILE = CONFIG_DIR / "bus.env"
DEVICE_FILE = CONFIG_DIR / "device.json"


def load_or_create_device() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if DEVICE_FILE.exists():
        try:
            return json.loads(DEVICE_FILE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    host = re.sub(r"[^A-Za-z0-9-]", "", _platform.node().lower())[:20] or "node"
    dev = {"agent_id": f"{host}-{secrets.token_hex(2)}"}
    DEVICE_FILE.write_text(json.dumps(dev, ensure_ascii=False, indent=2), encoding="utf-8")
    return dev


def save_env(creds: dict):
    lines = [
        f"BUS_BROKER_HOST={creds['broker_host']}",
        f"BUS_BROKER_PORT={creds.get('broker_port', 1883)}",
        f"BUS_HTTP_BASE=http://{creds['broker_host']}:{creds.get('http_port', 8000)}",
        f"BUS_MQTT_USER={creds['mqtt_user']}",
        f"BUS_MQTT_PASS={creds['mqtt_pass']}",
        f"BUS_HTTP_TOKEN={creds['http_token']}",
    ]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name == "nt":
        for line in lines:
            k, v = line.split("=", 1)
            subprocess.run(["setx", k, v], capture_output=True)  # 持久化用户环境变量


def pick_team(teams: list) -> dict:
    if len(teams) == 1:
        t = teams[0]
        print(f"发现队伍: {t['team_name']}（主机 {t['host_name']} @ {t['host_ip']}）")
        return t
    print("发现多个队伍:")
    for i, t in enumerate(teams, 1):
        print(f"  [{i}] {t['team_name']}（主机 {t['host_name']} @ {t['host_ip']}）")
    while True:
        raw = input("选择要加入的队伍编号: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(teams):
            return teams[int(raw) - 1]


def pick_alive_ip(team: dict) -> str:
    """连通性自检（发现 ≠ 连通）：逐候选探测 /api/health，选第一个可达 IP。

    主机多网卡/代理 TUN 时 beacon 自报多个候选 IP，广播来源地址也可能只是
    本地可达的虚拟段——join 前必须逐一验证。
    """
    cands = []
    for ip in [team.get("host_ip")] + (team.get("host_ips") or []):
        if ip and ip not in cands:
            cands.append(ip)
    for ip in cands:
        try:
            with urllib.request.urlopen(
                    f"http://{ip}:{team['http_port']}/api/health", timeout=2) as r:
                if r.status == 200:
                    if ip != team.get("host_ip"):
                        print(f"  连通性自检: 选用 {ip}（首选 {team.get('host_ip')} 不可达）")
                    return ip
        except Exception:
            continue
    sys.exit(f"主机不可达（已尝试 {cands}）。检查主机防火墙放行 {team['http_port']}，"
             f"或用 --host <主机IP> 手动指定")


def join_http(base: str, body: dict) -> dict:
    req = urllib.request.Request(
        base.rstrip("/") + "/api/join",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            pass
        sys.exit(f"加入失败 HTTP {e.code}: {detail}")


def verify_online(creds: dict, agent_id: str):
    """立即连 MQTT 注册（面板可见）——服务模式 broker 重启前凭据未生效则重试。"""
    from agent_bus import AgentBus, BusConfig
    deadline = time.time() + 60
    last_err = None
    while time.time() < deadline:
        try:
            cfg = BusConfig(
                broker_host=creds["broker_host"], broker_port=creds.get("broker_port", 1883),
                agent_id=agent_id, mqtt_user=creds["mqtt_user"],
                mqtt_pass=creds["mqtt_pass"], http_token=creds["http_token"],
            )
            bus = AgentBus(agent_id, name=creds.get("device_name") or agent_id, config=cfg)
            bus.connect()
            bus.disconnect()
            return True
        except ConnectionError as e:
            last_err = e
            time.sleep(3)
    print(f"警告: 凭据尚未生效（{last_err}）——若主机为服务模式需管理员重启 broker")
    return False


def main():
    ap = argparse.ArgumentParser(description="agent-bus 子设备一键加入")
    ap.add_argument("--host", help="手动指定主机 IP（广播不可达时）")
    ap.add_argument("--http-port", type=int, default=8000)
    ap.add_argument("--mqtt-port", type=int, default=1883)
    ap.add_argument("--passphrase", help="队伍口令（非交互模式传入；省略则运行时提示输入）")
    ap.add_argument("--name", help="设备显示名（默认 <系统>@<主机名>）")
    args = ap.parse_args()

    dev = load_or_create_device()
    agent_id = dev["agent_id"]
    device_name = args.name or f"{_platform.system()}@{_platform.node()}"

    print("== agent-bus 加入队伍 ==")
    if args.host:
        team = {"team_name": "?", "host_name": args.host, "host_ip": args.host,
                "host_ips": [args.host],
                "mqtt_port": args.mqtt_port, "http_port": args.http_port}
    else:
        print("  扫描局域网队伍（5 秒）...")
        teams = discovery.scan_teams(timeout=5.0)
        if not teams:
            sys.exit("未发现队伍。确认主机已运行 setup_host.py 并完成首次向导；"
                     "或用 --host <主机IP> 手动指定")
        team = pick_team(teams)

    team["host_ip"] = pick_alive_ip(team)  # 连通性自检（发现≠连通）

    print(f"  设备身份: {agent_id}（{device_name}）")
    passphrase = args.passphrase
    if not passphrase:
        passphrase = getpass.getpass(f"  输入队伍 [{team['team_name']}] 的加入口令: ")
    elif not (4 <= len(passphrase) <= 64):
        sys.exit("口令长度需 4-64")

    base = f"http://{team['host_ip']}:{team['http_port']}"
    creds = join_http(base, {
        "passphrase": passphrase, "agent_id": agent_id,
        "device_name": device_name, "platform": _platform.system().lower(),
    })

    http_port = team["http_port"] if not args.host else args.http_port
    creds.update({"broker_host": team["host_ip"], "broker_port": team["mqtt_port"],
                  "http_port": http_port, "device_name": device_name})
    save_env(creds)

    print(f"  已加入队伍 [{creds['team_name']}]，凭据写入 {ENV_FILE}")
    if not creds.get("broker_restarted", False):
        print(f"  注意: {creds.get('broker_message', 'broker 需重启生效')}")

    print("  验证上线...")
    if verify_online(creds, agent_id):
        print("\n== 完成: 本机已上线 ==")
        print(f"  agent_id: {agent_id}")
        print(f"  启动执行器示例: python executor/codebuddy_executor.py --agent-id {agent_id} --name '{device_name}'")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
