"""节点凭据开通工具（在 broker 机器上运行）。

为每个节点发放一组凭据：
  - MQTT 用户名/密码（写入 mosquitto passwd 文件；用户名 = agent_id）
  - HTTP API 令牌（写入 bus.db 的 http_tokens 表）

用法:
  # 首次初始化（生成桥接账号 + 管理员令牌 + ACL/passwd 骨架）
  python scripts/add_node.py --init

  # 开通/重置一个节点（密码重新生成，令牌重新生成）
  python scripts/add_node.py --agent-id codebuddy_pc1

  # 列出已发放凭据的节点
  python scripts/add_node.py --list

输出（ stdout 仅此一次展示密码/令牌；data/credentials.json 在 broker 侧留档）:
  节点侧需要设置的三个环境变量 + 各平台示例命令。

安全说明:
  - agent_id 作为 MQTT 用户名，字符集限 [A-Za-z0-9_-]（防 ACL 通配符注入）
  - mosquitto 侧密码只存哈希（mosquitto_passwd PBKDF2-SHA512）
  - credentials.json 含明文密码，仅限 broker 机器、目录已被 .gitignore 排除
"""
import argparse
import json
import re
import secrets
import shutil
import sqlite3
import string
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT_DIR / "data" / "bus.db"
DEFAULT_CRED = ROOT_DIR / "data" / "credentials.json"
BRIDGE_USER = "bus-server-bridge"

# ASCII 路径，避免 mosquitto 服务读含中文/空格路径的问题
AUTH_DIR_CANDIDATES = [Path("C:/mosquitto-auth"), Path("/etc/agent-bus-auth")]

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def find_auth_dir() -> Path:
    for d in AUTH_DIR_CANDIDATES:
        if d.is_dir():
            return d
    # 首次：创建当前平台可写的目录
    d = AUTH_DIR_CANDIDATES[0] if sys.platform == "win32" else AUTH_DIR_CANDIDATES[1]
    d.mkdir(parents=True, exist_ok=True)
    return d


def find_mosquitto_passwd() -> str:
    name = shutil.which("mosquitto_passwd")
    if name:
        return name
    for p in (r"C:\Program Files\mosquitto\mosquitto_passwd.exe",
              "/usr/bin/mosquitto_passwd", "/usr/sbin/mosquitto_passwd"):
        if Path(p).exists():
            return p
    sys.exit("找不到 mosquitto_passwd 工具，请先安装 mosquitto")


