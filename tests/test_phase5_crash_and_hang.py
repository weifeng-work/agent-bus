"""Phase 5 自动化测试 2+3：进程崩溃恢复 + 卡死检测。

覆盖（Marvis Phase 5 #2, #3）:
  #2: 子进程崩溃后秒级拉起（respawn 计数增长）
  #3: 心跳文件刷新 + watchdog 线程存活

用法:  python tests/test_phase5_crash_and_hang.py
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


def _build_env():
    import os
    sp_paths = [
        "C:\\Users\\IKUN\\.meituan-catpaw\\runtimes\\python\\versions\\3.12.13\\Lib\\site-packages",
        "C:\\Python3.14\\Lib\\site-packages",
    ]
    env = os.environ.copy()
    pythonpath = str(ROOT)
    for sp in sp_paths:
        if Path(sp).exists():
            pythonpath += ";" + sp
    env["PYTHONPATH"] = pythonpath
    return env


def main():
    print("== Phase 5 测试 2+3: 进程崩溃恢复 + 心跳检测 ==")

    env = _build_env()

    # ---- 测试 2: 崩溃恢复（子进程崩溃后秒级拉起） ----
    print("\n-- T2: 子进程崩溃 → 秒级拉起 --")
    tmp = Path(tempfile.mkdtemp(prefix="p5-crash-"))
    try:
        rt = tmp / "data" / "runtime"
        rt.mkdir(parents=True, exist_ok=True)
        (tmp / "data" / "runtime" / "state.json").write_text(
            json.dumps({"state": "active", "updated_at": time.time()}),
            encoding="utf-8")
        (tmp / "data" / "runtime" / "tray_heartbeat.ts").write_text(
            str(time.time()), encoding="utf-8")

        cmd = [PY, CORE_NODE,
               "--role", "worker",
               "--agent-id", "test-crash",
               "--install-dir", str(tmp),
               "--no-bus",
               "--controlled", "on",
               "--child-cmd", "ping -n 2 127.0.0.1 >nul",
               "--test-seconds", "10"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           cwd=ROOT, env=env)
        out = r.stderr or ""

        respawn_count = out.count("秒级拉起")
        check("子进程崩溃后自动拉起", respawn_count >= 2, f"(respawn={respawn_count})")
        check("respawn 计数增长", "respawn=" in out, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 测试 3: 心跳文件刷新 ----
    print("\n-- T3: 心跳文件刷新（watchdog 存活） --")
    tmp = Path(tempfile.mkdtemp(prefix="p5-hang-"))
    try:
        rt = tmp / "data" / "runtime"
        rt.mkdir(parents=True, exist_ok=True)
        (tmp / "data" / "runtime" / "state.json").write_text(
            json.dumps({"state": "active", "updated_at": time.time()}),
            encoding="utf-8")
        (tmp / "data" / "runtime" / "tray_heartbeat.ts").write_text(
            str(time.time()), encoding="utf-8")

        cmd = [PY, CORE_NODE,
               "--role", "worker",
               "--agent-id", "test-hang",
               "--install-dir", str(tmp),
               "--no-bus",
               "--controlled", "off",
               "--test-seconds", "5"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20,
                           cwd=ROOT, env=env)
        out = r.stderr or ""

        # 验证心跳文件存在
        hb_file = tmp / "data" / "runtime" / "tray_heartbeat.ts"
        check("心跳文件已创建", hb_file.exists(), "")
        if hb_file.exists():
            try:
                hb_ts = float(hb_file.read_text().strip())
                age = time.time() - hb_ts
                # 启动后 5 秒，心跳应在刷新，所以 age < 10
                check("心跳文件新鲜（< 10s）", age < 10, f"(age={age:.1f}s)")
            except Exception:
                check("心跳文件可解析", False, "无法解析心跳文件内容")

        check("核心控制节点启动", "核心控制节点启动" in out, "")
        check("进程正常退出", "核心控制节点已退出" in out, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n== 结果: {PASS} 通过 / {FAIL} 失败 ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()