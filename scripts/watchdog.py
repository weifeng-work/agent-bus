"""OS 计划任务兜底（架构 §2.3）：托盘壳（通信节点）若挂则拉起。

由 schtasks 每分钟调用（AgentBusShellWatchdog）：
  判活依据（防 pid 复用 + 防假活）：
    runtime/tray_shell.pid     进程 ID 且进程存在
    runtime/tray_heartbeat.ts  心跳时间戳，新鲜 <150s
  壳不在/心跳过期 → 以独立进程拉起 start_tray.bat，本次运行即退出。

用法（由 setup_tray.ps1 注册）:
  python scripts/watchdog.py --install-dir C:\\agent-bus
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STALE_SECONDS = 150.0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=15)
            return str(pid) in (r.stdout or "")
        os.kill(pid, 0)  # 探活
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Agent Bus 托盘壳 watchdog（分钟级兜底）")
    ap.add_argument("--install-dir", required=True)
    args = ap.parse_args()

    install = Path(args.install_dir).resolve()
    runtime = install / "data" / "runtime"
    pid_file = runtime / "tray_shell.pid"
    hb_file = runtime / "tray_heartbeat.ts"

    # 判活
    alive = False
    if pid_file.exists() and hb_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            hb = float(hb_file.read_text(encoding="utf-8").strip())
            alive = pid_alive(pid) and (time.time() - hb) < STALE_SECONDS
        except (ValueError, OSError):
            alive = False
    if alive:
        return  # 壳活着，无操作

    # 壳不在/假活 → 以 pythonw 直启（无黑窗口），命令行来自 shell_launch.json
    launch_file = runtime / "shell_launch.json"
    if not launch_file.exists():
        sys.exit("shell_launch.json 不存在（节点从未启动过？请先运行 setup_tray.ps1）")
    try:
        spec = json.loads(launch_file.read_text(encoding="utf-8"))
        args = [spec["exe"]] + list(spec.get("args", []))
        cwd = spec.get("cwd") or str(install)
    except Exception as e:
        sys.exit(f"shell_launch.json 解析失败: {e}")

    print(f"watchdog: 托盘壳不在/失联，以 pythonw 直启（无窗口）args={args}")
    try:
        if os.name == "nt":
            subprocess.Popen(
                args,
                cwd=cwd,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True)
        else:
            subprocess.Popen(args, cwd=cwd, start_new_session=True, close_fds=True)
    except Exception as e:
        sys.exit(f"watchdog: 拉起失败 {e}")


if __name__ == "__main__":
    main()
