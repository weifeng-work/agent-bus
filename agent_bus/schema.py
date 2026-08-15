"""报文构造与轻量校验。契约见 docs/protocol.md。"""
import time
import uuid

PROTOCOL_VERSION = "1.0"


def _base(msg_type: str, sender_id: str) -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": msg_type,
        "sender_id": sender_id,
    }


def make_register(agent_id: str, name: str, capabilities=None, platform="", executor="") -> dict:
    msg = _base("register", agent_id)
    msg.update({
        "agent_id": agent_id,
        "name": name,
        "capabilities": capabilities or [],
        "platform": platform,
        "executor": executor,
        "registered_at": time.time(),
    })
    return msg


def make_task_request(
    sender_id: str,
    target_id: str,
    instruction: str,
    context_data=None,
    attachment_urls=None,
    session_id=None,
    timeout_seconds=600,
) -> dict:
    task_id = str(uuid.uuid4())
    msg = _base("task_request", sender_id)
    msg.update({
        "task_id": task_id,
        "correlation_id": str(uuid.uuid4()),
        "target_id": target_id,
        "reply_to": f"agent/{sender_id}/inbox",
        "payload": {
            "instruction": instruction,
            "context_data": context_data,
            "attachment_urls": attachment_urls or [],
            "session_id": session_id,
        },
        "timeout_seconds": timeout_seconds,
        "created_at": time.time(),
    })
    return msg


def make_task_result(
    sender_id: str,
    request: dict,
    output_text: str = "",
    status: str = "success",
    error=None,
    artifacts=None,
    session_id=None,
) -> dict:
    msg = _base("task_result", sender_id)
    msg.update({
        "task_id": request.get("task_id", ""),
        "correlation_id": request.get("correlation_id", ""),
        "status": status,
        "result": {
            "output_text": output_text,
            "artifacts": artifacts or [],
            "session_id": session_id,
        },
        "error": error,
        "completed_at": time.time(),
    })
    return msg


def validate(msg: dict) -> list:
    """轻量校验，返回错误列表；空列表表示合法。"""
    errors = []
    if not isinstance(msg, dict):
        return ["message is not a dict"]
    if msg.get("protocol_version") != PROTOCOL_VERSION:
        errors.append(f"protocol_version should be {PROTOCOL_VERSION!r}")
    t = msg.get("type")
    if t not in ("register", "task_request", "task_result"):
        errors.append(f"unknown type: {t!r}")
        return errors
    if not msg.get("sender_id"):
        errors.append("sender_id is required")
    if t == "task_request":
        if not msg.get("target_id"):
            errors.append("target_id is required")
        if not msg.get("correlation_id"):
            errors.append("correlation_id is required")
        if not msg.get("task_id"):
            errors.append("task_id is required")
        if not msg.get("reply_to"):
            errors.append("reply_to is required")
    if t == "task_result":
        if not msg.get("correlation_id"):
            errors.append("correlation_id is required")
        if msg.get("status") not in ("success", "error", "timeout"):
            errors.append("status must be success|error|timeout")
    return errors


def inbox_topic(agent_id: str) -> str:
    return f"agent/{agent_id}/inbox"
