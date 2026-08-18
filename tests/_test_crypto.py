"""控制面身份检查单测（简化版）：is_hub_message / is_control_op

用法: python tests/_test_crypto.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_bus import crypto  # noqa: E402

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
    print("== crypto 身份检查单测 ==")

    # is_hub_message
    check("hub-xxx 识别为 hub",
          crypto.is_hub_message("hub-desktop"))
    check("node-xxx 非 hub",
          not crypto.is_hub_message("node-pc1"))
    check("codebuddy_pc1 非 hub",
          not crypto.is_hub_message("codebuddy_pc1"))
    check("空字符串非 hub",
          not crypto.is_hub_message(""))
    check("None 非 hub",
          not crypto.is_hub_message(None))

    # is_control_op
    check("shell_exec 是控制操作",
          crypto.is_control_op("shell_exec"))
    check("executor_activate 是控制操作",
          crypto.is_control_op("executor_activate"))
    check("run 不是控制操作",
          not crypto.is_control_op("run"))
    check("空字符串不是控制操作",
          not crypto.is_control_op(""))

    # canonical 保留兼容
    a = crypto.canonical({"b": 2, "a": [1, {"z": 0}]})
    b = crypto.canonical({"a": [1, {"z": 0}], "b": 2})
    check("canonical 键排序确定性", a == b, a)

    print(f"\n== 结果: {PASS} 通过 / {FAIL} 失败 ==")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()