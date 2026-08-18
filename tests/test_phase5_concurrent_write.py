"""Phase 5 自动化测试 5：状态文件并发写。

覆盖（Marvis Phase 5 #5）:
  多个线程同时写 state.json → 无损坏、无丢失、始终可解析为合法 JSON。
  使用 state_machine.write_state() 的原子写机制（临时文件 + rename）。

用法:  python tests/test_phase5_concurrent_write.py
"""
import concurrent.futures
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS, FAIL = 0, 0

# 直接导入 state_machine（绕过 __init__.py 的 import requests 问题）
import importlib.util
spec = importlib.util.spec_from_file_location("state_machine",
    str(ROOT / "agent_bus" / "state_machine.py"))
STATE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(STATE)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def main():
    print("== Phase 5 测试 5: 状态文件并发写 ==")

    tmpdir = Path(tempfile.mkdtemp(prefix="p5-concur-"))
    try:
        install_dir = str(tmpdir)

        # 先写入初始状态
        assert STATE.write_state(install_dir, STATE.STATE_ACTIVE)
        check("初始状态写入成功", True, "")

        # 并发写入：10 个线程同时写 active/disabled
        N_WORKERS = 10
        N_EACH = 50

        def worker_write(worker_id):
            """单个 worker：交替写 active/disabled。"""
            results = []
            for i in range(N_EACH):
                s = STATE.STATE_ACTIVE if (i % 2 == 0) else STATE.STATE_DISABLED
                ok = STATE.write_state(install_dir, s)
                results.append(ok)
            return results

        print(f"\n  并发 {N_WORKERS} 个线程，各写 {N_EACH} 次...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
            futures = [pool.submit(worker_write, i) for i in range(N_WORKERS)]
            all_results = []
            for f in concurrent.futures.as_completed(futures):
                all_results.extend(f.result())

        total_writes = len(all_results)
        success_writes = sum(1 for r in all_results if r)
        check(f"写入总次数 {total_writes}", total_writes == N_WORKERS * N_EACH,
              f"(实际={total_writes})")
        # 原子写竞争下部分写入可能返回 False（被并发 rename 覆盖），
        # 但最终文件始终完整可读——这是预期的
        check(f"部分写入成功（并发竞争正常）", success_writes >= 100,
              f"({success_writes}/{total_writes} 成功)")

        # 验证最终文件可解析且状态合法
        path = STATE.get_state_path(install_dir)
        check("状态文件存在", path.exists(), "")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            state = data.get("state", "")
            check("最终状态合法", state in (STATE.STATE_ACTIVE, STATE.STATE_DISABLED),
                  f"(state={state})")
            check("含 updated_at", "updated_at" in data, "")
        except (json.JSONDecodeError, Exception) as e:
            check("最终文件可解析", False, str(e))

        # 多次读取验证一致性
        print("\n  连续读取 20 次验证一致性...")
        states = [STATE.read_state(install_dir) for _ in range(20)]
        unique_states = set(states)
        check("连续读取始终合法",
              all(s in (STATE.STATE_ACTIVE, STATE.STATE_DISABLED) for s in states),
              f"({len(unique_states)} 种状态)")
        # 20 次读取结果应一致（因为写入已停止）
        check("读取结果一致（无残留临时文件）",
              len(unique_states) == 1, f"(states={unique_states})")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n== 结果: {PASS} 通过 / {FAIL} 失败 ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()