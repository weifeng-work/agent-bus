# 通信节点架构设计（Comm Node Architecture）

> 版本：v0.4（评审修订）
> 状态：待评审
> 关联：需求清单.md 需求1（用户机执行器自愈闭环）；docs/protocol.md（通信契约 v1.0）
> 基线事实：broker `allow_anonymous true`（v2 匿名化，git 4c805e7）；`/api/join` 免口令；面板/API 全匿名
> v0.2：三进程关系；control_key + HMAC 配对；shell_control 一次性开关；受控可见性；远程运维 M5；提权方案
> v0.3：提权方案 B 已采纳；新增「对话路由层」（任意入队智能体经通信节点互相对话，匿名授权，与控制层分离）；明确 control_key 输入位置（主控生成、受控机安装时人工输入，不走网络）
> v0.4：配对机制改为「一次性安装码 + 本地派生长期密钥」——安装码（8 位短码，15 分钟有效，一次性）完全不过网络，两端各自本地派生配对密钥 K=HKDF(安装码)，proof 校验握手后即作废；明确验签≠加密（消息明文走局域网，仅 HMAC 验签，CPU 负担微秒级可忽略）

---

## 1. 背景与目标

### 1.1 问题

- 现有架构里**执行器 = 通信 + 执行耦合**：`executor/*_executor.py` 自行连 MQTT、心跳、处理任务。执行器崩溃即通信中断，且无自愈机制（需求1要补）。
- 主控智能体与受控执行器之间缺少**统一入口与授权点**：MCP server 直连 broker 以自身身份收发，无路由/审计/授权集中位。
- 需要一种**不依赖 SSH**（用户机不开入站、不装 SSH）却能对目标机执行**底层操作**（shell 命令、文件、进程）的通道，权限级别相当于 SSH 命令执行。
- **受控机只装一次**，后续升级与新执行器安装全部由主控远程完成，不再人工登机操作。
- **队列内任意智能体都应能通过通信节点与其他智能体对话**（统一入口 + 审计）。

### 1.2 目标

1. **通信节点（Comm Node）常驻保活**：三层自愈（壳秒级拉起执行器；OS 计划任务分钟级拉起壳），用户机零维护。
2. **通信与执行解耦**：通信节点 = 永远活着的基础设施；执行器 = 可插拔能力，按需激活，崩溃由节点拉起。
3. **底层操作通道**：主控可对目标机执行 shell/fs 级操作（SSH 权限级别，含可选提权），带授权、可见性与审计。
4. **统一对话路由**：任意入队智能体经通信节点与其他智能体对话（寻址/审计集中）。
5. **主控统一入口**：主控智能体经 MCP 工具与 hub 通信，hub 代理路由/授权/审计。
6. **状态真实**：状态灯反映真实 bus 连接/心跳状态，杜绝"进程活着但失联"的假健康。
7. **远程运维**：受控机一次性安装后，升级与执行器安装/激活全远程完成。

### 1.3 非目标

- 不做远程开机（WOL 已排除）。
- 不做公网/跨网部署（当前信任边界 = 局域网可信组织内设备；公网需恢复认证 + TLS）。
- 睡眠/关机/断网/硬件故障只能检测并报告，属人工介入。
- 提权默认不开启（§7，需显式安装授权）。

---

## 2. 总体架构

### 2.1 主控机进程关系（三进程分离）

```
主控机
├─ broker        mosquitto（独立 C 程序 / Windows 服务）
│                └─ 职责：消息路由（主题/收件箱），不存数据
├─ bus_server    Python 进程（现有）
│                └─ 职责：MQTT 桥 + 全队唯一消息库（SQLite bus.db）+ HTTP API + 面板
│                   全队消息/文件/节点数据集中一份，hub 经 API/消息读取，不持有库
└─ hub           通信节点（新，Python 进程，--role hub）
                 └─ 职责：逻辑总机（对话路由/授权/审计/编排）+ 三层自愈 + 熔断 + 托盘 UI
```

- 三进程同主控机、彼此独立：独立升级、故障隔离；mosquitto 为 C 程序无法并入 Python hub。
- 受控机只部署 **worker**（通信节点，--role worker）一个常驻进程。

### 2.2 同构通信节点（一套代码，两种角色）

