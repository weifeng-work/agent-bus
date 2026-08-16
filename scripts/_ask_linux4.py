"""重发 linux-compat-02 核实任务（debian_worker 已通过 SSH 用新代码重启）。"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent_bus import AgentBus, BusConfig  # noqa: E402
from _ask_linux3 import STEP2  # noqa: E402

CREDS = json.loads((ROOT / "data" / "credentials.json").read_text(encoding="utf-8"))["nodes"]["trae_agent"]


def main():
    cfg = BusConfig.load(agent_id="trae_agent")
    cfg.mqtt_user, cfg.mqtt_pass, cfg.http_token = (
        CREDS["mqtt_user"], CREDS["mqtt_pass"], CREDS["http_token"])
    bus = AgentBus("trae_agent", name="trae_agent", config=cfg)
    bus.connect()
    print("发 linux-compat-02-r3 核实任务...")
    req = bus.send_task("debian_worker", STEP2, timeout_seconds=1200, wait=False)
    print("task:", req["task_id"])

    result = None
    deadline = time.time() + 1200
    while time.time() < deadline:
        for msg in bus.poll_inbox(timeout=3.0):
            if msg.get("type") == "task_result" and msg.get("correlation_id") == req["correlation_id"]:
                result = msg
        if result:
            break
    print("\n" + "=" * 60)
    if result:
        print("status:", result.get("status"))
        out = (result.get("result") or {}).get("output_text") or ""
        (ROOT / "tests" / "linux_compat02_review.txt").write_text(out, encoding="utf-8")
        print(out)
        print(f"\n[全文 {len(out)} 字符已存 tests/linux_compat02_review.txt]")
    else:
        print("超时未收到结果")
    bus.disconnect()
    sys.exit(0 if result and result.get("status") == "success" else 1)


if __name__ == "__main__":
    main()
