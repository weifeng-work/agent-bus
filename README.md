# Agent Bus —— 局域网多智能体协作总线

让同一局域网内的任意智能体（CodeBuddy / OpenCode / WorkBuddy / TRAE…）互相发现、收发任务、共享文件，全程可追溯、可视化。**主机一键起服务，子设备给智能体一句提示词即可加入队伍。**

## 核心模型

```
[ 主机 1 台 ]
   运行 broker(MQTT) + bus_server(HTTP/面板)
   面板首次向导：设置【队伍名】（一次）
   每 3s UDP 广播 beacon（队名/主机 IP/端口）→ 局域网内可被发现
        │
        ▼ UDP beacon 41830 / MQTT 1883 / HTTP 8000
        │
[ 子设备 N 台 ]
   智能体收到一句提示词 → setup_worker_windows.ps1
      → 发现主机（UDP beacon）→ 匿名登记入队（无口令/凭据）
      → 安装通信节点（托盘壳，三层自愈）→ 启动执行器 → 上线
```

- **出站连接模型**：所有节点主动连主机（MQTT/HTTP），无需公网 IP、无需内网穿透、不开入站端口。
- **发现即用**：子设备扫描局域网 beacon 自动发现主机；广播不可达时可用 `--host <IP>` 手动指定。
- **安全**：局域网可信边界——broker `allow_anonymous true`，HTTP 面板/API 匿名可读，入队免口令；**控制面独立配对**（一次性安装码 + 本地派生密钥，见安全模型）。
- **一键入队**：子设备发现队伍即自动入队，无需口令/凭据（信任 = 局域网可达）。
- **受控机免维护**：通信节点三层自愈（托盘壳秒级拉起执行器 + OS 计划任务分钟级兜底），用户只需不睡眠、不锁屏。

## 需要什么

| 角色 | 一台 Windows/Linux 机器即可 | 前置 |
|---|---|---|
| **主机** | Python 3.10+；配网卡接收 UDP 广播 | mosquitto 会自动装 |
| **子设备** | Python 3.10+（自动装）；目标机器的 CLI 智能体 | 同局域网可达主机 |

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
- **首次进入面板：设置队伍名** → 之后 beacon 广播自动开始

面板功能：在线名单 / 消息时间线 / 文件列表 / 移除节点。

> 参考主机的候选 IP：`python -c "from agent_bus import provision; print(provision.get_local_ips())"`

防火墙放行（入站）：`TCP 1883`（MQTT）`TCP 8000`（HTTP）`UDP 41830`（beacon 发现）。

---

## 二、子设备给智能体一句提示词安装（受控节点 = 通信节点 + 执行器）

**第 0 步（主控机，一次性）**：生成控制面配对码（15 分钟有效、一次性）：

```bash
curl -X POST http://127.0.0.1:8000/api/control/codes   # 在主机本机执行
# 返回 {"ok":true,"code":"SK2ZEC5M",...} —— 记下 code，15 分钟内用
```

在目标 Windows 电脑上，对着那台电脑的智能体说（示例，`<配对码>` 换成上一步的 code）：

> “用 https://github.com/weifeng-work/agent-bus 把 agent-bus 作为受控节点装到我这台 Windows。
> 先安装/确认 Python 3.10+（没有则用 winget 装 Python.Python.3.12），然后：
> `irm https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_worker_windows.ps1 -o C:\setup_worker.ps1`
> 再运行 `powershell -ExecutionPolicy Bypass -File C:\setup_worker.ps1 -PairCode <配对码> -EnableShellControl`。
> 装完后任务栏会出现 Agent Bus 托盘图标（绿色=已连接），告诉我分配到的 agent_id。”

智能体就会自动执行 [scripts/setup_worker_windows.ps1](scripts/setup_worker_windows.ps1)：装 Python → 下载项目 → 装依赖 → 发现主机 → 入队 → 安装通信节点（托盘壳）→ 配对 → 启动执行器 → 上线。

如果你想手动在别的 Windows 机器操作：

```powershell
irm https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_worker_windows.ps1 -o C:\setup_worker.ps1
powershell -ExecutionPolicy Bypass -File C:\setup_worker.ps1 -PairCode SK2ZEC5M -EnableShellControl
```

| 参数 | 说明 |
|---|---|
| `-Host` | 主机 IP（省略则 UDP 扫描自动发现） |
| `-Executor` | 启动哪个执行器：`codebuddy` / `opencode` / `workbuddy`（默认 codebuddy） |
| `-Name` | 设备显示名（默认 `执行器@主机名`） |
| `-PairCode` | 控制面配对码（主控机 `curl /api/control/codes` 生成；省略则 shell 控制不可用） |
| `-EnableShellControl` | 安装即开启 shell 受控能力（默认关；开启后免二次确认，状态托盘常驻可见） |

