# Agent Bus —— 局域网多智能体协作总线

让同一局域网内的任意智能体（CodeBuddy / OpenCode / WorkBuddy / TRAE…）互相发现、收发任务、共享文件，全程可追溯、可视化。**主机一键起服务，受控机给智能体一句提示词即可加入队伍。**

**工作流程**：
```
主机 1 台：运行 MQTT broker + HTTP 面板 + 服务端
   每 3s UDP 广播 beacon（队伍名/主机 IP/端口）→ 局域网内可被发现
        │
        ▼ UDP beacon 41830 / MQTT 1883 / HTTP 8000
        │
受控机 N 台：
   智能体收到一句提示词 → 自动下载安装脚本
     → 发现主机 → 入队 → 注册 NSSM 服务 + 启动托盘 UI
     → 执行器上线 → 状态灯变绿
```

---

## 目录

- [一、主机安装（一次）](#一主机安装一次)
- [二、受控机安装（每台电脑各跑一次）](#二受控机安装每台电脑各跑一次)
- [三、智能体接入方式（MCP / CLI）](#三智能体接入方式mcp--cli)
- [四、远程 Shell（受控机完全 Shell 权限）](#四远程-shell受控机完全-shell-权限)
- [五、项目结构](#五项目结构)
- [六、安全模型](#六安全模型简化版)
- [七、已知约束](#七已知约束)

---

## 一、主机安装（一次）

在一台 Windows/Linux 机器上跑一键引导：

```powershell
# Windows 或 Linux
python -c "import urllib.request,sys; urllib.request.urlretrieve('https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_host.py','host.py')"
python host.py
```
```bash
# 或 Bash 一行
curl -fsSL https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_host.py -o host.py && python host.py
```

完成后：
- MQTT broker 监听 `0.0.0.0:1883`，HTTP 面板监听 `0.0.0.0:8000`
- 自动打开面板 `http://<主机IP>:8000/`
- **首次进入面板：设置队伍名** → 之后 beacon 广播自动开始

面板功能：在线名单 / 消息时间线 / 文件列表 / 移除节点。

防火墙放行（入站）：`TCP 1883`（MQTT）`TCP 8000`（HTTP）`UDP 41830`（beacon 发现）。

---

## 二、受控机安装（每台电脑各跑一次）

### 方式 A：让智能体自动安装（推荐）

在目标 Windows 电脑上，对着那台电脑的智能体说：

> 用 https://github.com/weifeng-work/agent-bus 把 agent-bus 作为受控节点装到我这台 Windows。
> 项目 README 里有完整安装说明：需要 Python 3.10+（没有就用 winget 装 Python.Python.3.12），运行 scripts/setup_worker_windows.ps1（会自动装依赖、入队、安装通信节点 NSSM 服务并启动托盘）。
> 装完任务栏会出现 Agent Bus 托盘图标（绿色=已连接），告诉我分配到的 agent_id。

智能体读 README 后会自动执行：
1. 检测/安装 Python 3.10+
2. 下载项目代码
3. 安装 Python 依赖
4. 发现主机并匿名入队
5. 清理旧版计划任务
6. 注册 NSSM 服务 `AgentBusCore`（自动启动，崩溃 5 秒重启）
7. 创建开始菜单快捷方式
8. 启动服务 + 托盘 UI

**整个过程无需人工干预，无需输入任何密码。**

### 方式 B：手动安装

```powershell
# 下载安装脚本
irm https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_worker_windows.ps1 -o C:\setup_worker.ps1

# 以管理员身份运行（需要管理员权限注册 NSSM 服务）
powershell -ExecutionPolicy Bypass -File C:\setup_worker.ps1 -Queue myteam -EnableShellControl
```

| 参数 | 说明 |
|---|---|
| `-Host` | 主机 IP（省略则 UDP 扫描自动发现） |
| `-Executor` | 启动哪个执行器：`codebuddy` / `opencode` / `workbuddy`（默认 codebuddy） |
| `-Name` | 设备显示名（默认 `执行器@主机名`） |
| `-Queue` | 队列标识（可选，用于区分不同队伍） |
| `-EnableShellControl` | 安装即开启 shell 受控能力（默认关） |

> 目标机器的 CLI 智能体（CodeBuddy/OpenCode）需已安装并登录，执行器才能接任务。
> Linux 受控机见 [scripts/setup_linux.sh](scripts/setup_linux.sh)。

### 安装后验证

- 任务栏托盘区出现 Agent Bus 图标（绿色=已连接）
- 服务管理器（`services.msc`）中可见 `AgentBusCore` 服务，状态"运行中"，启动类型"自动"
- 开始菜单 → Agent Bus → Agent Bus Tray（恢复托盘用）
- 主机面板 `http://<主机IP>:8000/` 可见新节点上线

---

## 三、智能体接入方式（MCP / CLI）

智能体安装 Agent Bus 后，还需要配置接入方式才能收发消息。有两种方式：

### 方式 A：MCP Server（推荐，适用于支持 MCP 的智能体客户端）

在智能体客户端的 MCP 配置中添加（以 CodeBuddy 为例，写入 `mcp_servers.json`）：

```json
{
  "mcpServers": {
    "agent-bus": {
      "command": "python",
      "args": ["<安装目录>/skill/mcp_server.py"],
      "env": {
        "BUS_AGENT_ID": "<智能体ID>"
      }
    }
  }
}
```

**说明**：
- 主控机 IP 和端口不用填，`mcp_server.py` 会自动读取安装时生成的 `bus.env`
- 安装目录默认 `%LOCALAPPDATA%\agent-bus`（Windows）或 `~/.local/share/agent-bus`（Linux）
- `<智能体ID>` 取一个唯一标识，如 `agent_pc1`，每台机器不同

配置完成后，智能体就能通过 MCP 工具使用总线能力：
- `list_online_agents` — 查看当前在线智能体
- `send_task` — 给目标智能体发任务并等待结果
- `check_inbox` — 拉取自己的收件箱
- `reply_task` — 回传任务结果
- `upload_file` / `download_file` — 文件上传下载

### 方式 B：CLI 命令行（适用于不支持 MCP 的智能体，或脚本调用）

```bash
# 注册自己（声明存在和能力）
python skill/cli.py register --name "分析Agent" --caps analysis

# 查看在线智能体
python skill/cli.py agents

# 发任务
python skill/cli.py send --to agent_pc2 --text "分析数据" --wait 300

# 拉取收件箱
python skill/cli.py check --timeout 5

# 回传结果
python skill/cli.py reply --req-file req.json --text "完成"
```

两种方式功能完全等价，都包含注册、发任务、收消息、回传结果、文件上传下载。

---

## 四、远程 Shell（受控机完全 Shell 权限）

已开启 shell 受控能力（`-EnableShellControl`）的受控节点，主控机可直接执行任意命令（SSH 权限级别，无需 SSH）：

```bash
# 主控机：向受控节点发命令并等待回执
python executor/comm_node.py --role hub --shell-exec \
  --target node-<受控机agent_id> --cmd "hostname" --timeout 30

# 例：跨机文件操作
python executor/comm_node.py --role hub --shell-exec \
  --target node-host-4f2a --cmd "dir C:\\Users" --timeout 60
```

- 受控节点 `agent_id` 见其 `~/.config/agent-bus/device.json`（节点身份为 `node-<agent_id>`）
- 每次执行三处留存：受控机托盘通知气泡 + 本地 `control.log` + bus_server 全量审计
- 未开启 shell 受控能力 → 拒绝；关闭方法：托盘菜单取消勾选「shell 受控能力」

---

## 五、项目结构

```
agent-bus/
├── scripts/                        # 安装与运维脚本
│   ├── setup_host.py               # 主机一键引导（装 mosquitto + 起 bus_server + 面板）
│   ├── setup_worker_windows.ps1    # 受控机 Windows 一键安装（NSSM 服务 + 托盘）
│   ├── setup_tray.ps1              # 通信节点安装（NSSM 注册 + 开始菜单快捷方式 + 计划任务）
│   ├── agent_service.py            # NSSM 服务包装入口 + 管理工具（install/remove/start/stop）
│   ├── remote_update_worker.ps1    # 远程更新脚本（服务形态：停服务 → 替换代码 → 起服务）
│   ├── join_team.py                # 子设备入队（UDP 发现主机 → 匿名登记 → 上线）
│   ├── watchdog.py                 # 旧版计划任务兜底（兼容旧部署）
│   └── setup_linux.sh              # Linux 受控机接入
│
├── executor/                       # 核心执行器
│   ├── core_node.py                # Layer 1 核心控制节点（无头，MQTT + shell_exec + 执行器监督 + 自愈 watchdog）
│   ├── tray_app.py                 # Layer 2+ 托盘 UI（纯可视化，读 state.json 显示状态）
│   ├── comm_node.py                # 兼容包装（委托 core_node.py）
│   ├── codebuddy_executor.py       # CodeBuddy CLI 执行器
│   ├── opencode_executor.py        # OpenCode CLI 执行器
│   ├── workbuddy_executor.py       # WorkBuddy GUI 执行器
│   ├── interactive_executor.py     # 交互式执行器（psmux 可视窗口）
│   ├── mux_transport.py            # 多路复用传输层
│   └── _tray.py                    # 旧版托盘 UI（已废弃，保留兼容）
│
├── agent_bus/                      # Python SDK
│   ├── client.py                   # MQTT 客户端（注册/心跳/收发消息/状态上报）
│   ├── config.py                   # 集中配置（环境变量：BUS_BROKER_HOST 等）
│   ├── crypto.py                   # 身份检查（is_hub_message / is_control_op）
│   ├── state_machine.py            # 状态机（state.json 原子读写，active/disabled）
│   ├── discovery.py                # UDP beacon 发现协议
│   ├── schema.py                   # 消息报文规范
│   ├── files.py                    # 文件上传下载（Claim-Check）
│   └── provision.py                # 网络工具（获取本地 IP 等）
│
├── server/                         # 服务端
│   ├── bus_server.py               # 中间架构服务端（MQTT 桥 + SQLite + HTTP API + 面板）
│   └── static/index.html           # Web 控制面板
│
├── skill/                          # 智能体接入层
│   ├── SKILL.md                    # 技能说明书（智能体读取后知道如何接入）
│   ├── mcp_server.py               # MCP Server（暴露 bus_* 工具给 MCP 客户端）
│   └── cli.py                      # CLI 命令行工具（供不支持 MCP 的智能体使用）
│
├── tests/                          # 测试
│   ├── test_phase5_state_machine.py    # 状态机切换测试
│   ├── test_phase5_crash_and_hang.py   # 崩溃恢复 + 心跳测试
│   ├── test_phase5_dual_launch.py      # 双重拉起防护测试
│   ├── test_phase5_concurrent_write.py # 状态文件并发写测试
│   ├── _test_crypto.py                 # 身份检查单测
│   ├── _neg_shell.py                   # 负向测试（非 hub 拒绝）
│   └── _smoke_comm_node.py             # M1 冒烟测试
│
├── docs/                           # 设计文档
│   ├── architecture.md             # 架构设计（v1.0，Phase 1-5 重构完成）
│   ├── protocol.md                 # 通信契约 + 队伍发现协议
│   ├── git_central_repo.md         # Git 中心仓协作方案
│   ├── broker_setup.md             # MQTT Broker 搭建说明
│   └── backing_agent_probe.md      # 后备 Agent 探测方案
│
└── data/                           # 运行时数据（自动生成）
    ├── runtime/                    # 运行时状态（state.json、心跳文件等）
    │   ├── _dl/nssm.exe            # NSSM 2.24（随包分发）
    │   └── ...
    ├── bus.db                      # SQLite 消息库
    └── files/                      # 上传文件存储
```

---

## 六、安全模型（简化版）

本设计适用于**高安全局域网 + 所有设备可信**的环境：

- **信任边界 = 高安全局域网**：所有设备均为可信设备，无外部攻击者能接入网络
- **broker 匿名**：`allow_anonymous true`，`/api/join` 免口令登记入队，面板与 API 全匿名可读
- **控制消息身份检查**：控制消息（shell_exec 等）仅接受来自 `hub-*` 身份的 sender，在 worker 侧检查
- **队列标识**：安装时通过 `-Queue` 参数指定队列归属（纯文本，用于区分不同队伍）
- **shell_control 本地开关**：受控机本地确认是否开放 shell 能力（默认关），开启后状态托盘常驻可见
- **三处审计**：每次控制操作 → 托盘通知气泡 + 本地 control.log + bus_server 全量入库

---

## 七、已知约束

- 受控机节点需保证局域网可达主机（同网段或可路由）
- 安装脚本需要管理员权限（注册 NSSM 服务需要）
- Windows GUI 执行器（WorkBuddy）须跑在用户会话、不能锁屏；CLI 执行器无此限制
- 广播发现对跨 VLAN/AP 隔离可能失效，此时用 `--host <IP>` 手动加入
- 目前仅 Windows 受控机有完整 NSSM 服务化支持；Linux 受控机沿用旧版 setup_linux.sh