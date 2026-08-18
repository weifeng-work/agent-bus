# Git 中心仓协作方案（Git Central Repo）

> 版本：v0.3（已评审，全部决策已确认，已吸收外部评审意见）
> 状态：G1 已验收（2026-08-18）；G2 施工中
> 关联：需求清单.md；docs/protocol.md（通信契约 v1.0）；docs/architecture.md（通信节点架构 v0.4）
> 修订记录：v0.3（2026-08-18）按本机 opencode 外部评审裁决吸收八项改进，
>   详见 §11 评审回应。
> 用户已拍板决策（2026-08-18）：
>   D1 鉴权：git daemon（端口 9418），完全无鉴权（与局域网可信边界一致）
>   D2 推送范围：所有 agent 只推自己的任务分支，main 由合并角色统一合并
>   D3 分支命名：短码（task_tag，task_id 前 8 位）
>   D4 审查合并：由审查者 agent（常驻执行器 reviewer_main）裁决——定向 task_request
>      触发（投递即唤醒）+ git_event 留痕，支持驳回返工闭环；冲突不擅自解，
>      驳回原作者 rebase 重提。详见 §6.1。
>   D5 审查者选型与切换：暂定由 TraeWork CN（traework 执行器）承载审查；审查者必须
>      可人工切换——目标 agent_id 从配置文件 data/reviewer.json 动态解析，改文件即生效，
>      无需重启。详见 §6.1。
>   Q1-Q6 全部按建议确认，见 §10 决策记录。
> 环境已核实（2026-08-18，只读）：git 2.55.0.windows.3；git-daemon.exe 位于
>   C:\Program Files\Git\mingw64\libexec\git-core\；端口 9418 空闲；
>   主机物理网卡 IP 192.168.31.186（WLAN），另有 VMware/Hyper-V 虚拟网卡需注意区分。

---

## 1. 背景与目标

### 1.1 问题

- 多机多智能体协作开发同一项目时，其他机器的 agent 无法获得项目文件：现有 Claim-Check 只能传单个文件，不构成持续演化的项目目录，也没有版本/冲突/回滚语义。
- agent 干活必须依赖完整本地目录树（grep/编译/测试），"远程按需读"不能支撑完整开发循环。

### 1.2 定位

**git = 数据平面，bus = 控制平面**。git 负责字节与状态（分布式克隆、冲突合并、历史回滚）；bus 负责协调语义（任务分配带分支、push 完成通知、合并完成广播）。bus 不承担文件传输。

### 1.3 目标

1. worker 领任务即获得完整本地副本：clone/checkout 后读文件、编译、测试全在本机。
2. 多 agent 并行开发互不干扰：一任务一分支。
3. 改动可审计可回滚：commit author = agent_id，blame/历史天然可用。
4. 主机不是数据单点：每台机器都是完整克隆，主机故障时 origin 可临时改指任何克隆。
5. 出站连接模型不变：worker 仍主动连主机（与 MQTT/HTTP 一致），无需 worker 开入站端口。

### 1.4 非目标

- 大二进制资产管理（Git LFS，后置评估）。
- 公网/跨网仓库（9418 匿名读写严禁出局域网，见 §9）。
- PR/Web 审查界面（gitea 为 G4 可选升级位，本期不引入）。

---

## 2. 总体架构

```
[ 主机 192.168.31.186 ]
  git-daemon :9418   匿名读写（--enable-receive-pack，D1）
    └─ base-path: <agent-bus安装目录>/data/git_repos/
         └─ <project>.git    裸仓库 + pre-receive hook（防呆）
  bus_server :8000   协调消息（git_event 全量入库审计）
  reviewer_main      常驻审查执行器：main 的唯一写入口（D4，§6.1）

[ Worker N 台 ]
  本地克隆: <工作区>/<project>/        完整仓库
  任务分支: task/<短码>  ──push──>  中心仓（D2/D3）
                     └─审查请求(定向task_request)──> reviewer_main
```

