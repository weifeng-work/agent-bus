"""用户态 broker 进程管理（setup_host.py 启动的便携 mosquitto）。

用法:
  python scripts/broker_ctl.py status    # 查看 pid/端口/进程存活
  python scripts/broker_ctl.py restart   # 重启（join 后手动生效用；join 已自动调用）
  python scripts/broker_ctl.py stop
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_bus import provision  # noqa: E402


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    pid_file = provision.ROOT_DIR / "data" / "broker.pid"

    if not pid_file.exists():
        print("未找到 data/broker.pid——服务模式部署请用系统服务管理器")
        sys.exit(1)

    pid = pid_file.read_text().strip()
    if cmd == "status":
        import subprocess
        alive = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
        print(f"broker pid: {pid}")
        print("alive" if pid in alive else "dead")
    elif cmd == "restart":
        ok, msg = provision.restart_user_broker()
        print(msg)
        sys.exit(0 if ok else 1)
    elif cmd == "stop":
        import os
        try:
            os.kill(int(pid), 9)
            pid_file.unlink(missing_ok=True)
            print(f"已停止 pid={pid}")
        except (OSError, ValueError) as e:
            print(f"停止失败: {e}")
            sys.exit(1)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
