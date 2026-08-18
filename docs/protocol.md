# Agent Bus 通信契约 v1.2

所有跨节点消息均为 UTF-8 JSON，通过 MQTT 传递（QoS 1）。

> 版本记录：v1.1 增 `task_progress` / `session_input`；v1.2 增 `git_event`（中心仓协作，§2.7）。

## 1. MQTT 主题设计

| 主题 | 方向 | 用途 |
|---|---|---|
| `agent/{agent_id}/inbox` | 发往该 Agent | 任务请求 / 任务结果的投递箱 |
| `bus/register` | Agent → 总线 | 注册声明（保留消息） |
| `bus/heartbeat/{agent_id}` | Agent → 总线 | 心跳（默认 30s 一次） |
| `bus/offline/{agent_id}` | Broker → 总线 | MQTT 遗嘱（LWT），异常掉线时自动发布 |
| `bus/git_event` | Agent → 总线 | Git 中心仓协调事件广播（§2.7） |

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

### 2.4 任务进度 `task_progress`（v1.1 新增）

交互式执行器在任务执行期间发布到 `reply_to` 主题（直播流，QoS 1）。
发送方以 `correlation_id` 关联，`seq` 单调递增。

```json
{
  "protocol_version": "1.0",
  "type": "task_progress",
  "task_id": "uuid4",
  "correlation_id": "uuid4",
  "sender_id": "codebuddy_tui1",
  "target_id": "agent_alpha",
  "seq": 3,
  "phase": "running",
  "result": {
    "output_text": "重建屏幕尾部文本（节流快照）",
    "session_id": "agentbus_codebuddy_tui1"
  },
  "ts": 1773400020.0
}
```

`phase` ∈ `started | running | input_needed | done`。
`input_needed` 表示 TUI 弹出了需要人工决策的确认框（y/n 类），发送方可人工介入（见 2.5）或等待。
进度流是尽力而为的直播，不保证完整性；最终结论一律以 `task_result` 为准。

### 2.5 会话输入 `session_input`（v1.1 新增）

向执行器上活跃的交互会话注入输入（人工干预 / 中途追问）。发布到 `agent/{executor_id}/inbox`。

```json
{
  "protocol_version": "1.0",
  "type": "session_input",
  "sender_id": "agent_alpha",
  "target_id": "codebuddy_tui1",
  "session_id": "agentbus_codebuddy_tui1",
  "text": "补充信息：路径改为 /data 下",
  "special": null,
  "ts": 1773400030.0
}
```

`text`：字面文本（paste-buffer 注入）；`special`：tmux 键名（`Enter` / `C-c` / `Esc` 等），两者可同时给。

### 2.6 任务取消 `task_cancel`

请求执行方取消指定任务。发布到 `agent/{executor_id}/inbox`。
执行方应优雅中断（交互式执行器: C-c → 宽限 → 会话重置），并回 `task_result` 且 `status="cancelled"`。

```json
{
  "protocol_version": "1.0",
  "type": "task_cancel",
  "sender_id": "agent_alpha",
  "target_id": "codebuddy_tui1",
  "task_id": "uuid4",
  "reason": "需求变更",
  "ts": 1773400040.0
}
```

`task_result.status` 枚举相应扩展为 `success | error | timeout | cancelled`。

### 2.7 Git 事件 `git_event`（v1.2 新增，中心仓协作）

Git 中心仓的协调语义广播（设计见 docs/git_central_repo.md §6）。发布到 `bus/git_event`
（bus_server 经既有 `bus/#` 订阅全量入库，面板时间线可检索，服务端无需改动）。
审查/合并的**触发**不走本报文，仍用定向 `task_request`（§2.2）；本报文只做留痕与通报。

```json
{
  "protocol_version": "1.0",
  "type": "git_event",
  "sender_id": "codebuddy_pc1",
  "payload": {
    "event": "pushed",
    "repo": "agent-bus",
    "branch": "task/8a3f2b1c",
    "commit": "edd6948",
    "note": "可选说明"
  },
  "ts": 1773400050.0
}
```

`payload.event` 枚举：`pushed`（任务分支已推送）｜ `review_request`（请求审查）｜
`merged`（已合入 main）｜ `pull_advisory`（建议各节点同步 main）。
`commit`、`note` 可选。

## 3. 在线状态判定

1. 心跳：`bus/heartbeat/{agent_id}` 每 30s 一次，服务端刷新 `last_seen`。
2. 遗嘱：MQTT 连接时设置 LWT 到 `bus/offline/{agent_id}`，网络断开/进程崩溃时由 Broker 自动发布，服务端立即标记离线。
3. 兑底：`last_seen` 超过 90s 视为离线。

## 4. 文件传输（Claim-Check）

1. 上传：`POST {HTTP_BASE}/api/files/upload`（multipart），返回 `{file_id, url, name, size}`。
2. 消息内只携带 `url`。
3. 下载：`GET {url}`。
4. 所有文件元数据入 SQLite，可在面板追溯。

## 5. 队伍发现协议（UDP beacon，v1.2 新增）

子设备加入队伍前的主机发现，走 UDP 广播而非 MQTT（加入前没有凭据）。

### 5.1 beacon 报文

主机侧（bus_server）队伍初始化后每 3 秒向 `255.255.255.255:41830` 广播，
并按自报候选 IP 补发各网段定向广播（`a.b.c.255:41830`，防代理 TUN 劫持默认路由）：

```json
{
  "proto": "agent-bus", "ver": 1,
  "team_id": "160086f6d220", "team_name": "Alpha 小队",
  "host_name": "DESKTOP-ABC",
  "ips": ["192.168.31.186", "192.168.176.1"],
  "mqtt_port": 1883, "http_port": 8000
}
```

- `ips`：主机全部私网候选地址（多网卡/虚拟网卡/代理 TUN 场景无法给出唯一答案）
- beacon 不含任何凭据；入队即匿名登记于 HTTP `POST /api/join`（v2 匿名化后无口令核对）
- 选广播而非组播：零配置；代价是 AP 隔离下不可达——保留 `--host` 手动回退

### 5.2 加入流程（发现 ≠ 连通）

1. 子设备 `scan_teams()` 绑定 41830 收集 beacon（按 team_id 去重，`host_ips` 取并集）
2. **连通性自检**：逐候选探测 `http://{ip}:{http_port}/api/health`，选第一个可达 IP
3. `POST /api/join`（匿名登记 agent_id/设备名）→ 返回 broker 连接信息
4. 连接配置落 `~/.config/agent-bus/bus.env`，立即连 MQTT 注册验证上线

安全：信任边界为局域网——`/api/join` 匿名放行，任何可访问 8000/1883 端口的设备
均可入队并读取全部消息；公网部署需恢复认证或加 TLS（见 `docs/broker_setup.md`）。
