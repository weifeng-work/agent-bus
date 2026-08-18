# Agent Bus 通信节点（服务化 + 托盘）安装 —— Phase 4（NSSM 服务 + 开始菜单快捷方式）
#
# 做什么:
#   1. 读取设备身份（~/.config/agent-bus/device.json，join_team 写入）
#   2. 生成 start_tray.bat + 开始菜单快捷方式（恢复托盘用）
#   3. 删除旧版直启任务（升级兼容）
#   4. 注册 NSSM 服务 AgentBusCore（core_node 后台服务，自动启动 + 崩溃重启）
#   5. 清理旧版计划任务 AgentBusShell / AgentBusShellWatchdog
#   6. 创建开始菜单快捷方式（替代计划任务拉起托盘）
#   7. 启动服务 + 托盘 UI
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\setup_tray.ps1 `
#     -InstallDir <dir> -Executor codebuddy -AgentId host-xxxx
#   非管理员权限时自动提重运行（注册 NSSM 服务需要）
#
# 参数:
#   -Queue              队列标识（可选，用于区分不同队伍）
#   -EnableShellControl 安装后即开启 shell 受控能力（默认关）
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\agent-bus",
    [string]$Executor = "codebuddy",
    [string]$AgentId = "",
    [string]$Name = "",
    [string]$Queue = "",
    [switch]$EnableShellControl
)

$ErrorActionPreference = "Stop"

# ---------- 0. 管理员权限检查 ----------
# 注册 NSSM 服务需要管理员权限，非管理员时自动提权重启
if (-NOT ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")) {
    Write-Host "  需要管理员权限，正在提权重启..." -ForegroundColor Yellow
    $myArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    foreach ($arg in $MyInvocation.BoundParameters.Keys) {
        $val = $MyInvocation.BoundParameters[$arg]
        if ($val -is [switch]) { if ($val) { $myArgs += " -$arg" } }
        else { $myArgs += " -$arg `"$val`"" }
    }
    Start-Process powershell -Verb RunAs -ArgumentList $myArgs
    exit
}

# ---------- 0. Python 检测 ----------
$py = $null
foreach ($c in @("python", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        $test = if ($c -eq "py") { "py -3" } else { "python" }
        try { & cmd /c "$test --version" 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $py = $test; break } } catch {}
    }
}
if (-not $py) { throw "未找到 Python 3.10+，请先安装" }

# ---------- 0.2 解析 pythonw ----------
function Resolve-PythonW {
    $c = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $c = Get-Command pyw -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    if ($py -eq "py") { return "pyw" }
    return "pythonw"
}
$pyw = Resolve-PythonW