- 数据流（git 协议）与协调流（MQTT/HTTP）完全分离；bus 消息只携带 repo/branch/commit 等元数据。
- 与匿名化基线（v2，git 4c805e7）一致：信任边界 = 局域网可达。

---

## 3. 主机侧部署

### 3.1 目录与裸仓库

| 项 | 取值 |
|---|---|
| base-path | `<agent-bus安装目录>/data/git_repos/`（与 bus.db 同一数据根，便于备份） |
| 首个仓库 | `agent-bus.git`（bare；种子源 = 现有工作仓库，见下） |
| 种子推送 | 工作仓库执行：`git remote add central <base-path>/agent-bus.git` → `git push central main` |
| 工作仓库 | origin 保持 GitHub 不变，central 为第二 remote，互不影响 |

### 3.2 启动 git daemon

```bat
"C:\Program Files\Git\mingw64\libexec\git-core\git-daemon.exe" ^
  --reuseaddr --verbose ^
  --listen=192.168.31.186 ^
  --base-path="<agent-bus安装目录>\data\git_repos" ^
  --export-all ^
  --enable=receive-pack ^
  --port=9418
```

| 参数 | 说明 |
|---|---|
| `--listen=192.168.31.186` | 只绑物理网卡（WLAN），避免虚拟网卡抢路由（B6，v0.3 吸收） |
| `--base-path` | URL 根路径；仓库 URL 相对它解析 |
| `--export-all` | 免 `git-daemon-export-ok` 标记文件 |
| `--enable=receive-pack` | 允许 push（D1；不加则只读）。**注意**：git 2.55 已移除旧写法 `--enable-receive-pack`（G1 施工实测） |
| `--reuseaddr --verbose` | 端口快速复用 + 请求日志（排障用） |

worker 访问 URL：`git://192.168.31.186/agent-bus.git`

> 本机有多块虚拟网卡（VMware 192.168.154.x/26.x、Hyper-V 192.168.176.x）。
> daemon 已用 `--listen` 绑死 WLAN IP；主机本地组件（如 reviewer）可不经 daemon，
> 直接以裸仓文件路径作 remote（本地路径推拉），不受网卡影响。

### 3.3 持久化与自愈

- daemon 为前台进程，需要拉起机制。分两步：
  - G1：脚本手动启动（验证连通性优先）。
  - G2：注册**登录无关**的计划任务 `AgentBusGitDaemon`——daemon 无 GUI、不需用户会话，
    任务以 SYSTEM 上下文运行（`/sc onstart` 开机即起，或 `/sc minute` 分钟级检查拉起），
    **不用 `/sc onlogon`**（重启无人登录时不会启动，B1，v0.3 吸收）；watchdog 分钟级兜底，
    复用 comm_node 三层自愈的既有模式（architecture.md §2.3）。
- 长期归属：M2 hub 落地后，daemon 可纳入通信节点 supervise 管理（视为基础设施子进程），届时撤掉独立计划任务。

### 3.4 防火墙

```bat
netsh advfirewall firewall add rule name="AgentBus GitDaemon" dir=in action=allow ^
  protocol=TCP localport=9418 remoteip=192.168.31.0/24
```

- 已确认限定子网 192.168.31.0/24（Q6，v0.2）；与现有 1883/8000/41830 的放行规则并列管理。

---

## 4. 分支与推送约定（D2/D3 落地）

| 项 | 约定 |
|---|---|
| 保护分支 | `main`：仅合并角色可写，agent 直推视为违规 |
| 任务分支 | `task/<短码>`，短码 = task_id 前 8 位（与执行器 task_tag 完全对齐） |
| 分支创建 | `git checkout -b task/<短码> origin/main`（始终基于最新 main） |
| 提交信息 | `task <短码>: <一句话摘要>` |
| 提交身份 | worker 初始化时 `git config user.name "<agent_id>"` → blame/审计天然定位到人 |
| force push | 全分支禁止 |

### 4.1 pre-receive hook（防呆层）