脚本依次完成：下载项目到 `C:\agent-bus` → `pip install` 依赖 → `scripts/join_team.py` 入队 → 安装通信节点（`scripts/setup_tray.ps1`：生成 start_tray.bat + 注册 `AgentBusShell` 登录自启 + `AgentBusShellWatchdog` 分钟兜底）→ 启动托盘壳 → 监督拉起执行器。

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

## 四、远程 Shell（受控机完全 Shell 权限）

已配对（安装时 `-PairCode`）且开启 shell 受控能力（`-EnableShellControl`）的受控节点，
主控机可直接执行任意命令（SSH 权限级别，无需 SSH）：

```bash
# 主控机：向受控节点发命令并等待回执
python executor/comm_node.py --role hub --shell-exec \
  --target node-<受控机agent_id> --cmd "hostname" --timeout 30

# 例：跨机文件操作
python executor/comm_node.py --role hub --shell-exec \
  --target node-host-4f2a --cmd "dir C:\\Users" --timeout 60
```

- 受控节点 `agent_id` 见其 `~/.config/agent-bus/device.json`（节点身份为 `node-<agent_id>`）。
- 每次执行：受控机托盘弹通知气泡 + 本地 `control.log` 记录 + bus_server 全量审计（三处留存）。
- 未开启 shell 受控能力 → 拒绝 `shell_control_disabled`；伪造签名 → 拒绝 `验签失败`。
- 关闭方法：受控机托盘菜单取消勾选「shell 受控能力」。

---

## 目录

| 路径 | 说明 |
|---|---|
| `scripts/setup_host.py` | 主机一键引导（装 mosquitto + 起 broker/server + 打开面板） |
| `scripts/join_team.py` | 子设备入队（发现队伍 → 匿名登记 → 上线） |
| `scripts/setup_worker_windows.ps1` | 子设备 Windows 一键引导（装 Python/依赖/入队/装通信节点/启执行器） |
| `scripts/setup_tray.ps1` | 通信节点安装（start_tray.bat + AgentBusShell 登录自启 + Watchdog 分钟兜底） |
| `scripts/watchdog.py` | OS 计划任务兜底：托盘壳挂则拉起 |
| `scripts/setup_linux.sh` | Linux 子设备接入（Skill 主动 / Worker 被召） |
| `scripts/add_node.py`（遗留）/ `broker_ctl.py` | 历史凭据管理（v2 匿名化后常规入队不再需要）/ 用户态 broker 进程管理 |
| `agent_bus/` | Python SDK（MQTT 客户端、discovery 发现协议、files、crypto 控制面、遗留 provision 凭据逻辑） |
| `server/bus_server.py` | 服务端（MQTT 桥 + SQLite + HTTP API + 面板 + 控制面配对端点） |
| `executor/` | 各 CLI 执行器：codebuddy / opencode / workbuddy / interactive |
| `executor/comm_node.py` | 通信节点（托盘壳）：三层自愈 + 监督 + 熔断 + shell 控制面（worker/hub 同构） |
| `skill/` | 通信 Skill：`SKILL.md` + `cli.py`（CLI）+ `mcp_server.py`（MCP 工具） |
| `docs/protocol.md` | 通信契约 + 队伍发现协议（UDP beacon v1.2） |
| `docs/architecture.md` | 通信节点架构设计 v0.4（hub/worker 同构、配对机制、提权方案） |

## 安全模型（简，v2 匿名化 + 控制面配对）

- **信任边界 = 局域网**：broker `allow_anonymous true`，`/api/join` 免口令登记入队，面板与 API 全匿名可读（服务端 conf 与 `bus_server.py` 实测一致）。
- **已移除逐节点凭据**：不再发放 MQTT 独立账号（PBKDF2）/ HTTP Bearer 令牌（见 git `4c805e7`）；`scripts/add_node.py` 保留为历史凭据管理工具。
- **控制面独立配对**（架构 v0.4 §6.1）：主控机生成**一次性安装码**（8 位短码、15 分钟有效、仅本机 API 可生成）→ 人工输入受控机 → 两端各自本地派生配对密钥 `K=HKDF(码)`（码零网络传输）→ `/api/pair` proof 校验后码即作废。之后 hub 发控制命令带 `HMAC(K)` 签名，worker 验签通过才执行——**无 K 无法伪造控制消息**。
- **验签 ≠ 加密**：只做 HMAC 验签（防伪造，微秒级），不做消息加密（局域网可信，偷听已排除）；TLS 仅公网部署需要（见 `docs/broker_setup.md`）。
- **仅限可信局域网**：任何能访问 `1883/8000` 端口的设备都可入队并读到全部消息。跨网/公网部署需恢复认证或加 TLS。

## 已知约束

- 子设备节点需保证局域网可达主机（同网段或可路由）。
- Windows GUI 执行器（WorkBuddy）须跑在用户会话、不能锁屏；CLI 执行器无此限制。
- 广播发现对跨 VLAN/AP 隔离可能失效，此时用 `--host <IP>` 手动加入。