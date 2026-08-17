# Agent Bus 通信节点（托盘壳）安装 —— 计划任务注册 + 启动（需求1 三层自愈）
#
# 做什么:
#   1. 读取设备身份（~/.config/agent-bus/device.json，join_team 写入）
#   2. 生成 start_tray.bat（以用户会话启动 comm_node.py）
#   3. 删除旧版直启执行器任务 AgentBus<Executor>（升级兼容）
#   4. 注册计划任务（pythonw 直启 + 隐藏，无黑窗）:
#        AgentBusShell          /sc onlogon      → pythonw executor\comm_node.py（登录即起）
#        AgentBusShellWatchdog  /sc minute /mo 1 → pythonw scripts\watchdog.py（分钟级兜底）
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

# ---------- 0.2 解析 pythonw（无控制台窗口，避免受控机弹黑窗） ----------
function Resolve-PythonW {
    $c = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $c = Get-Command pyw -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    if ($py -eq "py") { return "pyw" }
    return "pythonw"
}
$pyw = Resolve-PythonW

# ---------- 0.5 安装目录权限自适应（与 setup_worker 一致） ----------
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
    if ($InstallDir -eq "C:\agent-bus") {
        $InstallDir = "$env:LOCALAPPDATA\agent-bus"
        Write-Host "  C:\agent-bus 不可写（普通权限），自动改用: $InstallDir" -ForegroundColor Yellow
    } else {
        throw "安装目录不可写: $InstallDir（请改用用户可写目录，如 %LOCALAPPDATA%\agent-bus）"
    }
}

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

# ---------- 2. 启动参数（单一来源：任务 / 批处理 / launch json 共用） ----------
$logFile = "$InstallDir\data\tray_shell.log"
# 节点身份与执行器身份分离（架构 §3）：node-<agent> 收控制消息，执行器用原 agent_id
$nodeId = "node-$AgentId"
$shellArgs = @(
    "--role", "worker",
    "--agent-id", $nodeId,
    "--executor-agent-id", $AgentId,
    "--name", $Name,
    "--executor", $Executor,
    "--install-dir", $InstallDir
)
if ($EnableShellControl) { $shellArgs += "--enable-shell-control" }
# 含空格参数加引号
$shellArgsStr = ($shellArgs | ForEach-Object {
    if ($_ -match '\s') { "`"$_`"" } else { $_ }
}) -join " "

# start_tray.bat（仅手动双击用，已改用 pythonw，不弹黑窗）
$bat = @"
@echo off
cd /d $InstallDir
start "" pythonw.exe executor\comm_node.py $shellArgsStr > "$logFile" 2>&1
"@
Set-Content -Path "$InstallDir\start_tray.bat" -Value $bat -Encoding ASCII
Write-Host "  已生成: $InstallDir\start_tray.bat（节点身份 $nodeId，pythonw 无窗口）"

# shell_launch.json：供 watchdog / 远程更新无窗口重启（pythonw 直启，不经过 .bat/cmd）
$runtimeDir = "$InstallDir\data\runtime"
if (-not (Test-Path $runtimeDir)) { New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null }
$launchSpec = [ordered]@{
    exe  = $pyw
    args = $shellArgs
    cwd  = $InstallDir
} | ConvertTo-Json -Compress
Set-Content -Path "$runtimeDir\shell_launch.json" -Value $launchSpec -Encoding UTF8
Write-Host "  已生成: $runtimeDir\shell_launch.json（watchdog 无窗口重启用）"

# ---------- 2.5 一次性配对（若给了 -PairCode；成功后密码即作废，不落盘不残留） ----------
if ($PairCode) {
    Write-Host "  配对中（一次性密码）..."
    & cmd /c "cd /d $InstallDir && $pyw executor\comm_node.py --role worker --agent-id $nodeId --headless --no-bus --pair-code `"$PairCode`" --test-seconds 1"
    $ctrlFile = "$env:USERPROFILE\.config\agent-bus\control.json"
    $paired = $false
    if (Test-Path $ctrlFile) {
        try { $paired = ((Get-Content $ctrlFile -Raw | ConvertFrom-Json).agent_id -eq $nodeId) } catch {}
    }
    if ($paired) {
        Write-Host "  配对成功 ✓（控制面已激活，密码已作废）" -ForegroundColor Green
    } else {
        Write-Host "  配对未完成（密码无效/过期？）：节点照常运行，装完可在托盘『输入配对码』补配对" -ForegroundColor Yellow
    }
}

# ---------- 3. 删除旧版直启任务（升级兼容） ----------
schtasks /delete /tn "AgentBus$Executor" /f 2>$null | Out-Null

# ---------- 4. 注册计划任务（pythonw 直启 + 隐藏，杜绝黑窗） ----------
# 用工件命令直接注册，不经过 .bat / cmd，避免任何控制台窗口。
$shellCmd = "`"$pyw`" executor\comm_node.py $shellArgsStr"
$wdCmd    = "`"$pyw`" scripts\watchdog.py --install-dir `"$InstallDir`""
schtasks /create /tn "AgentBusShell" /tr $shellCmd /sc onlogon /f | Out-Null
schtasks /create /tn "AgentBusShellWatchdog" /tr $wdCmd /sc minute /mo 1 /f | Out-Null
# 设为隐藏运行（任务计划程序里不弹窗）
function Set-TaskHidden([string]$n) {
    try {
        $t = Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue
        if ($t) { $t.Settings.Hidden = $true; $t | Set-ScheduledTask | Out-Null }
    } catch {}
}
Set-TaskHidden "AgentBusShell"
Set-TaskHidden "AgentBusShellWatchdog"
Write-Host "  计划任务: AgentBusShell(onlogon, 隐藏) + AgentBusShellWatchdog(每分钟, 隐藏)"

# ---------- 5. 启动托盘壳（pythonw 直启，隐藏窗口） ----------
# 幂等：先清理本机已有节点进程，避免重复实例（重装/升级场景）
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'comm_node|_executor\.py' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 1
Start-Process -FilePath $pyw -ArgumentList $shellArgs -WindowStyle Hidden -WorkingDirectory $InstallDir
Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "  托盘壳已启动（状态灯: 绿=已连接 / 黄=重连中 / 灰=已停止）"
Write-Host "  日志: $logFile"
if ($PairCode)  { Write-Host "  配对码: $PairCode（M3 控制面配对预留）" }
if ($EnableShellControl) { Write-Host "  shell 受控能力: 已开启（M3 生效）" }
