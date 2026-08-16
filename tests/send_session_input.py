"""向交互会话注入问题（session_input），等待后抓屏提取回复。

演示"人机协同"链路:
  1. 总线发 session_input（text + Enter）→ 执行器注入 TUI 输入框
  2. 等待 CLI 智能体回答（在 attach 窗口里全程可见）
  3. capture-pane 抓屏 → 按 profile 提取回复区

用法:
  python tests/send_session_input.py [target_agent_id] [问题文本]
"""
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "executor"))

from agent_bus import AgentBus, make_session_input  # noqa: E402
from mux_transport import find_mux_binary  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "codebuddy_tui1"
QUESTION = sys.argv[2] if len(sys.argv) > 2 else "追加一个问题：请用一句话介绍你自己"
SESSION = f"agentbus_{TARGET}"
WAIT_SECONDS = float(sys.argv[3]) if len(sys.argv) > 3 else 40.0

bus = AgentBus("sender_interactive", name="InteractiveTester").connect(register=False)

# 1. 注入问题 + 提交键
msg = make_session_input(
    sender_id=bus.agent_id, target_id=TARGET,
    session_id=SESSION, text=QUESTION, special="Enter",
)
bus._client.publish(f"agent/{TARGET}/inbox", __import__("json").dumps(msg), qos=1)
print(f"[ok] session_input 已发送: {QUESTION}")

# 2. 等待回答（attach 窗口里可见打字与回答过程）
print(f"[..] 等待 {WAIT_SECONDS}s 供 CLI 回答...")
time.sleep(WAIT_SECONDS)

# 3. 抓屏取回
tmux = find_mux_binary()
r = subprocess.run(
    [tmux, "capture-pane", "-p", "-t", SESSION, "-S", "-60"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
print("=" * 60)
print(r.stdout[-3000:] if r.stdout else f"[!] 抓屏失败: {r.stderr}")
bus.disconnect()
