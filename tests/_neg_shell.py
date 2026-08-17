"""负向用例（临时）：伪造签名必须被 worker 拒绝。

- 用错误 K 签名 shell_exec 发往已配对 worker → 应回 error（验签失败）
用法: python tests/_neg_shell.py <target> <shell_control_on: 1|0>
"""
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_bus import AgentBus, BusConfig, crypto  # noqa: E402
from agent_bus.schema import make_task_request  # noqa: E402


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "node-e2e1"
    shell_on = (sys.argv[2] if len(sys.argv) > 2 else "1") == "1"

    bus = AgentBus("neg-attacker", name="attacker", executor="test")
    bus.connect(register=True, timeout=8)

    fake_key = crypto.derive_pair_key("WRONGCODE9")  # 攻击者伪造的 K
    payload = {"op": "shell_exec", "cmd": "echo pwned",
               "timeout_seconds": 15,
               "control_sig": crypto.hmac_sign(fake_key, {
                   "op": "shell_exec", "cmd": "echo pwned", "timeout_seconds": 15})}
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
        ok = result.get("status") == "error" and "验签失败" in (result.get("error") or "")
    else:
        ok = result.get("status") == "error" and "shell_control_disabled" in (result.get("error") or "")
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