匿名模型无法认证"谁在推"，但裸仓库的 hook 对任何传输方式都生效，可挡住常见错误：

1. 拒绝分支名不匹配 `^refs/heads/(main|task/[0-9a-f]{8})$` 的推送；
2. 拒绝一切带 `--force` 的更新；
3. 放行 main 推送但打印告警日志（Q3 已确认：放行 + 记录。若硬阻断会连唯一合法写入口
   reviewer_main 一起挡住，故不阻断，靠日志 + bus 审计追责，见 §6.1）；
4. **硬拦大文件**：单文件 >10MB 直接拒绝（B/膨胀防护，v0.3 吸收），倒逼大资产走 LFS；
5. hook 为裸仓内 **POSIX sh 兼容**脚本（`#!/bin/sh`，Git for Windows 自带 sh，无额外依赖；
   注意 Windows 下路径分隔符/换行符差异，B2，v0.3 吸收），且须在**第二台机器真推一次**验证，
   不能只本地跑。

**诚实边界**：这是约定级防呆（防 agent 手滑），不是安全级鉴权；追责靠 commit author + bus 审计（§6）双轨。

---

## 5. Worker 侧工作流

1. **首次**：`git clone git://192.168.31.186/agent-bus.git` 到本地工作区（建议 `<agent-bus安装目录>/data/workspaces/<project>/`），并设置 `user.name=<agent_id>`。
2. **任务开始**：`git fetch origin` → `git checkout -b task/<短码> origin/main`。
3. **执行任务**，按 §4 提交信息规范 commit。
4. **推送**：`git push origin task/<短码>`。
5. **通报**：经 bus 发 `git_event(pushed)`（§6）。
6. **下一任务前**：`git checkout main && git pull`，保持本地 main 新鲜。

- 并发模型：MVP 单克隆串行（执行器本就串行）；单机多任务并行用 `git worktree` 扩展，后置。
- git 安装：worker 需自备 git。已确认并入 `setup_worker_windows.ps1` 自动检测/安装（Q4，v0.2）；
  因涉及修改既有脚本，施工时单独授权，不在 G1/G2 范围内。安装走**离线包静默安装**
  （`Git-*-64-bit.exe /VERYSILENT /NORESTART`，预下载放内部源），失败回滚并上报，
  避免 UAC/联网卡顿（B5，v0.3 吸收）。

---

## 6. 协调协议扩展（最小化）

protocol.md 扩展一个报文类型（task 族不动），与 `op` 扩展同风格：

```
type = "git_event"
payload = {
  event:  "pushed" | "review_request" | "merged" | "pull_advisory",
  repo:   "agent-bus",
  branch: "task/8a3f2b1c",
  commit: "<sha，可选>",
  note:   "<可选说明>"
}
```

- `task_request` payload 可选增加 `repo` / `branch` 字段：任务信封携带仓库 URL 与分支名，执行器注入提示词，agent 领任务即知去哪 clone、建什么分支。
- bus_server 对 git_event 无需特殊处理——全量入库是既有行为，面板时间线天然可检索。
- `skill/cli.py` 增 `git-event` 子命令为 G2 事项，不阻塞 G1。

### 6.1 审查合并机制（D4，v0.2 确认）

**审查者形态**：审查者是常驻主机（或任一常驻节点）上的一个执行器，与
codebuddy/opencode/traework 执行器完全同构——常驻连 bus、轮询 inbox。
**暂定由 TraeWork CN 承载（traework 执行器，D5）**，但审查者是"角色"不是"某台机器"，
随时可换。**没有额外的唤醒机制：投递即唤醒**，一条进 inbox 的消息就是唤醒信号。

**审查者可切换（D5）**：
- 审查请求不硬编码目标，而是从 `data/reviewer.json` 读取当前审查者 agent_id：
  `{"agent_id": "traework_pc1", "updated": "<时间戳，可选>"}`；
