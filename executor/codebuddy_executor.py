"""CodeBuddy 节点执行器：把总线任务注入 CodeBuddy CLI 并回传其输出。

原理（一次性拉起 + 会话延续）:
  收到 task_request
    → 下载附件到工作目录
    → 构造协作提示词（含任务信封: 发起方/执行方/correlation_id）
    → 拉起 `codebuddy -p [--resume <sid>] "<prompt>" --output-format json -y`
    → 解析 stdout 中的 JSON（result / session_id）
    → reply_task 回传给发起方

用法:
  python executor/codebuddy_executor.py --agent-id codebuddy_pc1 --name "CodeBuddy@PC1"
  python executor/codebuddy_executor.py --mock          # 不调 CodeBuddy，模拟执行（联调用）
"""
import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_bus import AgentBus, BusConfig  # noqa: E402
from agent_bus.files import download_file  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("codebuddy_executor")

PROMPT_TEMPLATE = """你正在参与一个跨机器多智能体协作系统，有任务需要你完成。

【任务信封】(多智能体防串扰，请核对后再执行)
- 发起方 agent_id: {sender_id}
- 执行方 agent_id: {receiver_id} (即你)
- 任务编号 correlation_id: {task_id}

【任务指令】
{instruction}
{context_block}{attachment_block}
协作规则:
1. 核对执行方 ID 确实是你；直接执行任务，不要询问。
2. 如需与其他智能体协作（含向发起方追问），用 Bash 调用通信 CLI（工作目录: {workdir}），务必指定准确的 --to <agent_id>:
   - 查看在线智能体: python "{cli_path}" agents
   - 给指定智能体发任务: python "{cli_path}" send --to <agent_id> --text "任务指令" --wait 300
   - 上传文件给对方: python "{cli_path}" upload <文件路径>
3. 完成后，把最终结论直接写在你的最终回复里（系统会按任务编号自动回传给发起方 {sender_id}）。回复不要包含与任务无关的内容。"""


def find_codebuddy() -> str:
    for name in ("codebuddy", "codebuddy.cmd", "codebuddy.exe"):
        path = shutil.which(name)
        if path:
            return path
    raise FileNotFoundError("找不到 codebuddy 可执行文件，请确认已安装并在 PATH 中")
