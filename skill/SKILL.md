---
name: agent-bus
description: 跨机器多智能体协作通信。当用户要求给其他机器/其他智能体发任务、查询在线智能体名单、跨机传文件、加入 Agent Bus 总线协作时使用。触发词：智能体协作、跨机器、发任务、在线名单、agent-bus、总线。
---

# Skill: agent-bus 跨网络智能体协作

## 描述

让智能体接入 Agent Bus 中间架构：向总线注册声明自身存在、发现其他在线智能体、互发任务/文件、回传结果。适用于多机器（局域网/广域网）多智能体协作场景。

## 环境准备

环境变量由安装脚本写入 `~/.config/agent-bus/bus.env`，调用前先加载：

```bash
source ~/.config/agent-bus/bus.env
```

内容（安装时自动生成）：

```bash
BUS_BROKER_HOST=<服务器IP>         # MQTT Broker 所在机器
BUS_BROKER_PORT=1883
BUS_HTTP_BASE=http://<服务器IP>:8000  # 中间架构 HTTP（文件服务/查询 API/面板）
BUS_AGENT_ID=<本机节点ID>           # 本节点唯一 ID
```

CLI 入口（安装脚本已部署）：
`python3 ~/.codebuddy/skills/agent-bus/bus/cli.py <子命令>`

（仓库内开发调试时等价于 `python skill/cli.py`；也可挂载 MCP Server `bus/mcp_server.py`，工具名前缀 `bus_*`。）

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

1. **身份核对**：每条任务都带【任务信封】（发起方 ID / 执行方 ID / correlation_id）。执行前核对执行方 ID 确实是自己；给他人发消息时务必指定准确的 `--to <agent_id>`，防止多智能体场景下误发。
2. **注册**：开始参与协作前先 `register`，声明 ID、名称与能力。
3. **发现**：发任务前用 `agents` 确认对方在线；只给 `online: true` 的节点发。
4. **任务**：`send` 返回的 JSON 中 `status` 为 `success/error/timeout`；`result.session_id` 可在下次 `--session` 带入以延续对方会话上下文。
5. **回传**：若你是被动收到任务的节点（`check` 拿到 `task_request`），完成处理后**必须**用 `reply` 回传，把最终结论放在 `--text`。
6. **文件**：消息内只传 URL（Claim-Check）；发大文件先 `upload` 再把 URL 写入任务文本。
7. **追溯**：所有来往消息均可在面板 `http://<服务器>:8000/` 查询，不要在消息中携带密钥等敏感信息。

## 示例工作流

```bash
source ~/.config/agent-bus/bus.env
CLI=~/.codebuddy/skills/agent-bus/bus/cli.py

# 1. 注册自己
python3 $CLI register --name "数据分析Agent" --caps analysis,python

# 2. 找一个代码智能体帮忙
python3 $CLI agents

# 3. 发任务并等结果（返回 JSON 含 result.output_text 与 result.session_id）
python3 $CLI send --to codebuddy_pc1 --text "分析 ./data.csv 并给出结论" --file data.csv --wait 600

# 4. 延续同一会话继续追问
python3 $CLI send --to codebuddy_pc1 --text "把上一轮结论整理成表格" --session <上一步的 session_id>
```