| 角色 | 部署位 | 职责 | 额外职责 |
|---|---|---|---|
| **hub** | 主控电脑 | 三层自愈、能力监督、状态上报、熔断 | 逻辑总机：对话路由/授权/审计/编排 |
| **worker** | 受控电脑 | 三层自愈、能力监督、状态上报、熔断 | 执行器激活、shell/fs 底层操作、远程升级、对话路由（可选） |

- 两者运行**同一程序**，`--role hub|worker` 区分；自愈/监督/熔断/状态逻辑 100% 复用。
- 通信节点 = 常驻进程（即需求1的"托盘壳"）。托盘 UI 是**该进程的交互面**（状态灯 + 菜单），不是独立进程，不参与保活层级。

### 2.3 三层自愈（复用需求1设计）

```
[OS 计划任务] 分钟级 → 拉起通信节点进程（watchdog）
[通信节点]    秒级   → 监督/拉起执行器子进程
[执行器]      承载   → 智能体任务逻辑（executor/*_executor.py）
```

- 用户最小保证：不睡眠、不锁屏、重启后登录一次、托盘灯绿。
- Windows 用户机不要求 SSH；Debian 无人值守节点保留 SSH。

### 2.4 消息路径（两层：对话路由 + 底层控制）

```
[ 对话层 —— 任意入队智能体，匿名授权 ]
  智能体 A（主控或受控上的执行器）
     │ MCP/CLI 经通信节点（route 能力）
     ▼
  通信节点（hub/worker）── 寻址 + 审计 + 代发
     │ task_request（MQTT 直达 agent/{B}/inbox）
     ▼
  智能体 B

[ 控制层 —— 仅 hub，配对密钥 K 验签 ]
  hub ──→ shell_exec / executor_* / upgrade（HMAC 签名）
              ──→ worker 本地执行（shell_control 开关把关）
```

- **关键约束**：broker 已承担消息路由（主题 + 收件箱），通信节点不做消息转发器（避免单点、多一跳、重复造轮子）。节点是**决策者**（寻址/授权/审计），消息字节仍由 MQTT 主题直达。
- **兼容现状**：执行器间现有直发模式（`skill/cli.py send --to`）保留，已部署节点零改动；新部署可选"经节点路由"模式（cli/mcp 指向节点，节点代发并审计）。
- 所有消息仍走 `docs/protocol.md` 的 task 族协议，入 SQLite 全量追溯。

---

## 3. 角色与职责

| 角色 | 身份 | capabilities 示例 | 说明 |
|---|---|---|---|
| hub | `hub-<host>`（node 身份） | `[supervise, route, control]` | 常驻在线；逻辑总机；持有配对密钥 K |
| worker | `node-<host>`（node 身份） | `[supervise, executor_activate, route?]` + 可选 `[shell, fs]` | 常驻在线；执行器宿主；安装时一次性码配对 |
| 执行器 | `codebuddy_pc1` 等（agent 身份） | `[code, files, shell]` | 激活才上线；任务执行者；经节点与其他 agent 对话 |
| 主控智能体 | 经 MCP 工具，不直接注册 | — | 只与 hub 对话，不管理总线身份/连接 |
| bus_server / broker | 基础设施 | — | 路由 + 存储 + 面板；不变 |

- node 与 agent 通过 `register` 报 capabilities 区分（`executor` 字段填 `comm_node`）。
- 在线名单天然呈现"节点"与"智能体"两类实体。

---

## 4. 通信节点内部结构（进程模型）

```
通信节点进程（comm_node.py）
├─ 守护核心（常驻线程）
│   ├─ 监督循环：2s 周期检查子进程存活 → 秒级拉起；读状态文件判定灯色
│   ├─ 心跳：自身 bus 心跳（30s）+ 本地心跳文件（30s，供 watchdog 判活）
│   ├─ 消息处理：task_request 按 payload.op 分派
│   │   ├─ op="run"            → 转发/代发（对话层，匿名）
│   │   ├─ op="shell_exec"     → 验签 + shell_control 开关 → 本地执行（控制层）
│   │   ├─ op="executor_*"     → 验签 → 拉/停执行器（控制层）
│   │   └─ op="upgrade"        → 验签 → 自更新（控制层）
│   ├─ 受控记录：每次控制操作追加本地 control.log + 托盘通知气泡
│   └─ 熔断执行：受控开关关闭 → kill 子进程树（taskkill /T /F）+ 写 stopped
├─ 托盘 UI（GUI 模式；--headless 可禁用）
│   ├─ 状态灯：绿(connected) / 黄(reconnecting/失联) / 灰(stopped)
│   ├─ 受控状态：常驻可见（角标/文案），控制操作时弹通知气泡
│   └─ 右键菜单：受控开关 / shell 受控能力开关 / 自修复 / 查看错误日志 / 查看受控记录 / 一键收集诊断包 / 退出
└─ 状态文件 executor_status.json（执行器经 BUS_STATUS_FILE 写入）
```

