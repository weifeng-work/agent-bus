"""Agent Bus MCP Server —— 把总线通信能力暴露为 MCP 工具。

配置（环境变量）:
  BUS_AGENT_ID       本节点 ID（必填）
  BUS_BROKER_HOST    MQTT 地址（默认 127.0.0.1）
  BUS_BROKER_PORT    MQTT 端口（默认 1883）
  BUS_HTTP_BASE      HTTP 基址（默认 http://127.0.0.1:8000）

接入（以 CodeBuddy / Claude Code 类客户端为例，写入其 MCP 配置）:
  {
    "mcpServers": {
      "agent-bus": {
        "command": "python",
        "args": ["<绝对路径>/skill/mcp_server.py"],
        "env": { "BUS_AGENT_ID": "my_agent", "BUS_HTTP_BASE": "http://<服务器IP>:8000" }
      }
    }
  }
"""
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from agent_bus import AgentBus, BusConfig  # noqa: E402

mcp = FastMCP("agent-bus")
_bus = None


def bus() -> AgentBus:
    global _bus
    if _bus is None:
        cfg = BusConfig.load()
        if not cfg.agent_id:
            raise RuntimeError("未设置 BUS_AGENT_ID 环境变量")
        _bus = AgentBus(cfg.agent_id, name=cfg.agent_id, executor="mcp", config=cfg)
        _bus.connect(register=True)
    return _bus


@mcp.tool()
def list_online_agents() -> str:
    """查看当前已注册到总线的所有智能体及其在线状态。"""
    agents = bus().list_agents()
    return json.dumps(agents, ensure_ascii=False)


@mcp.tool()
def send_task(target_id: str, instruction: str, file_paths: str = "",
              session_id: str = "", wait_seconds: int = 300) -> str:
    """给另一个智能体发送任务并等待结果。

    Args:
        target_id: 目标智能体 ID（先用 list_online_agents 查询）
        instruction: 任务指令正文
        file_paths: 可选，本地附件路径，多个用逗号分隔（会自动上传转为 URL）
        session_id: 可选，延续目标会话的 session_id（来自上次结果的 result.session_id）
        wait_seconds: 等待结果秒数，0 表示发完即回
    """
    b = bus()
    attachments = []
    for p in filter(None, [x.strip() for x in file_paths.split(",")]):
        attachments.append(b.upload(p)["url"])
    result = b.send_task(
        target_id, instruction, attachments=attachments,
        session_id=session_id or None,
        wait=wait_seconds > 0, wait_timeout=wait_seconds or None,
    )
    if result is None:
        return json.dumps({"ok": False, "error": "超时未收到结果"}, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def check_inbox(timeout_seconds: float = 3.0) -> str:
    """拉取发给我（BUS_AGENT_ID）的新消息，返回 task_request / task_result 列表。"""
    msgs = bus().poll_inbox(timeout=timeout_seconds)
    return json.dumps(msgs, ensure_ascii=False)


@mcp.tool()
def reply_task(request_json: str, output_text: str, status: str = "success") -> str:
    """回传任务结果。request_json 传 check_inbox 收到的原始请求 JSON 字符串。"""
    req = json.loads(request_json)
    bus().reply_task(req, output_text=output_text, status=status)
    return json.dumps({"ok": True}, ensure_ascii=False)


@mcp.tool()
def upload_file(path: str) -> str:
    """上传本地文件到总线文件服务，返回可放入消息的 URL。"""
    return json.dumps(bus().upload(path), ensure_ascii=False)


@mcp.tool()
def download_file(url: str, dest: str) -> str:
    """下载总线上的文件（URL 或 file_id）到本地路径。"""
    return json.dumps({"ok": True, "saved_to": bus().download(url, dest)}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
