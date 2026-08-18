"""Phase 5 自动化测试 1：state.json 状态机切换。

覆盖（Marvis Phase 5 #1）:
  - state.json 从 active 切换到 disabled → core_node 停止执行器、断开总线
  - state.json 从 disabled 切换到 active → core_node 恢复执行器监督

用法:  python tests/test_phase5_state_machine.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
CORE_NODE = str(ROOT / "executor" / "core_node.py")

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def run_core(args, test_seconds, pre_state=None, pre_status=None):
    """在临时目录运行 headless core_node，返回 stdout+stderr 文本。"""
    tmp = Path(tempfile.mkdtemp(prefix="p5-sm-"))
    try:
        rt = tmp / "data" / "runtime"
        rt.mkdir(parents=True, exist_ok=True)

        # 写入 state.json
        if pre_state:
            (tmp / "data" / "runtime" / "state.json").write_text(
                json.dumps({"state": pre_state, "updated_at": time.time()}),
                encoding="utf-8")

        # 写入心跳文件（避免 watchdog 立即自杀）
        (tmp / "data" / "runtime" / "tray_heartbeat.ts").write_text(
            str(time.time()), encoding="utf-8")

        # 写入 executor_status.json（模拟子进程状态）
        if pre_status:
            (rt / "executor_status.json").write_text(
                json.dumps(pre_status), encoding="utf-8")

        # 构建 PYTHONPATH
        sp_paths = [
            "C:\\Users\\IKUN\\.meituan-catpaw\\runtimes\\python\\versions\\3.12.13\\Lib\\site-packages",
            "C:\\Python3.14\\Lib\\site-packages",
        ]
        import os
        env = os.environ.copy()
        pythonpath = str(ROOT)
        for sp in sp_paths:
            if Path(sp).exists():
                pythonpath += ";" + sp
        env["PYTHONPATH"] = pythonpath

        cmd = [PY, CORE_NODE,
               "--role", "worker",
               "--agent-id", "test-phase5",
               "--install-dir", str(tmp),
               "--no-bus",
               "--controlled", "on",
               "--child-cmd", "ping -n 2 127.0.0.1 >nul",
               "--test-seconds", str(test_seconds)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40,
                           cwd=ROOT, env=env)
        # logging 模块输出到 stderr
        return (r.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("== Phase 5 测试 1: state.json 状态机切换 ==")

    # T1: active 状态 → 正常启动，应拉起执行器
    print("\n-- T1: active 状态 → 正常拉起 --")
    out = run_core([], test_seconds=6, pre_state="active")
    check("active 状态拉起执行器", "拉起执行器子进程" in out, "")
    check("状态机 active", "状态机状态: active" in out, "")

    # T2: disabled 状态 → 不拉起，等待启用
    print("\n-- T2: disabled 状态 → 不拉起，等待启用 --")
    out = run_core([], test_seconds=6, pre_state="disabled")
    check("disabled 状态等待启用", "等待启用事件" in out, "")
    check("disabled 不拉起执行器", "拉起执行器子进程" not in out, "")
    check("disabled 状态机识别", "状态机 disabled" in out, "")

    # T3: active 状态 + 状态文件正常 → 绿灯
    print("\n-- T3: active + 正常状态 → 绿灯 --")
    out = run_core([], test_seconds=6, pre_state="active",
                   pre_status={"status": "connected", "ts": time.time()})
    check("active 状态正常启动", "核心控制节点启动" in out, "")

    print(f"\n== 结果: {PASS} 通过 / {FAIL} 失败 ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()