### 4.1 状态判定（真实 bus 状态，非进程存活）

| 灯色 | 判定条件 | 含义 |
|---|---|---|
| 绿 | 子进程在 且 状态文件新鲜(<60s) 且 status=connected | 已连接 bus，心跳正常 |
| 黄 | 子进程在 但 状态文件过期 或 status=reconnecting | 进程在，连接断/重连中 |
| 灰 | 子进程不在 或 受控开关关闭 | 已停止 |

- 状态文件由 `agent_bus/client.py` 在真实 MQTT 事件写入：`_on_connect→connected`、`_on_disconnect→reconnecting`、`disconnect()→stopped`，心跳循环刷新 ts。
- 注入方式：环境变量 `BUS_STATUS_FILE`（节点拉起子进程时设置），执行器零改动。

### 4.2 熔断（受控开关）

- 「正在智能体受控队列」开关 = 指示灯 + **紧急停止按钮**。
- 关闭：kill 执行器进程树 + 断开 + 写 stopped + 灯灰；打开：立即拉起执行器。
- 开关状态落盘（`runtime/controlled.json`），节点重启后保持。

### 4.3 托盘菜单

| 菜单项 | 动作 |
|---|---|
| 受控开关（勾选） | 见 4.2 |
| shell 受控能力开关（勾选） | 见 §6.3；状态常驻可见 |
| 自修复 | 检查 Python/依赖/broker 可达性 → 自动重启执行器 → 报告结果 |
| 查看错误日志 | 打开 `<install>/data/{agent_id}.log.err` |
| 查看受控记录 | 打开本地 `control.log`（每次控制操作的完整记录） |
| 一键收集诊断包 | 打包日志 + 状态文件 + bus.env + device.json + control.log → `data/diagnostics/agent-bus-diag-<ts>.zip` 并打开目录 |
| 退出 | 停止监督（执行器随停）；OS 计划任务 ≤1min 自动拉回 |

---

## 5. 能力模型

### 5.1 内置能力（worker 侧）

| 能力 | 消息 op | 授权 | 说明 |
|---|---|---|---|
| `route` | `task_request`(op=run) | 匿名（对话层） | 任意入队智能体经节点与其他智能体对话；节点寻址/审计/代发 |
| `supervise` | `executor_activate` / `executor_deactivate` | K 验签 | 拉/停执行器子进程；崩溃自动重启 |
| `shell` | `shell_exec` | K 验签 + shell_control | 任意命令执行（subprocess，超时硬杀），权限 = 当前用户 |
| `fs` | `shell_exec`（fs 封装或 shell 原语） | K 验签 + shell_control | 文件读写/传输（也可走现有 /api/files Claim-Check） |
| `upgrade` | `upgrade` | K 验签 | 远程自更新（见 5.4） |
| `install_executor` | `executor_activate`（带 install 指令） | K 验签 | 远程下载/安装新执行器（见 5.4） |

### 5.2 对话路由（route，v0.3 新增）

- 队列内**任意智能体**（主控或受控上的执行器 agent）可经通信节点与其他智能体对话。
- 协议：复用 `task_request`（op=run）；节点代发时保留真实 `sender_id`，可附加 `via` 字段记录路由节点（审计用）。
- 授权：**匿名**（与现有任务同权），**不需要 K**——对话是普通任务消息，控制层才需要签名。
- 形态：cli.py/mcp_server 支持"经节点路由"模式（`--via <node_id>` 或默认走 hub）；现有直发模式保留兼容。

### 5.3 插件式执行器

