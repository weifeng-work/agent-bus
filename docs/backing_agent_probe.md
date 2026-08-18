# 宿主智能体可用性探测方案（Readiness Probe）

> 版本：v0.1（待评审）
> 状态：待评审，未施工
> 关联：需求清单.md（评审通过后记为新需求条目）；docs/protocol.md（health 状态与错误分类约定）
> 既有基础（本方案是扩展而非新建）：
> - `agent_bus/client.py`：`set_health(state)` + 心跳携带 health（30s/拍），bus_server 已落库并暴露给面板
> - `codebuddy_executor.py`：auth_required 识别→标记→60s 探测恢复闭环（2026-08-17 交付），错误串已用 `分类词: 详情` 格式

---

## 1. 背景与问题

**缺口：执行器注册 ≠ 宿主智能体可用。** 执行器只是桥接进程，其背后的宿主（CodeBuddy/OpenCode CLI、WorkBuddy/Trae 桌面端）是否安装、是否运行、是否登录，系统没有任何事实记录。安装脚本（`setup_worker_windows.ps1`）不装不探，可用性只在任务执行时以报错形式暴露。

现状失效模式盘点：

| 执行器 | 宿主缺失时的现状行为 | 问题 |
|---|---|---|
| codebuddy 一次性 | `find_codebuddy` 抛 FileNotFoundError → 任务错误回传 | 较快，但注册前不探测，面板上节点"看起来正常"；错误未分类 |
| opencode 一次性 | 同上（同款 which 查找） | 同上 |
| 交互式（codebuddy/opencode） | 会话创建成功 → 启动命令在窗格 shell 里 not found → 屏幕静止 → **"静止 30s 视为就绪"兜底误判** → 注入打在空 shell 上 → 拖到任务超时 | 最慢（240s 级）且误判，是本方案首要修复对象 |
| workbuddy | `_connect_window` 20s 超时 → 错误回传 | 秒级尚可，但错误无分类（分不清没装/没启动），无 health 标记 |
| traework | CDP 连接失败 → 错误回传 | 同上 |

**连带问题**：发起方（人或 hub）派发时无法区分"节点忙"与"节点根本没有宿主"，无法程序化换节点重试。

## 2. 目标与非目标

**目标**
1. 注册前探测宿主可用性，结果如实上报（面板/总线可见）。
2. 宿主缺失时任务**快速失败**并返回**分类错误**，发起方可程序化处理。
3. 消灭交互式执行器"空 shell 误判就绪"的慢速失败。
4. 探测失败**不退出进程**（避免托盘监督循环 2s 重启风暴），常驻 + 标记 + 复探自愈，与 auth_required 恢复模型一致。

**非目标**
- 远程修复（经 shell 控制面自动安装/拉起宿主）：依赖 M5 远程运维编排，本次不做。
- 派发侧自动过滤/重路由：现阶段派发是发起方指名 `--to`，状态可见即可，路由策略后置。

## 3. 总体设计

三层闭环：

```
探测（probe）          上报（health 通道）           快速失败（任务路径）
注册前探宿主    →   register/heartbeat 带 health   →   任务到达时兜底复检
心跳周期复探    →   bus_server 落库 → 面板可见          失败即回 task_result
                                                   error = "分类词: 详情"
```

### 3.1 关键决策：复用 health 通道，不新增 backing 字段

health 机制（register 字段、心跳携带、服务端落库、面板展示、`set_health` 推送）已全链路打通，且 codebuddy 执行器已用它表达"登录态"这一宿主可用性维度。宿主"存在性"是同一语义家族，扩展状态枚举即可，零协议结构改动。备选（独立 backing 字段）见 §10-Q5。

### 3.2 health 状态枚举（扩展）

| 状态 | 含义 | 现状 |
|---|---|---|
| `ok` | 宿主可用 | 已有 |
| `auth_required` | 宿主在但未登录 | 已有 |
| `agent_missing` | 宿主未检出（CLI 不在 PATH / GUI 无法定位） | **新增** |
| `unknown` | 未探测或探测无法定论（GUI 冷启动类） | 已有 |

语义约定：`agent_missing` 只用于**能明确判定缺失**的场景；GUI 类宿主因存在冷启动自愈（deeplink 可拉起），探测无法区分"没装"和"没启动"时保持 `unknown`，把判定推迟到任务路径。

### 3.3 错误分类词约定（task_result.error）

沿用既有格式 `分类词: 人类可读详情`（与 auth_required 先例一致）：

| 分类词 | 触发场景 |
|---|---|
| `agent_not_installed` | CLI 不在 PATH / 版本探测失败（明确没装） |
| `agent_not_running` | GUI 窗口等待超时、CDP 端口未监听（装了没启动） |
| `auth_required` | 未登录（已有） |
| `agent_permission_denied` | UIPI 输入拦截（err=5，WorkBuddy 以管理员运行） |

## 4. 各执行器探测策略

| 执行器 | 注册前探测 | 任务路径兜底 | 探测成本 |
|---|---|---|---|
| codebuddy 一次性 | which + `--version`（子进程，超时包裹） | FileNotFoundError → `agent_not_installed` | 低（秒级） |
| opencode 一次性 | 同上 | 同上 | 低 |
| 交互式 | 同上（启动前探一次） | `_wait_tui_ready` 检出 not-found 特征 → 立即失败分类（见 §5） | 低 |
| workbuddy | **不做**（冷启动可自愈，探测易误报，见 §10-Q2） | 窗口等待超时 → `agent_not_running`；UIPI → `agent_permission_denied` | — |
| traework | socket 探测 127.0.0.1:9433（1s 超时） | CDP 连接失败 → `agent_not_running` | 极低 |

