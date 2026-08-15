# Agent Bus 通信契约 v1.0

所有跨节点消息均为 UTF-8 JSON，通过 MQTT 传递（QoS 1）。

## 1. MQTT 主题设计

| 主题 | 方向 | 用途 |
|---|---|---|
| `agent/{agent_id}/inbox` | 发往该 Agent | 任务请求 / 任务结果的投递箱 |
| `bus/register` | Agent → 总线 | 注册声明（保留消息） |
| `bus/heartbeat/{agent_id}` | Agent → 总线 | 心跳（默认 30s 一次） |
| `bus/offline/{agent_id}` | Broker → 总线 | MQTT 遗嘱（LWT），异常掉线时自动发布 |

## 2. 报文规范

所有报文携带公共字段：`protocol_version`、`type`、`sender_id`。

### 2.1 注册报文 `register`

Agent 启动后发布到 `bus/register`（retain=true，新加入者可立即获取当前在线名单）。

```json
{
  "protocol_version": "1.0",
  "type": "register",
  "agent_id": "codebuddy_pc1",
  "name": "CodeBuddy @ DESKTOP-ABC",
  "capabilities": ["code", "files", "shell"],
  "platform": "windows",
  "executor": "codebuddy_cli",
  "registered_at": 1773400000.0
}
```

### 2.2 任务请求 `task_request`

发送方发布到 `agent/{target_id}/inbox`。

```json
{
  "protocol_version": "1.0",
  "type": "task_request",
  "task_id": "uuid4",
  "correlation_id": "uuid4",
  "sender_id": "agent_alpha",
  "target_id": "codebuddy_pc1",
  "reply_to": "agent/agent_alpha/inbox",
  "payload": {
    "instruction": "请分析附件并输出总结",
    "context_data": "补充上下文文本或元数据",
    "attachment_urls": ["http://bus:8000/api/files/abc123"],
    "session_id": null
  },
  "timeout_seconds": 600,
  "created_at": 1773400000.0
}
```

字段说明：
- `correlation_id`：全链路追踪 ID，结果必须原样带回。
- `reply_to`：回执主题，接收方将结果发到这里。
- `attachment_urls`：Claim-Check 模式，消息内只带 URL，大文件走文件服务。
- `session_id`（可选）：接收端执行器据此 `--resume` 延续 CodeBuddy 会话上下文；不传则新开会话。
- `timeout_seconds`：接收端执行超时（含硬杀）。

### 2.3 任务结果 `task_result`

执行方发布到 `reply_to` 主题。

```json
{
  "protocol_version": "1.0",
  "type": "task_result",
  "task_id": "uuid4",
  "correlation_id": "uuid4",
  "sender_id": "codebuddy_pc1",
  "status": "success",
  "result": {
    "output_text": "最终回复正文",
    "artifacts": ["http://bus:8000/api/files/def456"],
    "session_id": "7743d617-..."
  },
  "error": null,
  "completed_at": 1773400050.0
}
```

`status` ∈ `success | error | timeout`。
`result.session_id`：CodeBuddy 会话 ID，发送方可在下一轮 `payload.session_id` 中带回以延续多轮上下文。

## 3. 在线状态判定

1. 心跳：`bus/heartbeat/{agent_id}` 每 30s 一次，服务端刷新 `last_seen`。
2. 遗嘱：MQTT 连接时设置 LWT 到 `bus/offline/{agent_id}`，网络断开/进程崩溃时由 Broker 自动发布，服务端立即标记离线。
3. 兑底：`last_seen` 超过 90s 视为离线。

## 4. 文件传输（Claim-Check）

1. 上传：`POST {HTTP_BASE}/api/files/upload`（multipart），返回 `{file_id, url, name, size}`。
2. 消息内只携带 `url`。
3. 下载：`GET {url}`。
4. 所有文件元数据入 SQLite，可在面板追溯。