- 执行器 = 节点的一个"能力包"：`executor/<type>_executor.py` 已满足 `--agent-id/--name` 约定，可直接被节点拉起。
- 激活后执行器以**独立 agent_id** 注册上线；停用即下线。

### 5.4 远程运维（受控机只装一次）

**执行器远程安装**：
1. hub 发 `executor_activate`，payload 含 `install: {source_url, executor_type, sha256}`（或本地文件 URL）
2. worker 下载安装包（校验哈希）→ 解压到安装目录 → 装依赖（pip）→ 激活执行器 → 回 task_result
3. 全程无需人工登机

**通信节点远程升级（自更新）**：
1. hub 发 `upgrade`，payload 含新版本包 URL + 版本号 + sha256
2. worker 下载到临时目录并校验 → 写 `upgrade_pending.json`
3. 启动**升级代理**（detached 小进程，独立于 worker）：停 worker → 替换代码目录 → 重启 worker → 自检回执
4. 升级代理解决 Windows 文件锁（运行中的 pyc/日志句柄）；worker 重启后向 hub 报告新版本
- 边界：仅限安装目录用户权限内；系统级（服务/Program Files）需提权通道（§7）。

### 5.5 协议扩展（最小化：复用 task 族协议，payload 增加 `op`）

```
task_request payload 增加字段: op
  op = "run"                  → 执行器正常任务 / 节点代发（对话层，匿名）
  op = "shell_exec"           → worker 直接执行命令（不转智能体）
       payload.cmd / cwd / timeout_seconds
  op = "executor_activate"    → worker 拉起（或远程安装+拉起）执行器
       payload.executor / agent_id / name / args / install?
  op = "executor_deactivate"  → worker 停用指定执行器
       payload.agent_id
  op = "upgrade"              → worker 自更新
       payload.url / version / sha256

控制类消息（shell_exec / executor_* / upgrade）附加字段:
  payload.control_sig = HMAC-SHA256(配对密钥 K, canonical(payload))  → 见 §6.1
对话消息（op=run）可选附加:
  payload.via = 路由节点 agent_id（审计用）
```

- 回执统一用 `task_result`（success/error/timeout/cancelled），`correlation_id` 追踪不变。
- `validate()` 仅对 `op` 做白名单校验 + 控制类消息要求 `control_sig` 字段存在；验签在 worker 侧完成。
- 审计：bus_server 已全量入库消息（含 payload），shell 操作天然可追溯。

---

## 6. 授权与安全模型

**信任假设**：局域网、组织内可信设备（用户已确认）。在此前提下，通信与任务通道保持 v2 匿名（免口令入队），**控制面单独授权一次**。

### 6.1 配对机制（一次性安装码 + 本地派生密钥，v0.4）

**目标**：人工只输入 8 位短码，不搬运长随机码；安装码完全不过网络。

```
1. 主控面板「安全设置」生成一次性安装码（8 位短码，15 分钟有效，一次性），
   同时本地算好配对密钥 K = HKDF-SHA256(安装码, info="agent-bus-ctrl-v1")
2. 人工把短码输入受控机（setup_worker_windows.ps1 -PairCode 或交互输入）
3. worker 本地派生 K（安装码只在 worker 内存，不发送、不落盘）
4. worker POST /api/pair {agent_id, proof = HMAC(K, "pairing")}
5. bus_server 用已知 K 校验 proof → 配对成功，登记 agent_id
6. 安装码立即作废（一次性 + 15 分钟有效期双保险），仅 K 长期有效
7. 之后控制消息由 hub 用 K 签名（HMAC-SHA256），worker 验签通过才执行
```

- **安装码零网络传输**：两端各自本地算 K，嗅探者拿不到码更拿不到 K。
- K 存储：主控侧（bus_server/hub）与受控侧 worker 本地各一份；任一侧 K 泄露 → 面板重置（K 作废，全部 worker 重新配对，动作入审计）。
- 验签成本：HMAC-SHA256 ≈ 0.5–2µs/条，100 条/秒无感知；**不引入消息加密**（局域网明文，信任边界内，见 §6.6）。
- 非对称密钥不引入：当前无不可抵赖需求；未来跨信任域再升级（Ed25519 签名同样微秒级）。

### 6.2 授权规则（两层）

