"""向交互式执行器发任务的发送方测试脚本（观察进度流 + 最终结果）。"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_bus import AgentBus

TARGET = sys.argv[1] if len(sys.argv) > 1 else "codebuddy_tui1"
INSTRUCTION = sys.argv[2] if len(sys.argv) > 2 else "请只回复六个字：交互链路已打通"

bus = AgentBus("sender_interactive", name="InteractiveTester").connect(register=False)
print(f"在线 agents: {[a['agent_id'] for a in bus.list_agents()]}")

# wait=False 发送，自己消费 task_progress 流 + task_result
req = bus.send_task(TARGET, INSTRUCTION, timeout_seconds=300, wait=False)
print(f"任务已发送 task={req['task_id'][:8]} corr={req['correlation_id'][:8]}")

deadline = time.time() + 300
result = None
progress_count = 0
while time.time() < deadline:
    for msg in bus.poll_inbox(timeout=2.0):
        t = msg.get("type")
        if t == "task_progress":
            progress_count += 1
            phase = msg.get("phase")
            text = (msg.get("result", {}).get("output_text") or "").strip()
            preview = text.splitlines()[-1][:100] if text else ""
            print(f"[progress #{msg.get('seq')} {phase}] {preview}")
        elif t == "task_result":
            result = msg
            break
    if result:
        break

print("\n" + "=" * 60)
if result:
    print(f"status   : {result.get('status')}")
    print(f"error    : {result.get('error')}")
    print(f"session  : {result.get('result', {}).get('session_id')}")
    print(f"progress : 收到 {progress_count} 条进度")
    print(f"output   :\n{result.get('result', {}).get('output_text', '')}")
else:
    print("超时未收到结果")
bus.disconnect()
sys.exit(0 if result and result.get("status") == "success" else 1)