def gen_password(n=24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def set_mqtt_password(passwd_file: Path, user: str, password: str):
    passwd_file.parent.mkdir(parents=True, exist_ok=True)
    if not passwd_file.exists():
        passwd_file.touch()
    r = subprocess.run([find_mosquitto_passwd(), "-b", str(passwd_file), user, password],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"mosquitto_passwd 失败: {r.stderr.strip()}")


def write_acl(acl_file: Path):
    """基于 pattern 的 ACL：按用户名（%u=agent_id）授权，新增节点零维护。

    说明: pattern 规则对所有通过认证的连接生效；bus-server-bridge 为全量读写
    （消息追溯需要）。节点可写任意 agent/+/inbox 是 P2P 通信的前提；
    队伍隔离（bus/{team}/ 前缀 + 每队主题级 ACL）在后续版本叠加。
    """
    acl = f"""# agent-bus ACL (pattern %u = MQTT username = agent_id)
user {BRIDGE_USER}
topic readwrite #

pattern write bus/register
pattern write bus/heartbeat/%u
pattern write bus/offline/%u
pattern read agent/%u/inbox
pattern write agent/+/inbox
"""
    acl_file.write_text(acl, encoding="utf-8")


class CredStore:
    """broker 侧留档（data/credentials.json，已 gitignore）+ DB 令牌表。"""

    def __init__(self, cred_file: Path, db_path: Path):
        self.cred_file = cred_file
        self.db_path = db_path
        if cred_file.exists():
            self.data = json.loads(cred_file.read_text(encoding="utf-8"))
        else:
            self.data = {"nodes": {}}
        cred_file.parent.mkdir(parents=True, exist_ok=True)

    def save_node(self, agent_id, mqtt_pass, http_token, role="node"):
        self.data["nodes"][agent_id] = {
            "mqtt_user": agent_id, "mqtt_pass": mqtt_pass,
            "http_token": http_token, "role": role, "issued_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.cred_file.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        # HTTP 令牌写入服务端 DB（全新库时自动建表）
        conn = sqlite3.connect(str(self.db_path))
        with conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS http_tokens(
                       token TEXT PRIMARY KEY, agent_id TEXT,
                       role TEXT DEFAULT 'node', created_at REAL)"""
            )
            conn.execute(
                "INSERT OR REPLACE INTO http_tokens(token,agent_id,role,created_at) VALUES(?,?,?,?)",
                (http_token, agent_id, role, time.time()),
            )
        conn.close()


def print_node_env(agent_id, mqtt_pass, http_token):
    print(f"\n=== 节点 {agent_id} 凭据（仅此一次完整展示） ===")
    print(f'export BUS_MQTT_USER="{agent_id}"')
    print(f'export BUS_MQTT_PASS="{mqtt_pass}"')
    print(f'export BUS_HTTP_TOKEN="{http_token}"')
    print("\n# PowerShell (Windows) 示例:")
    print(f'$env:BUS_MQTT_USER="{agent_id}"; $env:BUS_MQTT_PASS="{mqtt_pass}"; $env:BUS_HTTP_TOKEN="{http_token}"')
    print("\n# bash (Linux/macOS) 后续长期使用可写入 /etc/environment 或 systemd Environment=")


def main():
    ap = argparse.ArgumentParser(description="agent-bus 节点凭据开通")
    ap.add_argument("--init", action="store_true", help="初始化：桥接账号+管理员令牌+ACL")
    ap.add_argument("--agent-id", help="要开通/重置的节点 ID")
    ap.add_argument("--list", action="store_true", help="列出已发放凭据的节点")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--cred-file", default=str(DEFAULT_CRED))
    args = ap.parse_args()

    auth_dir = find_auth_dir()
    passwd_file = auth_dir / "passwd"
    acl_file = auth_dir / "acl"
    cred = CredStore(Path(args.cred_file), Path(args.db))

    if args.list:
        for aid, info in cred.data["nodes"].items():
            print(f"{aid:24s} role={info['role']:8s} issued={info['issued_at']}")
        return

    if args.init:
        write_acl(acl_file)
        print(f"ACL 已写入: {acl_file}")
        bridge_pw = gen_password(32)
        set_mqtt_password(passwd_file, BRIDGE_USER, bridge_pw)
        admin_token = secrets.token_urlsafe(24)
        cred.save_node(BRIDGE_USER, bridge_pw, admin_token, role="bridge")
        print(f"\n=== 桥接账号（bus_server 启动环境变量） ===")
        print(f'export BUS_MQTT_USER="{BRIDGE_USER}"')
        print(f'export BUS_MQTT_PASS="{bridge_pw}"')
        print(f"\n=== 管理员令牌（面板登录用） ===")
        print(f"panel token: {admin_token}")
        print("\n下一步: 以管理员权限运行 scripts/enable_mqtt_auth_admin.ps1 启用 broker 认证，"
              "然后用 --agent-id 逐个开通节点。")
        return

    if not args.agent_id:
        ap.error("需要 --agent-id 或 --init 或 --list")
    if not ID_RE.match(args.agent_id):
        sys.exit(f"agent_id 非法（限 [A-Za-z0-9_-]，1-64 位）: {args.agent_id}")

    mqtt_pass = gen_password()
    http_token = secrets.token_urlsafe(24)
    set_mqtt_password(passwd_file, args.agent_id, mqtt_pass)
    if not acl_file.exists():
        write_acl(acl_file)
    cred.save_node(args.agent_id, mqtt_pass, http_token)
    print(f"已写入 mosquitto passwd: {passwd_file}")
    print_node_env(args.agent_id, mqtt_pass, http_token)
    print("\n注意: mosquitto 不热加载 passwd 文件——新增/重置节点后需重启 broker 生效:")
    print("  Windows: Restart-Service mosquitto   (管理员 PowerShell)")
    print("  Linux:   sudo systemctl restart mosquitto")


if __name__ == "__main__":
    main()