| 操作 | 允许发送方 | 授权 | 目标 |
|---|---|---|---|
| run（对话/任务） | **任意入队智能体** | 匿名（现状不变） | 任意执行器 |
| executor_activate / deactivate | 仅 hub | 配对密钥 K 验签 | 本 worker 节点 |
| shell_exec / fs | 仅 hub | K 验签 + shell_control 开关 | 本 worker 节点 |
| upgrade | 仅 hub | K 验签 | 本 worker 节点 |

### 6.3 shell/fs 一次性开启（免二次确认，但状态可见）

- worker 本地持久化开关 `shell_control`（`runtime/control_config.json`），**安装后默认关**。
- 开启动作发生在**受控电脑本地**（托盘菜单勾选，或安装脚本参数 `-EnableShellControl`），一次即可；之后控制消息验签通过即执行，**不再弹确认**。
- 开启状态**常驻可见**：托盘菜单勾选态 + 状态灯角标；随时可关（熔断原则）。
- 关闭时收到 shell_exec：拒绝并回 `task_result(status=error, reason=shell_control_disabled)`，入审计。

### 6.4 受控可见性（强制，不可关闭）

每次控制操作（shell_exec / executor_* / upgrade）三处留存：
1. **托盘通知气泡**："主控 `hub-xxx` 正在执行: <命令摘要>"
2. **本地 control.log**：追加时间/发送方/op/命令/结果摘要（托盘菜单可查看）
3. **bus_server 审计**：全量消息入库（面板可检索）

### 6.5 安全红线

- **shell 能力 + 匿名 broker = 高危组合**：若无 §6.1 签名保护，等于对局域网开放目标机 shell。配对密钥 K 是硬约束，不是可选项。
- 安装码不得经匿名 /api/join 分发，且全程不过网络（§6.1）——只能人工输入到受控机。
- 提权通道（§7）开启时攻击面扩大，必须同受 K 签名保护 + 强制审计。

### 6.6 验签 ≠ 加密（CPU 负担澄清）

- 本架构只做**消息验签**（防伪造/防冒充），**不做消息加密**（防偷听）。
- 理由：信任边界 = 局域网可信组织内设备，偷听已排除；消息明文走 MQTT（v2 匿名基线不变）。
- 成本：HMAC-SHA256 ≈ 0.5–2µs/条，100 条/秒对现代 CPU 完全无感；即便未来引入 Ed25519 签名也仅微秒级。
- 真正吃 CPU 的是传输层加密（TLS/AES），本架构局域网内**不引入**；仅公网/跨网部署时按 docs/broker_setup.md 启用 TLS。

---

## 7. 提权通道（管理员操作，M6 可选）——方案 B 已采纳

### 7.1 意义

- 无提权：worker 仅能操作当前用户级（用户目录、用户软件、普通命令）。
- 有提权：主控可直接装系统软件、注册服务、改 HKLM/防火墙/系统设置、跨用户操作——**免去 RDP / 管理员 SSH 会话**，远程运维能力完整。

### 7.2 已采纳方案（B：最高权限计划任务）

- **安装时授权一次**（注册计划任务时的一次管理员授权/UAC），**之后免确认**。
- 实现：安装时注册计划任务 `AgentBusElevated`（`/RL HIGHEST`），worker 收到 `shell_exec(elevated=true)` 时委托该任务执行，不弹 UAC。
- **诚实边界**：Windows 不存在"免 UAC 且零风险"的提权路径。方案 B = 一次授权换取长期管理员能力，代价是攻击面扩大（该通道被攻破 ≈ 目标机沦陷）。
- 要求：提权通道同样受配对密钥 K 签名保护；提权操作额外高亮审计（O6）。
- Linux 提权后置（sudoers 免密或 systemd 服务），随 Linux comm node 形态一并设计。

---

## 8. 部署与安装

### 8.1 受控端（worker，Windows）——一次性

- `scripts/setup_worker_windows.ps1`：装 Python/依赖 → join（匿名入队）→ **输入 -PairCode 配对**（可选 -EnableShellControl）→ 生成 `start_tray.bat` + 注册计划任务 + 启动托盘壳。
- 计划任务（schtasks，交互式用户会话）：
  - `AgentBusShell`：`/sc onlogon` → start_tray.bat（登录即起）
  - `AgentBusShellWatchdog`：`/sc minute /mo 1` → `scripts/watchdog.py`（壳挂分钟级兜底）
