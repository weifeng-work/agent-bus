# Agent Bus —— 跨网络多智能体协作系统（阶段一）

局域网优先（可平滑迁移公网）的多智能体通信中间架构：任何机器上的智能体（首个适配：**CodeBuddy CLI**）通过统一消息总线互发任务/文件并回传结果，全程可追溯、状态可视化。

## 架构

```
[ 机器 A: Agent + Skill/CLI ] ──┐                         ┌── [ 机器 B: CodeBuddy 执行器 ]
                                ▼                         ▲        │ subprocess(-p 模式)
                    ┌──────────────────────────────┐      │        ▼
                    │  中间架构（局域网一台服务器）  │──────┘   [ CodeBuddy CLI 进程 ]
                    │  MQTT Broker (Mosquitto)      │  任务注入   stdout(JSON) 回传
                    │  bus_server: 注册/心跳/遗嘱    │
                    │  SQLite 消息追溯 + Web 面板    │
                    │  HTTP 文件服务 (Claim-Check)   │
                    └──────────────────────────────┘
```

- **MQTT 出站连接模型**：各节点主动连 Broker，无需内网穿透；迁公网只改地址。
- **Claim-Check**：消息内只传文件 URL，大文件走 HTTP 文件服务。
- **CodeBuddy 会话延续**：执行器统一在固定工作目录拉起 `codebuddy -p --output-format json -y`，解析事件数组末尾的 `type:"result"` 元素提取 `result`/`session_id`；发送方在 `payload.session_id` 带回即可 `--resume` 多轮上下文。

## 目录

| 路径 | 说明 |
|---|---|
| `docs/protocol.md` | 通信契约（三类报文 + 主题设计 + 状态判定） |
| `docs/broker_setup.md` | Mosquitto 安装配置（Windows/Linux/Docker） |
| `agent_bus/` | Python SDK（MQTT 客户端：注册/心跳/遗嘱/收发/等待结果） |
| `server/bus_server.py` | 服务端单进程：MQTT 桥 + SQLite + API + 文件服务 |
| `server/static/index.html` | 可视面板（在线名单 / 消息时间线 / 文件列表） |
| `executor/codebuddy_executor.py` | CodeBuddy 节点执行器（`--mock` 联调模式） |
| `skill/` | 通信 Skill：`SKILL.md`（提示词）+ `cli.py`（Bash 调用）+ `mcp_server.py`（MCP 工具） |
| `scripts/setup_debian.sh` | Debian/Linux 节点一键安装脚本 |
| `tests/test_e2e.py` | 本机三进程端到端测试 |

## 快速开始（单机验证）

前置：Python 3.10+，Mosquitto 运行于 `127.0.0.1:1883`（回环匿名即可，见 `docs/broker_setup.md`）。

```powershell
pip install -r requirements.txt

# 终端1: 服务端（面板 http://127.0.0.1:8000/）
python server/bus_server.py

# 终端2: CodeBuddy 执行器节点（--mock 可先不调真实 CLI 联调）
python executor/codebuddy_executor.py --agent-id codebuddy_pc1 --name "CodeBuddy@PC1"

# 终端3: 任意一端发任务（CLI 即"Skill"的命令面）
$env:BUS_HTTP_BASE="http://127.0.0.1:8000"
python skill/cli.py --id sender_test send --to codebuddy_pc1 --text "你好，介绍你自己" --wait 300
```

返回 JSON 中 `result.output_text` 为 CodeBuddy 最终回复，`result.session_id` 可用于下一轮 `--session <sid>` 延续上下文。

## 局域网部署（多机）

1. 服务端机器：开放 1883（MQTT）与 8000（HTTP）防火墙；Mosquitto 配置 `listener 1883 0.0.0.0` + `allow_anonymous true`（需管理员，见 `docs/broker_setup.md`）。
2. 各节点环境变量：
   ```bash
   BUS_BROKER_HOST=<服务器IP>  BUS_HTTP_BASE=http://<服务器IP>:8000
   ```
3. 每台机器跑一个执行器（`--agent-id` 全网唯一）。

### Debian/Linux 节点一键接入

```bash
curl -fsSL https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_debian.sh -o setup.sh
bash setup.sh <服务器IP>
```

或直接对那台机器上的 CodeBuddy 说："从 https://github.com/weifeng-work/agent-bus 获取项目，运行 scripts/setup_debian.sh <服务器IP> 完成接入"。

## 给智能体安装通信 Skill

- **CLI 方式**（任何有 Bash 的 Agent）：把 `skill/SKILL.md` 内容注入其系统提示词/规则文件，命令参考见 SKILL.md。
- **MCP 方式**：把 `skill/mcp_server.py` 注册为 MCP Server（配置示例见该文件头部注释），提供 `list_online_agents / send_task / check_inbox / reply_task / upload_file / download_file` 工具。

## 阶段二路线（未实现）

- 常驻管道执行器（stdin/stdout + NDJSON 分帧、流式回传）
- 附件产物自动上传（artifacts 回传）、PTY 代理交互式 Agent
- macOS / 移动端适配、公网部署（TLS + 鉴权）

## 已验证

- 本机 e2e 测试 10/10（任务闭环、附件 Claim-Check、在线名单、消息追溯）
- 真实 CodeBuddy 链路：任务下发 → headless 执行 → stdout 解析 → 结果回传
- 跨进程会话延续：`--resume` 双轮问答上下文正确（暗号实验）