# ---------- 0.5 安装目录权限检查（D6：默认 %LOCALAPPDATA%\agent-bus） ----------
# %LOCALAPPDATA% 是用户目录，用户与 SYSTEM 服务均可读写，无需 ACL 调整
function Test-DirWritable([string]$dir) {
    try {
        $parent = if (Test-Path $dir) { $dir } else { Split-Path $dir -Parent }
        $probe = Join-Path $parent ("_wprobe_" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $probe -Force -ErrorAction Stop | Out-Null
        Remove-Item $probe -Force -ErrorAction SilentlyContinue
        return $true
    } catch { return $false }
}
if (-not (Test-DirWritable $InstallDir)) {
    throw "安装目录不可写: $InstallDir（请指定一个用户可写的目录）"
}

# ---------- 1. 设备身份 ----------
$devJson = "$env:USERPROFILE\.config\agent-bus\device.json"
if (-not $AgentId -and (Test-Path $devJson)) {
    try { $AgentId = (Get-Content $devJson -Raw | ConvertFrom-Json).agent_id } catch {}
}
if (-not $AgentId) { throw "未找到 agent_id：请先运行 join_team.py 入队，或传 -AgentId" }
if (-not $Name) { $Name = "Node@$env:COMPUTERNAME" }
$nodeId = "node-$AgentId"

Write-Host "== Agent Bus 通信节点安装（服务化模式）==" -ForegroundColor Cyan
Write-Host "  agent_id : $AgentId"
Write-Host "  node_id  : $nodeId"
Write-Host "  executor : $Executor"
if ($Queue) { Write-Host "  queue    : $Queue" }

# ---------- 2. 生成托盘 UI 启动参数 ----------
$trayLogFile = "$InstallDir\data\tray_shell.log"
$trayArgs = @(
    "executor\tray_app.py",
    "--install-dir", $InstallDir,
    "--role", "worker",
    "--agent-id", $nodeId
)
$trayArgsStr = ($trayArgs | ForEach-Object {
    if ($_ -match '\s') { "`"$_`"" } else { $_ }
}) -join " "

# start_tray.bat（手动双击恢复托盘用）
$bat = @"
@echo off
cd /d $InstallDir
start "" pythonw.exe $trayArgsStr > "$trayLogFile" 2>&1
"@
Set-Content -Path "$InstallDir\start_tray.bat" -Value $bat -Encoding ASCII
Write-Host "  已生成: $InstallDir\start_tray.bat（托盘 UI 恢复入口）"

# ---------- 3. 旧版清理 ----------
# 3a. 清理旧版直启执行器任务
$oldTask = Get-ScheduledTask -TaskName "AgentBus$Executor" -ErrorAction SilentlyContinue
if ($oldTask) {
    Unregister-ScheduledTask -TaskName "AgentBus$Executor" -Confirm:$false -ErrorAction SilentlyContinue
}

# 3b. 清理旧版计划任务 AgentBusShell / AgentBusShellWatchdog（D5）
$oldTasks = @("AgentBusShell", "AgentBusShellWatchdog")
foreach ($tn in $oldTasks) {
    $t = Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue
    if ($t) {
        Write-Host "  清理旧计划任务: $tn" -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $tn -Confirm:$false -ErrorAction SilentlyContinue
    }
}

# ---------- 4. 注册 NSSM 服务 AgentBusCore ----------
Write-Host "  [4/6] 注册 NSSM 服务 AgentBusCore..." -ForegroundColor Cyan
$nssm = "$InstallDir\scripts\_dl\nssm.exe"
if (-not (Test-Path $nssm)) {
    throw "NSSM 未找到: $nssm（请确保项目完整，或重新下载）"
}

# 构建 agent_service.py 参数（作为服务入口）
$serviceArgs = @(
    "$InstallDir\scripts\agent_service.py",
    "--role", "worker",
    "--agent-id", $nodeId,
    "--install-dir", $InstallDir,
    "--executor", $Executor,
    "--executor-agent-id", $AgentId
)
if ($Queue) { $serviceArgs += "--queue"; $serviceArgs += $Queue }
if ($EnableShellControl) { $serviceArgs += "--enable-shell-control" }
$serviceArgsStr = ($serviceArgs | ForEach-Object {
    if ($_ -match '\s') { "`"$_`"" } else { $_ }
}) -join " "

# 先确保旧服务已移除
& $nssm stop AgentBusCore confirm 2>$null | Out-Null
& $nssm remove AgentBusCore confirm 2>$null | Out-Null
Start-Sleep 1

# 安装服务
& $nssm install AgentBusCore $py $serviceArgsStr 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "NSSM 安装服务失败（需要管理员权限）" }

# 配置 NSSM 参数
& $nssm set AgentBusCore AppDirectory $InstallDir 2>&1 | Out-Null
& $nssm set AgentBusCore Start SERVICE_AUTO_START 2>&1 | Out-Null
& $nssm set AgentBusCore AppExit Default Restart 2>&1 | Out-Null
& $nssm set AgentBusCore AppRestartDelay 5000 2>&1 | Out-Null
& $nssm set AgentBusCore AppStdout "$InstallDir\data\service.log" 2>&1 | Out-Null
& $nssm set AgentBusCore AppStderr "$InstallDir\data\service.err.log" 2>&1 | Out-Null
Write-Host "  服务 AgentBusCore 已注册（自动启动，崩溃 5s 后重启）"

# ---------- 5. 创建开始菜单快捷方式（代替计划任务拉起托盘） ----------
Write-Host "  [5/6] 创建开始菜单快捷方式..." -ForegroundColor Cyan
$startMenuDir = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Agent Bus"
if (-not (Test-Path $startMenuDir)) { New-Item -ItemType Directory -Path $startMenuDir -Force | Out-Null }
$shortcutPath = "$startMenuDir\Agent Bus Tray.lnk"
$wshell = New-Object -ComObject WScript.Shell
$shortcut = $wshell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pyw
$shortcut.Arguments = $trayArgsStr
$shortcut.WorkingDirectory = $InstallDir
$shortcut.Description = "Agent Bus 托盘 UI - 受控节点状态面板"
$shortcut.Save()
Write-Host "  开始菜单快捷方式: $shortcutPath"

# 也注册计划任务（onlogon 自动启动托盘，作为备用拉起方式）
$trayAction = New-ScheduledTaskAction -Execute $pyw -Argument $trayArgsStr -WorkingDirectory $InstallDir
$trayTrigger = New-ScheduledTaskTrigger -AtLogOn
$traySettings = New-ScheduledTaskSettingsSet -Hidden `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)
Register-ScheduledTask -TaskName "AgentBusTray" `
    -Action $trayAction -Trigger $trayTrigger -Settings $traySettings -Force | Out-Null
Write-Host "  计划任务: AgentBusTray(onlogon, 隐藏，备用)"

# 写入 state.json 初始状态（active）
$runtimeDir = "$InstallDir\data\runtime"
if (-not (Test-Path $runtimeDir)) { New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null }
$stateInit = '{"state": "active", "updated_at": ' + (Get-Date -UFormat %s) + '}'
Set-Content -Path "$runtimeDir\state.json" -Value $stateInit -Encoding UTF8
Write-Host "  状态机: state.json -> active"

# ---------- 6. 启动服务 + 托盘 ----------
Write-Host "  [6/6] 启动服务与托盘..." -ForegroundColor Cyan

# 启动 NSSM 服务
& $nssm start AgentBusCore 2>&1 | Out-Null
Start-Sleep 2

# 启动托盘 UI（用户会话）
# 先清理旧版托盘进程
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'tray_app|comm_node|_executor\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 1
Start-Process -FilePath $pyw -ArgumentList $trayArgs -WindowStyle Hidden -WorkingDirectory $InstallDir

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "  服务 AgentBusCore: 运行中（自动启动，崩溃 5s 重启）"
Write-Host "  托盘 UI: 已启动（状态灯: 绿=已连接 / 黄=重连中 / 灰=已停止）"
Write-Host "  服务日志: $InstallDir\data\service.log"
Write-Host "  托盘日志: $trayLogFile"
Write-Host "  开始菜单: 开始菜单 → Agent Bus → Agent Bus Tray（恢复托盘用）"
Write-Host "  管理命令: python scripts\agent_service.py status/stop/start/remove
if ($Queue) { Write-Host "  队列: $Queue" }
if ($EnableShellControl) { Write-Host "  shell 受控能力: 已开启" }