- watchdog 判活：`runtime/tray_shell.pid` + `runtime/tray_heartbeat.ts`（新鲜 <150s 即活）；否则以独立进程拉起壳。
- 之后不再登机：升级/装执行器/对话全远程（§5）。

### 8.2 主控端（hub）

- 同形态安装（`--role hub`）；持有配对密钥 K（主控面板生成安装码时本地派生，配置到 hub 环境）。
- MCP server（skill/mcp_server.py）配置指向 hub；`BUS_AGENT_ID` 语义不变。

### 8.3 依赖

- `requirements.txt` 增加：`pystray`（托盘）、`pillow`（图标）。仅 GUI 模式需要，`--headless` 免装。

---

## 9. 实施路径（里程碑）

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **M1 worker 托盘壳**（= 需求1） | comm_node.py（worker 形态）：三层自愈 + 监督 + 熔断 + 真实状态灯 + 托盘菜单 + watchdog + setup 集成 | headless 冒烟：子进程崩溃秒级重启、状态文件驱动灯色、开关熔断 |
| **M2 主控 hub** | hub 形态：对话路由/代理/授权/审计；mcp_server 接 hub | hub 代智能体发任务、控制消息验签生效、审计可查 |
| **M3 shell/fs 能力** | shell_exec/fs + 一次性安装码配对 + shell_control 开关 + 受控可见性（气泡/记录） | 主控经 hub 对 worker 执行命令，验签/开关/记录全链路 |
| **M4 执行器插件化** | executor_activate/deactivate（本地） | 远程激活/停用执行器，崩溃自动重启 |
| **M5 远程运维** | 执行器远程安装 + 节点远程升级（升级代理） | 受控机装一次后，升级与装新执行器全程远程 |
| **M6（可选）提权通道** | 最高权限计划任务（方案 B，已采纳） | 主控远程执行管理员操作，免 UAC 二次确认，审计完整 |

- M1 的 comm_node.py 从第一天就按 `--role` + 能力注册点 + 消息 op 分派形态写，M2-M6 不返工。

---

## 10. 与现有组件的映射

| 现有组件 | 在新架构中的位置 |
|---|---|
| `executor/*_executor.py` | 插件式能力包（不改签名，仅由节点拉起） |
| `skill/mcp_server.py`（bus_* 工具） | 主控智能体 → hub 的入口；支持 --via 经节点路由（M2） |
| `skill/cli.py` | 对话直发保留；新增经节点路由模式 |
| `agent_bus/client.py` | 增加 `BUS_STATUS_FILE` 状态上报（M1 前置） |
| `server/bus_server.py` | 增加：面板「安全设置」一次性安装码生成/配对重置（M3 前置）；其余不变 |
| `scripts/setup_worker_windows.ps1` | 步骤 5 改托盘壳安装 + -PairCode 配对（M1/M3） |
| `scripts/watchdog.py`（新增） | OS 计划任务兜底目标（M1） |
| `scripts/setup_tray.ps1`（新增） | 计划任务注册（M1） |
| `scripts/upgrade_agent.py`（新增） | 升级代理：替换+重启（M5） |

---

## 11. 开放问题（v0.3 收敛后）

| # | 问题 | 状态 |
|---|---|---|
| O1 | hub 身份凭据保护 | **已定**：一次性安装码 + 本地派生密钥 K + HMAC 配对（§6.1） |
| O2 | shell 确认交互频率 | **已定**：shell_control 一次性开关（§6.3） |
| O3 | Linux 受控端 comm node | 保持后置（沿用 setup_linux.sh），随 M 阶段评审再定 |
| O4 | 多 hub 场景 | 暂单 hub；多 hub 需定义 hub 间互信（配对密钥共享机制） |
| O5 | 提权方案 | **已定并采纳**：方案 B 最高权限计划任务，M6（§7.2） |
| O6 | 提权通道的独立审计与告警强度 | 待 M6 设计时细化（建议：提权操作额外发一条高亮审计） |
| O7 | 对话路由的 `via` 审计字段是否需要前端展示 | 待 M2 评审时定（面板消息时间线展示路由节点） |