- 人工切换 = 改这一个文件（后续可加面板按钮/CLI 子命令封装），下次审查请求即生效，
  任何执行器与 bus 服务都不需要重启；写入用**临时文件 + rename 原子替换**，
  避免切换瞬间读到半截 JSON（B3，v0.3 吸收）；
- 文件缺失或解析失败时回退到默认值（traework_pc1）并在 bus 上发一条告警 git_event；
- 新审查者上任无状态迁移负担：审查依据是 git 仓本身（fetch + diff），不依赖前任记忆。

**"请求审查"的双重身份**：
- **触发器**：定向 `task_request` 发给 `reviewer_main`，复用既有任务协议，带
  `correlation_id`，发起方可同步等到审查结论（task_result 回执）。审查超时沿用既有
  `timeout_seconds` 字段（建议 30min），超时由发起方按任务语义重发或升级（B4，v0.3 澄清）。
- **审计痕迹**：`git_event(review_request / merged)` 广播，全量入库、面板时间线可见。

**完整生命周期**：
1. 执行者 E 领任务（信封带 repo URL + 分支约定），建 `task/<短码>` 分支干活；
2. E `git push origin task/<短码>`（hook 校验分支名/禁 force）；
3. E 向 `reviewer_main` 发 `task_request("审查合并 task/<短码>")`，同时广播
   `git_event(review_request)`；
4. R 被 inbox 消息唤醒：`git fetch` → `git diff main..task/<短码>` 审查；
5. 裁决分两支：
   - **通过**：`git merge --no-ff task/<短码>` 入 main（保留任务分支拓扑，审计友好）
     → `git push origin main` → 广播 `git_event(merged)` → task_result 回执 E "已合并"；
   - **驳回**：task_result 回执 E 问题清单 → E 在**同一分支**修改、重新 push、重新发
     审查请求，形成"提交→审查→返工"闭环，分支名不变、短码贯穿。
6. 其他 agent 无需被通知：下个任务起步 `git fetch` main 自然拿到合并结果
   （merged 广播仅供面板可视化，非必需路径）。

**冲突处理**：R 不擅自解内容冲突。合并撞冲突时驳回 E："基于最新 main rebase 后重提"。
谁写的代码谁最懂怎么解，审查者只裁决、不做泥瓦匠。

**驳回升级（防死循环）**：同一任务分支的驳回次数计数（记入 git_event note / 任务元数据），
**累计 ≥3 次自动升级人工**——发面板告警并暂停该分支的自动审查闭环，由人裁决
（继续返工 / 拆分任务 / 放弃），防止反复 rebase 失败的死循环（驳回升级，v0.3 吸收）。

**人类升级钩子（v1 预留，默认关）**：可配阈值（diff 超 N 行 / 触碰受保护路径如协议
与安全相关代码），触发时 R 不直接合并，改发面板消息请人工终审。本期只留钩子不启用。

---

## 7. 能力清单（实现后）

| 能力 | 说明 |
|---|---|
| 本地全量副本 | worker 读/grep/编译/测试全在本机，"其他机器如何读项目文件"彻底解决 |
| 并行开发 | N agent N 分支，互不干扰 |
| 冲突治理 | git merge 集中到合并环节处理，不打断执行 |
| 审查闭环 | 任务分支须经 reviewer_main 审查方可入 main，驳回返工成环（D4） |
| 全程审计 | commit author = agent_id，blame/回滚即用 |
| 即时入伙 | 新机器 clone 一次即完成入职 |
| 数据容灾 | 全克隆皆备份；主机故障时 origin 可改指任意克隆，工作零丢失 |
| 人机同口 | 人类用普通 git 工具直接参与同一仓库 |
| 协调可见 | git_event 入 bus 库，面板时间线可查 |

---