心跳复探：每拍（30s）只做 which/socket 级轻探；`--version` 重探仅在状态迁移时触发（missing→尝试恢复），避免 CLI 卡死拖累心跳。

## 5. 模块改动清单

| 文件 | 改动 | 估计行数 |
|---|---|---|
| `executor/agent_probe.py`（新增） | `probe_cli(name)`（which+version，超时）、`probe_port(host, port)`、not-found 屏幕特征正则集（含中英文 shell：not recognized / not found / 找不到命令） | ~60 |
| `agent_bus/client.py` | `set_health` 白名单加入 `agent_missing` | ~2 |
| `executor/codebuddy_executor.py` | 启动探测 → `set_health`；任务失败错误分类 | ~15 |
| `executor/opencode_executor.py` | 同上 | ~15 |
| `executor/interactive_executor.py` | 启动探测；`_wait_tui_ready` 增加 not-found 特征检测分支（命中即抛分类错误，**不再等 30s 静止兜底**）；`_ensure_session` 失败回传分类错误 | ~30 |
| `executor/workbuddy_executor.py` | 错误分类（窗口超时/UIPI），无探测 | ~10 |
| `executor/traework_executor.py` | 端口探测 + 错误分类 | ~10 |
| `docs/protocol.md` | health 状态枚举表、错误分类词约定 | 文档 |
| `server/static/index.html` | health 徽章适配 `agent_missing`（若现为原样文本渲染则零改动，施工时确认） | ≤5 |
| `server/bus_server.py` | **零改动**（health 通道已通） | 0 |

## 6. 工作流程

**注册时**：执行器构造 → 探测宿主 → `health = ok / agent_missing / unknown` → `connect(register=True)`（无论结果都注册）→ 日志记录探测详情。

**运行中**：心跳每拍轻探 → 状态变化即 `set_health` 推送（bus_server 经心跳刷新 agents 表，面板随之变化）；`agent_missing` 状态下收到任务 → 立即回 `agent_not_installed` 分类错误（不做无谓尝试），但 CLI 类允许先复检一次（可能刚装好）。

**任务路径（交互式关键修复）**：`_wait_tui_ready` 在轮询中并行匹配 not-found 特征——命中 → 立刻终止启动流程，teardown 会话，任务回 `agent_not_installed`（秒级，替代 240s 超时）；未命中则走原就绪状态机，30s 静止兜底保留（profile 漏配场景仍需要它）。

## 7. 兼容与风险

- 状态枚举扩展向后兼容：旧执行器不发 `agent_missing`，不受影响；面板若遇未知状态按文本原样渲染（施工前确认）。
- 无重启风暴：探测失败进程常驻，与 auth_required 恢复模型同构。
- 探测副作用：which/version 只读；UIA 窗口探测只读不抢焦点；版本探测全程超时包裹（npm shim 可能卡）。
- `_wait_tui_ready` 新分支误杀风险：not-found 特征须锚定在**启动命令发出之后**的屏幕增量上，避免与 TUI 自身输出中的同词文本碰撞（如代码内容恰好含 "not found"）——实现时以"命令回显行之后 N 行内出现特征且无任何就绪征兆"为判定。

## 8. 测试与验收

1. 单元：`probe_cli`/`probe_port` 对真实存在/不存在的命令与端口各打一遍。
2. 组件（缺失路径）：交互式执行器指向不存在的 CLI（临时 profile 或 PATH 屏蔽）→ 注册后 agents API 显示 `agent_missing` → 派任务秒级回 `agent_not_installed`（验收硬指标：**<15s**，对照现状 240s）。
3. E2E（正常路径回归）：codebuddy 一次性 + 交互式各跑一单，health=ok，任务成功，可视窗口链路（需求 4）不受影响。
4. 负向回归：auth_required 闭环不受本次改动影响（codebuddy 登出场景抽测）。

## 9. 工作项清单（逐项确认后施工）

| 编号 | 工作项 | 建议 | 确认 |
|---|---|---|---|
| W1 | `agent_probe.py` + client 白名单（基础件） | 做 | ☐ |
| W2 | codebuddy/opencode 一次性执行器接入探测+分类 | 做 | ☐ |
| W3 | 交互式执行器接入（含 `_wait_tui_ready` 收紧，本方案核心价值点） | 做 | ☐ |
| W4 | workbuddy/traework 错误分类（无探测，仅分类） | 做 | ☐ |
| W5 | protocol.md 修订 + 面板徽章适配 | 做（随 W1-W4 同步） | ☐ |
| W6 | 验证（§8 全部四项） | 做 | ☐ |

## 10. 待确认决策清单

| 编号 | 决策点 | 建议 | 取舍 |
|---|---|---|---|
| Q1 | health 新状态命名 | `agent_missing` | 备选 `backing_missing` |
| Q2 | workbuddy 是否做注册前探测 | 不做（冷启动自愈，易误报） | 做 = 面板更早暴露，但"没启动"会被误标 |
| Q3 | 心跳复探强度 | 每拍轻探（which/socket），重探仅在状态迁移 | 每拍重探更实时但可能拖心跳 |
| Q4 | 面板展示粒度 | 仅 health 徽章着色 | 备选：加"探测详情/时间"列 |
| Q5 | 错误分类的协议形态 | 维持 `分类词: 详情` 字符串约定（零协议改动，与先例一致） | 备选：task_result 增 `error_code` 字段（更干净但要动协议+所有消费方） |
