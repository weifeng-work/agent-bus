"""M1 冒烟测试：通信节点 headless 模式（不依赖 pystray/broker）。

覆盖（架构 §4.1/§4.2 验收点）:
  T1 崩溃秒级重启：短命子进程退出 → 监督循环自动拉起（respawn 计数增长）
  T2 状态灯判定：状态文件 connected+新鲜 → 绿；无状态文件 → 黄
  T3 熔断：--controlled off → 不拉起子进程，灯灰

用法:
  python tests/_smoke_comm_node.py
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
CMD = [PY, str(ROOT / "executor" / "comm_node.py"), "--headless", "--install-dir", "PLACEHOLDER"]

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def run_node(args, test_seconds, pre_status=None):
    """在临时 install_dir 运行 headless 节点，返回 stdout+stderr 文本。"""
    tmp = Path(tempfile.mkdtemp(prefix="commnode-"))
    try:
        if pre_status:
            rt = tmp / "data" / "runtime"
            rt.mkdir(parents=True, exist_ok=True)
            (rt / "executor_status.json").write_text(
                json.dumps(pre_status), encoding="utf-8")
        cmd = [*CMD, *args, "--test-seconds", str(test_seconds)]
        cmd[cmd.index("PLACEHOLDER")] = str(tmp)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=40,
                           cwd=ROOT)
        return (r.stdout or "") + (r.stderr or "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("== M1 冒烟：通信节点 headless ==")

    # T1 崩溃秒级重启（ping -n 3 ≈ 3s 后退出 → 应被自动拉起）
    print("\n-- T1 崩溃秒级重启 --")
    out = run_node(["--agent-id", "smoke1", "--child-cmd",
                    "ping -n 3 127.0.0.1 >nul"], test_seconds=10)
    respawn = out.count("秒级拉起")
    check("崩溃后自动重启", respawn >= 2, f"(respawn={respawn})")
    check("无状态文件 → 黄灯", "YELLOW" in out, "")

    # T2 状态灯判定（connected + 新鲜 → 绿）
    print("\n-- T2 状态灯判定 --")
    out = run_node(["--agent-id", "smoke2", "--child-cmd",
                    "ping -n 3 127.0.0.1 >nul"], test_seconds=6,
                   pre_status={"status": "connected", "agent_id": "smoke2",
                               "health": "ok", "ts": time.time()})
    check("connected+新鲜 → 绿灯", "GREEN" in out, "")

    # T3 熔断（--controlled off → 不拉起，灰灯为初始态）
    print("\n-- T3 熔断 --")
    out = run_node(["--agent-id", "smoke3", "--child-cmd",
                    "ping -n 3 127.0.0.1 >nul", "--controlled", "off"],
                   test_seconds=6)
    check("熔断不拉起子进程", "拉起执行器子进程" not in out, "")
    check("熔断状态生效", "controlled=False" in out, "")

    print(f"\n== 结果: {PASS} 通过 / {FAIL} 失败 ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
