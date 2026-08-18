"""Agent Bus 服务管理器 —— NSSM 服务包装入口 + 管理工具。

本脚本有两个角色：
  1. NSSM 服务入口：被 NSSM 作为服务启动，直接运行 core_node.py
  2. 管理工具 CLI：install / remove / start / stop / status 服务

用法：
  # 作为服务入口（NSSM 配置的 Application Path）
  python scripts/agent_service.py --role worker --agent-id node-pc1 --install-dir C:\agent-bus

  # 管理服务
  python scripts/agent_service.py install --install-dir C:\agent-bus --role worker --agent-id node-pc1
  python scripts/agent_service.py remove
  python scripts/agent_service.py start
  python scripts/agent_service.py stop
  python scripts/agent_service.py status
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# 确保能找到项目模块
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import state_machine  # noqa: E402

log = logging.getLogger("agent_service")

# NSSM 路径
NSSM_REL = "scripts/_dl/nssm.exe"
SERVICE_NAME = "AgentBusCore"


def _get_nssm_path(install_dir: str) -> str:
    """返回 nssm.exe 的完整路径。"""
    return str(Path(install_dir) / NSSM_REL)


def _get_python_exe() -> str:
    """返回当前 Python 解释器路径（用于 NSSM 注册）。"""
    return sys.executable


# ---------------------------------------------------------------------------
# 服务入口 —— 被 NSSM 调用
# ---------------------------------------------------------------------------


def run_service(args):
    """作为服务启动：运行 core_node.py。

    NSSM 配置 Application Path 指向本脚本，本脚本再委托给 core_node.py。
    """
    log.info("AgentBusCore 服务启动 install_dir=%s", args.install_dir)

    # 读取 state.json
    sv = state_machine.read_state(args.install_dir)
    log.info("当前状态机: %s", sv)

    if sv == state_machine.STATE_DISABLED:
        # disabled 状态：服务休眠，不拉起任何进程
        # 轮询 state.json 等待变为 active（托盘用户点「启用」）
        # 或等待外部事件（命名事件通知，D9 预留）
        log.info("状态机 disabled：服务进入休眠，等待启用事件")
        _wait_for_enable(args.install_dir)

    # 走到这里说明 active 状态，委托 core_node.py
    log.info("状态机 active：启动核心控制节点")
    _run_core_node(args)


def _wait_for_enable(install_dir: str, poll_interval: float = 2.0):
    """disabled 状态下休眠，以轮询方式等待 state.json 变为 active。

    当托盘用户点「启用」写入 active 时，本函数返回，继续执行启动流程。
    """
    while True:
        sv = state_machine.read_state(install_dir)
        if sv == state_machine.STATE_ACTIVE:
            log.info("检测到状态机切换为 active，继续启动")
            return
        time.sleep(poll_interval)


def _run_core_node(args):
    """委托 core_node.py 运行。"""
    core_node_path = Path(args.install_dir) / "executor" / "core_node.py"
    cmd = [
        _get_python_exe(), str(core_node_path),
        "--role", args.role,
        "--agent-id", args.agent_id,
        "--install-dir", args.install_dir,
    ]
    if args.executor:
        cmd += ["--executor", args.executor]
    if args.executor_agent_id:
        cmd += ["--executor-agent-id", args.executor_agent_id]
    if args.enable_shell_control:
        cmd += ["--enable-shell-control"]
    if args.queue:
        cmd += ["--queue", args.queue]

    log.info("委托 core_node: %s", " ".join(cmd))

    # 直接替换进程（NSSM 会监控本进程的退出状态）
    os.execv(sys.executable, cmd)


# ---------------------------------------------------------------------------
# 管理工具 CLI
# ---------------------------------------------------------------------------


def cmd_install(args):
    """安装/注册 Windows 服务（NSSM）。"""
    nssm = _get_nssm_path(args.install_dir)
    if not Path(nssm).exists():
        print(f"error: NSSM 未找到: {nssm}")
        print("请先确保 scripts/_dl/nssm.exe 存在，或重新下载")
        sys.exit(1)

    # 构建 core_node 参数
    core_args = [
        str(Path(args.install_dir) / "scripts" / "agent_service.py"),
        "--role", args.role,
        "--agent-id", args.agent_id,
        "--install-dir", args.install_dir,
    ]
    if args.executor:
        core_args += ["--executor", args.executor]
    if args.executor_agent_id:
        core_args += ["--executor-agent-id", args.executor_agent_id]
    if args.enable_shell_control:
        core_args += ["--enable-shell-control"]
    if args.queue:
        core_args += ["--queue", args.queue]

    python_exe = _get_python_exe()

    # 先确保服务不存在
    subprocess.run([nssm, "stop", SERVICE_NAME, "confirm"],
                   capture_output=True, timeout=15)
    subprocess.run([nssm, "remove", SERVICE_NAME, "confirm"],
                   capture_output=True, timeout=15)
    time.sleep(1)

    # 安装服务
    # NSSM 命令行格式：nssm install <服务名> <应用路径> [参数...]
    install_cmd = [nssm, "install", SERVICE_NAME, python_exe] + core_args
    print(f"安装服务: {SERVICE_NAME}")
    print(f"  Application: {python_exe}")
    print(f"  Arguments: {' '.join(core_args)}")
    r = subprocess.run(install_cmd, capture_output=True, timeout=30)
    if r.returncode != 0:
        print(f"error: 安装失败 (exit={r.returncode})")
        print(r.stderr.decode("utf-8", errors="replace"))
        sys.exit(1)

    # 设置 NSSM 参数
    app_dir = str(Path(args.install_dir))
    settings = [
        (nssm, "set", SERVICE_NAME, "AppDirectory", app_dir),
        (nssm, "set", SERVICE_NAME, "Start", "SERVICE_AUTO_START"),
        (nssm, "set", SERVICE_NAME, "AppExit", "Default", "Restart"),
        (nssm, "set", SERVICE_NAME, "AppRestartDelay", "5000"),
        (nssm, "set", SERVICE_NAME, "AppStdout", str(Path(args.install_dir) / "data" / "service.log")),
        (nssm, "set", SERVICE_NAME, "AppStderr", str(Path(args.install_dir) / "data" / "service.err.log")),
    ]
    for s in settings:
        subprocess.run(list(s), capture_output=True, timeout=15)

    # 启动服务
    subprocess.run([nssm, "start", SERVICE_NAME], capture_output=True, timeout=30)
    print(f"服务 {SERVICE_NAME} 已安装并启动")
    print(f"  类型: 自动启动")
    print(f"  重启: 崩溃/退出后 5s 自动重启")
    print(f"  日志: {app_dir}\\data\\service.log")


def cmd_remove(args):
    """移除服务。"""
    nssm = _get_nssm_path(args.install_dir)
    if not Path(nssm).exists():
        print(f"error: NSSM 未找到: {nssm}")
        sys.exit(1)
    print(f"移除服务: {SERVICE_NAME}")
    subprocess.run([nssm, "stop", SERVICE_NAME, "confirm"],
                   capture_output=True, timeout=15)
    r = subprocess.run([nssm, "remove", SERVICE_NAME, "confirm"],
                       capture_output=True, timeout=15)
    if r.returncode == 0:
        print("服务已移除")
    else:
        print(f"移除失败或服务不存在 (exit={r.returncode})")


def cmd_start(args):
    """启动服务。"""
    nssm = _get_nssm_path(args.install_dir)
    r = subprocess.run([nssm, "start", SERVICE_NAME],
                       capture_output=True, timeout=30)
    if r.returncode == 0:
        print(f"服务 {SERVICE_NAME} 已启动")
    else:
        print(r.stderr.decode("utf-8", errors="replace"))


def cmd_stop(args):
    """停止服务。"""
    nssm = _get_nssm_path(args.install_dir)
    r = subprocess.run([nssm, "stop", SERVICE_NAME, "confirm"],
                       capture_output=True, timeout=30)
    if r.returncode == 0:
        print(f"服务 {SERVICE_NAME} 已停止")
    else:
        print(r.stderr.decode("utf-8", errors="replace"))


def cmd_status(args):
    """查询服务状态。"""
    nssm = _get_nssm_path(args.install_dir)
    r = subprocess.run([nssm, "status", SERVICE_NAME],
                       capture_output=True, timeout=15)
    out = r.stdout.decode("utf-8", errors="replace").strip()
    err = r.stderr.decode("utf-8", errors="replace").strip()
    if r.returncode == 0:
        print(f"Service {SERVICE_NAME}: {out}")
    else:
        # 尝试用 sc 查询
        r2 = subprocess.run(["sc", "query", SERVICE_NAME],
                            capture_output=True, timeout=15)
        print(r2.stdout.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description="Agent Bus 服务管理")
    sub = ap.add_subparsers(dest="command")

    # 服务入口（无子命令 = 被 NSSM 调用）
    ap.add_argument("--role", choices=("worker", "hub"), default="worker")
    ap.add_argument("--agent-id", default="")
    ap.add_argument("--install-dir", default=str(ROOT_DIR))
    ap.add_argument("--executor", default="codebuddy")
    ap.add_argument("--executor-agent-id", default="")
    ap.add_argument("--enable-shell-control", action="store_true")
    ap.add_argument("--queue", default="")

    # 管理子命令
    p_install = sub.add_parser("install", help="安装服务")
    p_install.add_argument("--role", choices=("worker", "hub"), default="worker")
    p_install.add_argument("--agent-id", required=True)
    p_install.add_argument("--install-dir", default=str(ROOT_DIR))
    p_install.add_argument("--executor", default="codebuddy")
    p_install.add_argument("--executor-agent-id", default="")
    p_install.add_argument("--enable-shell-control", action="store_true")
    p_install.add_argument("--queue", default="")

    sub.add_parser("remove", help="移除服务")
    sub.add_parser("start", help="启动服务")
    sub.add_parser("stop", help="停止服务")
    sub.add_parser("status", help="查询状态")

    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    # 管理命令需要 install-dir 参数
    if args.command in ("remove", "start", "stop", "status"):
        # 从默认配置读取 install-dir
        if not hasattr(args, "install_dir") or not args.install_dir:
            args.install_dir = str(ROOT_DIR)

    if args.command == "install":
        cmd_install(args)
    elif args.command == "remove":
        cmd_remove(args)
    elif args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        # 无子命令 = 服务入口模式
        run_service(args)


if __name__ == "__main__":
    main()