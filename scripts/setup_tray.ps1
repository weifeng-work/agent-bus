# Agent Bus 通信节点（托盘壳）安装 —— 计划任务注册 + 启动（需求1 三层自愈）
#
# 做什么:
#   1. 读取设备身份（~/.config/agent-bus/device.json，join_team 写入）
#   2. 生成 start_tray.bat（以用户会话启动 comm_node.py）
#   3. 删除旧版直启执行器任务 AgentBus<Executor>（升级兼容）
#   4. 注册计划任务:
#        AgentBusShell          /sc onlogon      → start_tray.bat（登录即起）
#        AgentBusShellWatchdog  /sc minute /mo 1 → scripts/watchdog.py（分钟级兜底）
#   5. 立即启动托盘壳
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_tray.ps1 `
#     -InstallDir C:\agent-bus -Executor codebuddy
#
# 参数（M3 起生效）:
#   -PairCode           主控面板生成的一次性安装码（配对数）
#   -EnableShellControl 安装后即开启 shell 受控能力（默认关）
param(
    [string]$InstallDir = "C:\agent-bus",
    [string]$Executor = "codebuddy",
    [string]$AgentId = "",
    [string]$Name = "",
    [string]$PairCode = "",
    [switch]$EnableShellControl
)

$ErrorActionPreference = "Stop"

# ---------- 0. Python 检测（与 setup_worker_windows.ps1 一致） ----------
$py = $null
foreach ($c in @("python", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        $test = if ($c -eq "py") { "py -3" } else { "python" }
        try { & cmd /c "$test --version" 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $py = $test; break } } catch {}
    }
}
if (-not $py) { throw "未找到 Python 3.10+，请先安装（可运行 setup_worker_windows.ps1 自动安装）" }

# ---------- 1. 设备身份 ----------
$devJson = "$env:USERPROFILE\.config\agent-bus\device.json"
if (-not $AgentId -and (Test-Path $devJson)) {
    try { $AgentId = (Get-Content $devJson -Raw | ConvertFrom-Json).agent_id } catch {}
}
if (-not $AgentId) { throw "未找到 agent_id：请先运行 join_team.py 入队，或传 -AgentId" }
if (-not $Name) { $Name = "Node@$env:COMPUTERNAME" }

Write-Host "== Agent Bus 通信节点安装 ==" -ForegroundColor Cyan
Write-Host "  agent_id : $AgentId"
Write-Host "  executor : $Executor"

# ---------- 2. start_tray.bat ----------
$logFile = "$InstallDir\data\tray_shell.log"
$bat = @"
@echo off
cd /d $InstallDir
start "" /min cmd /c "$py executor\comm_node.py --role worker --agent-id $AgentId --name `"$Name`" --executor $Executor --install-dir $InstallDir > `"$logFile`" 2>&1"
"@
Set-Content -Path "$InstallDir\start_tray.bat" -Value $bat -Encoding ASCII
Write-Host "  已生成: $InstallDir\start_tray.bat"

# ---------- 3. 删除旧版直启任务（升级兼容） ----------
schtasks /delete /tn "AgentBus$Executor" /f 2>$null | Out-Null

# ---------- 4. 注册计划任务 ----------
# watchdog 走独立 bat（避免 schtasks /tr 引号嵌套问题）
$wdBat = @"
@echo off
cd /d $InstallDir
$py scripts\watchdog.py --install-dir $InstallDir
"@
Set-Content -Path "$InstallDir\watchdog.bat" -Value $wdBat -Encoding ASCII
schtasks /create /tn "AgentBusShell" /tr "`"$InstallDir\start_tray.bat`"" /sc onlogon /f | Out-Null
schtasks /create /tn "AgentBusShellWatchdog" /tr "`"$InstallDir\watchdog.bat`"" /sc minute /mo 1 /f | Out-Null
Write-Host "  计划任务: AgentBusShell(onlogon) + AgentBusShellWatchdog(每分钟)"

# ---------- 5. 启动托盘壳 ----------
Start-Process "$InstallDir\start_tray.bat"
Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "  托盘壳已启动（状态灯: 绿=已连接 / 黄=重连中 / 灰=已停止）"
Write-Host "  日志: $logFile"
if ($PairCode)  { Write-Host "  配对码: $PairCode（M3 控制面配对预留）" }
if ($EnableShellControl) { Write-Host "  shell 受控能力: 已开启（M3 生效）" }