## 8. 里程碑

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **G1 中心仓联通** | `scripts/init_central_repo.ps1`（建裸仓 + 装 hook + 种子推送 + 端到端校验，B8）+ daemon 启动脚本（--listen 绑 WLAN）+ 防火墙规则 | 局域网第二台机器 clone 成功、push task 分支成功 |
| **G2 约定与保护** | pre-receive hook（分支名/禁 force/硬拦 >10MB 大文件）+ daemon **登录无关**计划任务持久化（SYSTEM/onstart）+ git_event 报文定义 | hook 挡非法分支名/force/大文件（第二台机器真推验证）；git_event 在面板可见 |
| **G3 执行器集成** | task_request 带 repo/branch；worker 工作区自动 clone/复用；commit 身份 = agent_id；部署常驻 reviewer_main | bus 发任务 → agent 自动完成 建分支/干活/push/审查请求；reviewer 完成 审查/合并或驳回 闭环 |
| **G4（可选）升级位** | gitea Web 审查 / 面板 git 视图 / worktree 并行 / LFS | 后置另议 |

**交付记录（2026-08-18）**：G1 验收通过——Debian 二机（weifeng-pc，git 2.47.3）经
`git://192.168.31.186/agent-bus.git` clone/push 成功；hook 远程执行且三项负面测试全过
（非法分支名拒、>10MB 拒、force 拒），task/* 删除放行——原属 G2 的 hook 验收提前完成。
施工踩坑已回写：`--enable=receive-pack` 新语法（§3.2）、裸仓 HEAD 须指 main（init 脚本内置）。

**G2 现状（2026-08-18 更新）**：git_event 报文定义已完成（docs/protocol.md §2.7 v1.2 +
scripts/send_git_event.py）；pre-receive 行尾已固定 LF（.gitattributes `eol=lf`）。
G2 剩余：daemon 登录无关计划任务持久化 + git_event 面板可见性验证。

**交付代码已 commit（2026-08-18，未 push）**：
- `660aaf8` feat(interactive) psmux 可视附着窗口
- `0380e90` feat(git) Git 中心仓协作（方案/钩子/init/daemon/watchdog/send + git_event 协议）
- `8125103` feat(executor) TraeWork CN 执行器（CDP 桥接，审查者前置）
- `236871a` docs Readiness Probe 设计文档（待评审）
- `95065a4` fix(git) pre-receive 钩子固定 LF 行尾（.gitattributes eol=lf）

---

## 9. 风险与红线

- **9418 匿名读写 = 局域网外高危**：防火墙规则限子网；严禁端口映射/公网暴露；跨网部署必须先恢复鉴权（与 broker 匿名化同一红线逻辑，见 README 安全模型）。
- **推送冲突**：两 agent 改同一文件 → 合并环节冲突。缓解：任务划分尽量按文件边界；冲突不擅自解，驳回原作者 rebase 重提（§6.1，D4）。
- **审查者误判**：合并质量取决于 reviewer 的提示词与模型能力。缓解：v1 人工抽检 + 人类升级钩子预留（§6.1）；main 的每次合入都有 --no-ff 拓扑与 commit author 可回滚追责。
- **Windows daemon 稳定性**：前台进程，需登录无关计划任务（SYSTEM/onstart）+ watchdog 兜底（G2 交付前勿无人值守依赖，B1）。
- **仓库膨胀**：hook 硬拦单文件 >10MB（v0.3）；确有大资产需求再评估 LFS。
- **驳回死循环**：反复 rebase 失败或同质补丁循环。缓解：驳回 ≥3 次自动升级人工（§6.1，v0.3）。
- **多网卡歧路**：worker 必须连 192.168.31.186（WLAN），虚拟网卡网段（154.x/26.x/176.x）不通。
- **Windows 主机本机 git:// push 挂起（已知约束，2026-08-18 实测复现）**：本机 git 2.55 经 git:// 推送必挂
  （客户端 pack-objects 0 CPU 僵死，25s+ 无进展；`--no-thin` 同样挂起）。排除 daemon 与 thin-pack：
  同一 daemon 下服务端 receive-pack 正常就位、file-path remote 推送秒成功、跨机 git:// 推送正常
  （Debian 二机验证）——定位在「本机客户端 + git:// 协议」组合，疑似 git 2.55 客户端 bug，
  根因尚未完全定位（本机回环 vs 跨机路径差异未 100% 排除）。**主机本地组件（如 reviewer）一律用
  裸仓文件路径作 remote**；跨机推送不受影响。

---

## 10. 决策记录（2026-08-18 全部确认）

| # | 事项 | 决定 |
|---|---|---|
| Q1 | 首个种子仓库 | agent-bus.git |
| Q2 | 合并角色 | 审查者 agent：常驻主机执行器 reviewer_main；定向 task_request 触发 + git_event 留痕 + 驳回返工闭环（D4，详见 §6.1） |
| Q3 | hook 对 main 推送 | 放行 + 告警日志（不硬阻断，否则挡住唯一合法写入口） |
| Q4 | git 安装并入 setup_worker_windows.ps1 | 并入；涉既有脚本修改，施工时单独授权 |
| Q5 | daemon 持久化 | G2 先独立计划任务（AgentBusGitDaemon + watchdog）；comm_node 统管为长期归属 |
| Q6 | 防火墙范围 | 限子网 192.168.31.0/24 |
| Q7 | 审查者选型与切换（2026-08-18 补充） | 暂定 TraeWork CN 承载（traework 执行器）；可人工切换：data/reviewer.json 动态解析，改文件即生效（D5） |

### 保留项（不阻塞施工，随里程碑细化）

| # | 事项 | 说明 |
|---|---|---|
| O1 | 人类升级阈值参数 | diff 行数 N、受保护路径清单——随 G3 评审定 |

---

## 11. 外部评审回应（opencode，2026-08-18）

v0.2 交本机 opencode（nemotron-3-ultra-free）独立评审，裁决如下（v0.3 已落实"吸收"项）。

### 11.1 吸收（便宜且正确，已入稿）

| 项 | 内容 | 落点 |
|---|---|---|
| B1 | daemon 拉起改为登录无关（SYSTEM/onstart），弃 onlogon | §3.3 |
| B6 | daemon 加 `--listen=192.168.31.186` 绑物理网卡；主机本地组件走文件路径 remote | §3.2 |
| B2 | hook 用 POSIX sh 兼容写法，第二台机器真推验证 | §4.1 |
| B3 | reviewer.json 用临时文件 + rename 原子替换 | §6.1 |
| B4 | 审查超时沿用既有 timeout_seconds（建议 30min） | §6.1 |
| B5 | worker git 安装走离线包静默装 + 失败回滚 | §5 |
| B8 | 补 `scripts/init_central_repo.ps1` 一键初始化 | §8 G1 |
| 膨胀/死循环 | hook 硬拦 >10MB 大文件；驳回 ≥3 次升级人工 | §4.1/§6.1/§9 |

### 11.2 维持（已有拍板约束，不改）

| 项 | 理由 |
|---|---|
| D1 匿名 daemon | 用户已拍板；项目全局（broker 匿名、/api/join 免口令）同为"信任边界=局域网"模型，单独给 git 上 SSH 破坏一致性；防火墙已限子网 |
| hook 放行 main（仅告警） | 匿名下无法区分推送者身份，硬阻断会卡死唯一合法写入口 reviewer_main；追责靠日志 + bus 审计 |

### 11.3 拒绝（过度设计，与场景不匹配）

| 项 | 拒绝理由 |
|---|---|
| 每 agent SSH 密钥对 | 与 D1 匿名模型冲突；密钥分发/轮换成本高，局域网收益不成比例 |
| reviewer 双实例热备 + Redis 租约锁 | 为 LAN 协作工具引入外部依赖；D5 人工切换已满足可用性需求 |
| 审计 Ed25519 签名/merkle 上链 | 无合规需求；bus.db + commit author + --no-ff 拓扑已可追责回滚 |
| 分支改 `task/<agent_id>/<序号>` | 短码与总线 task_id 对齐是 D3 拍板；task_id 由总线集中生成，百级并发碰撞概率约 1e-6 |

