"""节点凭据开通工具（在 broker 机器上运行）——薄 CLI 封装。

> 遗留工具：v2 匿名化（git 4c805e7）后，常规入队走 join_team.py 匿名直连，
> 不再发放节点凭据。本工具仅用于历史凭据的增删查（如日后恢复认证模式时）。

核心逻辑在 agent_bus/provision.py（历史版本与 bus_server 的 /api/join 共用）。

用法:
  # 首次初始化（生成桥接账号 + 管理员令牌 + ACL/passwd 骨架）
  python scripts/add_node.py --init

  # 开通/重置一个节点（密码重新生成，令牌重新生成，旧令牌自动撤销）
  python scripts/add_node.py --agent-id codebuddy_pc1

  # 列出已发放凭据的节点
  python scripts/add_node.py --list

  # 移除节点（吊销令牌 + 删 MQTT 用户）
  python scripts/add_node.py --remove codebuddy_pc1

输出（stdout 仅此一次展示密码/令牌；data/credentials.json 在 broker 侧留档）。

新机器推荐直接用 setup_host.py（主机）/ join_team.py（子设备）自动化全流程。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_bus import provision  # noqa: E402


def print_node_env(agent_id, mqtt_pass, http_token):
    print(f"\n=== 节点 {agent_id} 凭据（仅此一次完整展示） ===")
    print(f'export BUS_MQTT_USER="{agent_id}"')
    print(f'export BUS_MQTT_PASS="{mqtt_pass}"')
    print(f'export BUS_HTTP_TOKEN="{http_token}"')
    print("\n# PowerShell (Windows) 示例:")
    print(f'$env:BUS_MQTT_USER="{agent_id}"; $env:BUS_MQTT_PASS="{mqtt_pass}"; $env:BUS_HTTP_TOKEN="{http_token}"')
    print("\n# bash (Linux/macOS) 长期使用可写入 ~/.config/agent-bus/bus.env")


def main():
    ap = argparse.ArgumentParser(description="agent-bus 节点凭据开通")
    ap.add_argument("--init", action="store_true", help="初始化：桥接账号+管理员令牌+ACL")
    ap.add_argument("--agent-id", help="要开通/重置的节点 ID")
    ap.add_argument("--remove", help="要移除的节点 ID")
    ap.add_argument("--list", action="store_true", help="列出已发放凭据的节点")
    ap.add_argument("--db", default=str(provision.ROOT_DIR / "data" / "bus.db"))
    ap.add_argument("--cred-file", default=str(provision.ROOT_DIR / "data" / "credentials.json"))
    args = ap.parse_args()

    auth_dir = provision.auth_dir()
    passwd_file, acl_file = auth_dir / "passwd", auth_dir / "acl"
    cred = provision.CredStore(Path(args.cred_file), Path(args.db))

    if args.list:
        for aid, info in cred.data["nodes"].items():
            print(f"{aid:24s} role={info['role']:8s} issued={info['issued_at']}")
        return

    if args.init:
        provision.write_acl(acl_file)
        print(f"ACL 已写入: {acl_file}")
        bridge_pw = provision.gen_password(32)
        provision.set_mqtt_password(passwd_file, provision.BRIDGE_USER, bridge_pw)
        admin_token = provision.gen_token()
        cred.save_node(provision.BRIDGE_USER, bridge_pw, admin_token, role="bridge")
        print("\n=== 桥接账号（bus_server 启动环境变量） ===")
        print(f'export BUS_MQTT_USER="{provision.BRIDGE_USER}"')
        print(f'export BUS_MQTT_PASS="{bridge_pw}"')
        print("\n=== 管理员令牌（面板登录用） ===")
        print(f"panel token: {admin_token}")
        print("\n下一步: 服务模式运行 scripts/enable_mqtt_auth_admin.ps1 启用认证；"
              "用户态模式由 setup_host.py 自动完成。")
        return

    if args.remove:
        cred.revoke_tokens(args.remove)
        provision.remove_mqtt_user(passwd_file, args.remove)
        print(f"已移除节点 {args.remove}（MQTT 用户 + HTTP 令牌已吊销）")
        print("注意: passwd 变更需重启 broker 生效（服务模式需管理员，用户态自动）")
        return

    if not args.agent_id:
        ap.error("需要 --agent-id 或 --init 或 --list 或 --remove")
    try:
        creds = cred.provision(args.agent_id)
    except ValueError as e:
        sys.exit(str(e))
    print(f"已写入 mosquitto passwd: {passwd_file}")
    print_node_env(creds["agent_id"], creds["mqtt_pass"], creds["http_token"])
    print("\n注意: mosquitto 不热加载 passwd——新增/重置节点后需重启 broker 生效:")
    print("  用户态(便携): 自动（join 流程）或 python scripts/broker_ctl.py restart")
    print("  Windows 服务: Restart-Service mosquitto（管理员）")
    print("  Linux 服务:   sudo systemctl restart mosquitto")


if __name__ == "__main__":
    main()
