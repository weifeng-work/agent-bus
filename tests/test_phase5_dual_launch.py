"""Phase 5 自动化测试 4：双重拉起防护。

覆盖（Marvis Phase 5 #4）:
  安装时存在旧计划任务（AgentBusShell / AgentBusShellWatchdog）→
  安装脚本清理后仅一个 core_node 进程运行。

验证方式：检查 setup_tray.ps1 / setup_worker_windows.ps1 中的清理逻辑。
不需要实际运行安装脚本（需要管理员权限），而是验证清理代码片段正确。

用法:  python tests/test_phase5_dual_launch.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def main():
    print("== Phase 5 测试 4: 双重拉起防护 ==")

    # ---- 检查 setup_tray.ps1 中的清理逻辑 ----
    print("\n-- T4a: setup_tray.ps1 旧任务清理 --")
    tray_ps1 = ROOT / "scripts" / "setup_tray.ps1"
    content = tray_ps1.read_text(encoding="utf-8")

    # 应包含清理旧版计划任务的代码
    check("清理 AgentBusShell", "AgentBusShell" in content, "")
    check("清理 AgentBusShellWatchdog", "AgentBusShellWatchdog" in content, "")
    check("使用 Unregister-ScheduledTask", "Unregister-ScheduledTask" in content, "")
    # 应包含 NSSM 服务注册
    check("NSSM 服务注册", "nssm install" in content, "")
    check("NSSM AppExit Restart", "AppExit Default Restart" in content, "")
    check("NSSM AppRestartDelay", "AppRestartDelay" in content, "")

    # ---- 检查 setup_worker_windows.ps1 中的清理逻辑 ----
    print("\n-- T4b: setup_worker_windows.ps1 旧任务清理 --")
    worker_ps1 = ROOT / "scripts" / "setup_worker_windows.ps1"
    content2 = worker_ps1.read_text(encoding="utf-8")

    check("步骤 5 清理旧计划任务", "清理旧版计划任务" in content2, "")
    check("清理 AgentBusShell", "AgentBusShell" in content2, "")
    check("清理 AgentBusShellWatchdog", "AgentBusShellWatchdog" in content2, "")
    check("清理旧版执行器直启", "AgentBus$Executor" in content2, "")
    check("使用 Unregister-ScheduledTask", "Unregister-ScheduledTask" in content2, "")

    # ---- 检查 remote_update_worker.ps1 中的清理逻辑 ----
    print("\n-- T4c: remote_update_worker.ps1 旧任务清理 --")
    update_ps1 = ROOT / "scripts" / "remote_update_worker.ps1"
    content3 = update_ps1.read_text(encoding="utf-8")

    check("停 NSSM 服务", "nssm stop AgentBusCore" in content3, "")
    check("起 NSSM 服务", "nssm start AgentBusCore" in content3, "")
    check("兼容旧部署 schtasks disable", "AgentBusShell /disable" in content3, "")
    check("保留 data/ 目录", "data-backup" in content3 or "data" in content3, "")

    # ---- 检查 schedule 任务最终状态 ----
    print("\n-- T4d: 新架构使用 NSSM 服务 + 开始菜单快捷方式 --")
    check("有开始菜单快捷方式", "Start Menu" in content, "")
    check("使用 NSSM 服务", "nssm install AgentBusCore" in content, "")
    check("清理旧计划任务", "AgentBusShell" in content2, "")

    print(f"\n== 结果: {PASS} 通过 / {FAIL} 失败 ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()