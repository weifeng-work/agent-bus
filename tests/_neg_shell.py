"""负向用例：非 hub 身份发送控制消息必须被 worker 拒绝。

- 用 node-xxx 身份发送 shell_exec → 应回 error（sender 身份检查不通过）
用法: python tests/_neg_shell.py <target> <shell_control_on: 1|0>
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_bus import AgentBus, BusConfig  # noqa: E402
from agent_bus.schema import make_task_request  # noqa: E402


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "node-e2e1"
    shell_on = (sys.argv[2] if len(sys.argv) > 2 else "1") == "1"

    # 以 node-xxx 身份（非 hub）发送 shell_exec → 应被拒绝
    bus = AgentBus("neg-attacker", name="attacker", executor="test")
    bus.connect(register=True, timeout=8)

    payload = {"op": "shell_exec", "cmd": "echo pwned",
               "timeout_seconds": 15}
    req = make_task_request(bus.agent_id, target, instruction="",
                            timeout_seconds=15)
    req["payload"] = payload
    result = bus.send_msg(target, req, wait=True, wait_timeout=30)
    bus.disconnect()
    if result is None:
        print("FAIL: 无回执")
        sys.exit(1)
    print(f"status={result.get('status')} error={result.get('error')}")
    if shell_on:
        # shell_control 已开启，但 sender 非 hub → 应拒绝
        ok = result.get("status") == "error" and "仅接受 hub 身份" in (result.get("error") or "")
    else:
        # shell_control 未开启，先返回 shell_control_disabled
        ok = result.get("status") == "error" and "shell_control_disabled" in (result.get("error") or "")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()