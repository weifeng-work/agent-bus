# Agent Bus —— 局域网多智能体协作总线

让同一局域网内的任意智能体（CodeBuddy / OpenCode / WorkBuddy / TRAE…）互相发现、收发任务、共享文件，全程可追溯、可视化。**主机一键起服务，子设备给智能体一句提示词即可加入队伍。**

## 核心模型

```
[ 主机 1 台 ]
   运行 broker(MQTT) + bus_server(HTTP/面板)
   面板首次向导：设置【队伍名】+【加入口令】
   每 3s UDP 广播 beacon（队名/主机 IP/端口）→ 局域网内可被发现
        │
        ▼ UDP beacon 41830 / MQTT 1883 / HTTP 8000
        │
[ 子设备 N 台 ]
   智能体收到一句提示词 → setup_worker_windows.ps1
      → 发现主机 → 输入队伍口令 → 服务端核对并发凭据
      → 保存凭据 → 启动执行器 → 作为工作节点上线
```

- **出站连接模型**：所有节点主动连主机（MQTT/HTTP），无需公网 IP、无需内网穿透、不开入站端口。
- **发现即用**：子设备扫描局域网 beacon 自动发现主机；广播不可达时可用 `--host <IP>` 手动指定。
- **安全**：MQTT 禁匿名 + 每节点独立凭据 + pattern ACL；HTTP Bearer 令牌 + 角色分离（admin/node）。
- **一句口令入队**：用户只需在面板设一个口令，子设备输对口令即自动入队、自动下发凭据。

## 需要什么

| 角色 | 一台 Windows/Linux 机器即可 | 前置 |
|---|---|---|
| **主机** | Python 3.10+；配网卡接收 UDP 广播 | mosquitto 会自动装 |
| **子设备** | Python 3.10+（自动装）；目标机器的 CLI 智能体 | 同局域网 + 队伍口令 |

> Windows 上 Python 可用 winget 自动安装；无需 git（直接下载主分支 zip）。

---

## 一、主机安装（一次）

在某台 Windows/Linux 机器跑一键引导（跨平台，同一脚本）：

```powershell
# Windows 或 Linux 一致：下载后运行
python -c "import urllib.request,sys; urllib.request.urlretrieve('https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_host.py','host.py')"
python host.py
```
```bash
# 或 Bash 一行
curl -fsSL https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_host.py -o host.py && python host.py
```

完成后：
- broker(MQTT) 监听 `0.0.0.0:1883`，bus_server(HTTP+面板) 监听 `0.0.0.0:8000`
- 自动打开面板 `http://<主机IP>:8000/`
- **首次进入面板：设置队伍名 + 加入口令**（口令 4–64 位）→ 之后 beacon 广播自动开始

面板功能：在线名单 / 消息时间线 / 文件列表 / 重置口令 / 移除节点。

> 参考主机的候选 IP：`python -c "from agent_bus import provision; print(provision.get_local_ips())"`

防火墙放行（入站）：`TCP 1883`（MQTT）`TCP 8000`（HTTP）`UDP 41830`（beacon 发现）。

---

## 二、子设备给智能体一句提示词安装

在目标 Windows 电脑上，对着那台电脑的智能体说（示例）：

> “用 https://github.com/weifeng-work/agent-bus 把 agent-bus 作为工作节点装到我这台 Windows。
> 先安装/确认 Python 3.10+，然后运行：
> `irm https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_worker_windows.ps1 | iex`
> 队伍口令是 `1234`。装完后启动一个 codebuddy 执行器，并告诉我分配到的 agent_id。”

智能体就会自动执行 [scripts/setup_worker_windows.ps1](scripts/setup_worker_windows.ps1)：装 Python → 下载项目 → 装依赖 → 发现主机 → 入队 → 启动执行器 → 上线。

如果你想手动在别的 Windows 机器操作，一行命令：

```powershell
irm https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_worker_windows.ps1 | iex
```

