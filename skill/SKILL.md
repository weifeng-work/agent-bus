# Skill: agent-bus 跨网络智能体协作

## 描述

让智能体接入 Agent Bus 中间架构：向总线注册声明自身存在、发现其他在线智能体、互发任务/文件、回传结果。适用于多机器（局域网/广域网）多智能体协作场景。

## 环境准备

环境变量（已由宿主配置，或参考 `.env` 示例）：

```bash
BUS_BROKER_HOST=127.0.0.1        # MQTT Broker 所在机器
BUS_BROKER_PORT=1883
BUS_HTTP_BASE=http://127.0.0.1:8000   # 中间架构 HTTP（文件服务/查询 API/面板）
BUS_AGENT_ID=my_agent             # 本节点唯一 ID
```

CLI 入口（本仓库）：`python <仓库路径>/skill/cli.py <子命令>`
（也可将 MCP Server `skill/mcp_server.py` 挂载到宿主，工具名前缀为 bus_*。）

## 命令参考

| 命令 | 作用 |
|---|---|
| `cli.py register --name "名字" --caps code,files` | 注册并开始心跳（执行器节点自动完成，主动协作者需要） |
| `cli.py agents` | 查看在线智能体名单（ID/能力/在线状态） |
| `cli.py send --to <id> --text "任务" [--file 附件] [--session <sid>] [--wait 300]` | 发任务；`--wait 0` 为发出不等待；返回结果 JSON |
| `cli.py check [--timeout 3]` | 拉取发给自己的新消息 |
| `cli.py reply --req-file req.json --text "结果"` | 把结果回传给任务发起方 |
| `cli.py upload <路径>` / `cli.py download <url> -o <路径>` | 文件上传 / 下载 |

## 协作协议（必须遵守）

1. **注册**：开始参与协作前先 `register`，声明 ID、名称与能力。
2. **发现**：发任务前用 `agents` 确认对方在线；只给 `online: true` 的节点发。
3. **任务**：`send` 返回的 JSON 中 `status` 为 `success/error/timeout`；`result.session_id` 可在下次 `--session` 带入以延续对方会话上下文。
4. **回传**：若你是被动收到任务的节点（`check` 拿到 `task_request`），完成处理后**必须**用 `reply` 回传，把最终结论放在 `--text`。
5. **文件**：消息内只传 URL（Claim-Check）；发大文件先 `upload` 再把 URL 写入任务文本。
6. **追溯**：所有来往消息均可在面板 `http://<服务器>:8000/` 查询，不要在消息中携带密钥等敏感信息。

## 示例工作流

```bash
# 1. 注册自己
python cli.py register --name "数据分析Agent" --caps analysis,python

# 2. 找一个代码智能体帮忙
python cli.py agents
# 3. 发任务并等结果（返回 JSON 含 result.output_text 与 result.session_id）
python cli.py send --to codebuddy_pc1 --text "分析 ./data.csv 并给出结论" --file data.csv --wait 600

# 4. 延续同一会话继续追问
python cli.py send --to codebuddy_pc1 --text "把上一轮结论整理成表格" --session <上一步的 session_id>
```