脚本会交互引导：发现队伍 → 输入口令 → 拿凭据 → 启动执行器。非交互（自动化作答/脚本/CI）传参：

```powershell
powershell -ExecutionPolicy Bypass -File setup_worker_windows.ps1 `
  -Host 192.168.31.186 -Passphrase "1234" -Executor codebuddy
```

| 参数 | 说明 |
|---|---|
| `-Host` | 主机 IP（省略则 UDP 扫描自动发现） |
| `-Passphrase` | 队伍口令（省略则交互输入） |
| `-Executor` | 启动哪个执行器：`codebuddy` / `opencode` / `workbuddy`（默认 codebuddy） |
| `-Name` | 设备显示名（默认 `执行器@主机名`） |

脚本依次完成：下载项目到 `C:\agent-bus` → `pip install` 依赖 → `scripts/join_team.py` 入队 → 生成 `start_executor.bat` → 注册“登录时自启”计划任务并立即启动执行器。

> 目标机器的 CLI 智能体（CodeBuddy/OpenCode）需已安装并登录，执行器才能接任务。
> Linux 子设备见 [scripts/setup_linux.sh](scripts/setup_linux.sh)（角色：Skill 主动协作者 / Worker 被召唤执行）。

---

## 三、用起来（在主机面板 / 任意节点）

1. **查看在线名单**：面板 `http://<主机IP>:8000/`，所有设备主动连主机，可见 `● 在线 / ○ 离线`。
2. **给某节点发任务**（在 Skill 模式节点或主机上）：
   ```bash
   # 先登录/认证（Skill 节点）
   python skill/cli.py agents
   python skill/cli.py send --to codebuddy_pc2 --text "帮我在对方机器上执行 hostname 并汇报" --wait 300
   ```
   → 对方执行器 headless 拉起本机 CodeBuddy 执行 → 结果 + session_id 自动回传。
3. **延续对话**：回传的 `session_id` 可带 `--session <sid>` 继续同一上下文。

---

## 目录

| 路径 | 说明 |
|---|---|
| `scripts/setup_host.py` | 主机一键引导（装 mosquitto + 起 broker/server + 打开面板） |
| `scripts/join_team.py` | 子设备入队（发现队伍→口令→凭据→上线；支持 `--passphrase` 非交互） |
| `scripts/setup_worker_windows.ps1` | 子设备 Windows 一键引导（装 Python/依赖/入队/启执行器） |
| `scripts/setup_linux.sh` | Linux 子设备接入（Skill 主动 / Worker 被召） |
| `scripts/add_node.py` / `broker_ctl.py` | 手动开凭据 / 用户态 broker 进程管理 |
| `agent_bus/` | Python SDK（MQTT 客户端、provision 凭据、discovery 发现协议、files） |
| `server/bus_server.py` | 服务端（MQTT 桥 + SQLite + HTTP API + Web 面板） |
| `executor/` | 各 CLI 执行器：codebuddy / opencode / workbuddy / interactive |
| `skill/` | 通信 Skill：`SKILL.md` + `cli.py`（CLI）+ `mcp_server.py`（MCP 工具） |
| `docs/protocol.md` | 通信契约 + 队伍发现协议（UDP beacon v1.2） |

## 安全模型（简）

- MQTT：`allow_anonymous false` + 每节点独立账号（PBKDF2）+ pattern ACL；凭据按角色（admin/bridge/node）区分。
- HTTP：所有 API 需 Bearer token；仅 `/api/health` `/api/team/status` 匿名；`/api/join` 为唯一口令端。
- **口令即信任**：子设备输对口令即自动入队。请只在可信局域网使用；跨网/公网部署需 TLS（见 `docs/broker_setup.md`）。

## 已知约束

- 子设备节点需保证局域网可达主机（同网段或可路由）。
- Windows GUI 执行器（WorkBuddy）须跑在用户会话、不能锁屏；CLI 执行器无此限制。
- 广播发现对跨 VLAN/AP 隔离可能失效，此时用 `--host <IP>` 手动加入。