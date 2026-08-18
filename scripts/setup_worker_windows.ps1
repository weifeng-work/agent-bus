# Agent Bus Windows 子设备一键加入（NSSM 服务化版本，Phase 4）
#
# 在目标 Windows 机器上以普通权限 PowerShell 运行即可（非管理员时自动提权）:
#
#   片段一（推荐，让智能体照做）:
#     用 https://github.com/weifeng-work/agent-bus 把 agent-bus 作为工作节点装到我这台
#     Windows。先把 scripts/setup_worker_windows.ps1 下载并运行，加入队伍，再启动一个
#     codebuddy 执行器。
#
#   片段二（手动/直接命令）:
#   irm https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_worker_windows.ps1 | iex
#
# 参数示例（不传时全部自动探测/交互）:
#   powershell -ExecutionPolicy Bypass -File setup_worker_windows.ps1 `
#     -Executor codebuddy
#   powershell -ExecutionPolicy Bypass -File setup_worker_windows.ps1 `
#     -Host 192.168.31.186 -Executor opencode
#
# 队列参数（可选，用于区分不同队伍）:
#   powershell -ExecutionPolicy Bypass -File setup_worker_windows.ps1 `
#     -Queue myteam -Executor codebuddy
#
# 做了什么:
#   1. 检测/安装 Python（winget，3.12）；无 git 也可（直接下载主分支 zip）
#   2. 下载项目到 %LOCALAPPDATA%\agent-bus
#   3. pip 安装依赖（requirements.txt + pywinauto）
#   4. 调用 scripts/join_team.py 发现队伍 → 匿名登记入队 → 验证上线
#   5. 清理旧版计划任务（AgentBusShell / AgentBusShellWatchdog，D5）
#   6. 安装通信节点：注册 NSSM 服务 AgentBusCore + 计划任务 AgentBusTray(onlogon) 启动托盘
#   只出站连 MQTT(1883)+HTTP(8000) 与发现用 UDP(41830)，不开入站端口、无需防火墙配置。
#   需要管理员权限（注册 NSSM 服务需要）。
#
param(
    [string]$HostIP = "",
    [string]$Executor = "codebuddy",
    [string]$Name = "",
    [string]$InstallDir = "$env:LOCALAPPDATA\agent-bus",
    [string]$Queue = "",
    [switch]$EnableShellControl
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Repo = "https://github.com/weifeng-work/agent-bus"
$LogFile = "$env:LOCALAPPDATA\agent-bus-executor.log"

# ---------- 0. 安装目录权限检查（D6：默认 %LOCALAPPDATA%\agent-bus） ----------
# %LOCALAPPDATA% 是用户目录，用户与 SYSTEM 服务均可读写，无需 ACL 调整。
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

Write-Host "== agent-bus Windows 工作节点加入 ==" -ForegroundColor Cyan
Write-Host "  执行器: $Executor   目录: $InstallDir"

# ---------- 1. Python ----------
$py = $null
foreach ($c in @("python", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        $test = if ($c -eq "py") { "py -3" } else { "python" }
        try { & cmd /c "$test --version" 2>$null | Out-Null; if ($LASTEXITCODE -eq 0) { $py = $test; break } } catch {}
    }
}
if (-not $py) {
    Write-Host "  [1/5] 未检测到 Python，通过 winget 安装..." -ForegroundColor Yellow
    winget install --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "winget 安装 Python 失败，请手动安装 Python 3.10+ 后重跑" }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $py = "python"
}
& cmd /c "$py --version"
Write-Host "  [1/5] Python 就绪: $py"

# ---------- 2. 下载项目 ----------
Write-Host "  [2/5] 下载 agent-bus..."
# 先停在跑的旧执行器：进程 cwd 与日志重定向句柄会锁住 $InstallDir，导致覆盖更新失败
Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match '_executor\.py|interactive_executor\.py' } |
    ForEach-Object {
        Write-Host "  停止旧执行器 PID $($_.ProcessId)" -ForegroundColor DarkGray
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep 1
# B6：保留 data/ 目录（运行时状态：state.json、control.log 等），与 remote_update 策略一致
if (Test-Path $InstallDir) {
    $dataBackup = "$env:TEMP\ab-data-backup"
    if (Test-Path "$InstallDir\data") {
        if (Test-Path $dataBackup) { Remove-Item $dataBackup -Recurse -Force }
        Move-Item "$InstallDir\data" $dataBackup -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
}
$zip = "$env:TEMP\agent-bus.zip"
$dl = $false
foreach ($u in @(
    "$Repo/archive/refs/heads/main.zip",
    "https://ghproxy.net/$Repo/archive/refs/heads/main.zip"
)) {
    try { Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing; $dl = $true; break } catch {}
}
if (-not $dl) { throw "下载失败：GitHub 与镜像均不可达。可在能上网的机器下载 main.zip 后拷贝为 $zip 重跑" }
Expand-Archive -Path $zip -DestinationPath "$env:TEMP\agent-bus-extract" -Force
Move-Item "$env:TEMP\agent-bus-extract\agent-bus-main" $InstallDir
# B6：恢复 data/ 目录
if (Test-Path "$env:TEMP\ab-data-backup") {
    Move-Item "$env:TEMP\ab-data-backup" "$InstallDir\data" -Force -ErrorAction SilentlyContinue
    Write-Host "  保留 data/（运行时状态）"
}
Remove-Item "$env:TEMP\agent-bus-extract", $zip -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  [2/5] 代码就绪: $InstallDir"

# ---------- 3. 依赖 ----------
Write-Host "  [3/5] 安装 Python 依赖（requirements.txt）..."
& cmd /c "$py -m pip install --quiet -r $InstallDir\requirements.txt pywinauto"
if ($LASTEXITCODE -ne 0) { throw "pip 安装失败，检查网络后重跑" }
Write-Host "  [3/5] 依赖就绪"

# ---------- 4. 加入队伍 ----------
Write-Host "  [4/5] 加入队伍..."
$hostArg = if ($HostIP) { "--host $HostIP" } else { "" }
$nameArg = if ($Name) { '--name "' + $Name + '"' } else { "" }
Push-Location $InstallDir
try {
    & cmd /c "cd /d $InstallDir && $py scripts\join_team.py $hostArg $nameArg"
    if ($LASTEXITCODE -ne 0) { throw "加入队伍失败（exit=$LASTEXITCODE）" }
} finally {
    Pop-Location
}
Write-Host "  [4/5] 已加入队伍"

# ---------- 5. 清理旧版计划任务（D5：强制清理，容忍失败） ----------
Write-Host "  [5/6] 清理旧版计划任务..."
$oldTaskNames = @("AgentBusShell", "AgentBusShellWatchdog")
# 也用通配符清理可能残留的旧版任务
foreach ($taskName in $oldTaskNames) {
    $t = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($t) {
        Write-Host "  清理旧计划任务: $taskName" -ForegroundColor Yellow
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    }
}
# 额外清理：旧版执行器直启任务
$oldExecTask = Get-ScheduledTask -TaskName "AgentBus$Executor" -ErrorAction SilentlyContinue
if ($oldExecTask) {
    Unregister-ScheduledTask -TaskName "AgentBus$Executor" -Confirm:$false -ErrorAction SilentlyContinue
}
Write-Host "  [5/6] 旧计划任务清理完成"

# ---------- 6. 安装通信节点（NSSM 服务 + 托盘 UI） ----------
Write-Host "  [6/6] 安装通信节点（NSSM 服务 AgentBusCore + 托盘 UI）..."
# 从加入写出的设备身份读取 agent_id（host-xxxx），保证稳定
$devJson = "$env:USERPROFILE\.config\agent-bus\device.json"
$agentId = ""
if (Test-Path $devJson) {
    try { $agentId = (Get-Content $devJson -Raw | ConvertFrom-Json).agent_id } catch {}
}
if (-not $agentId) { $agentId = $Executor + "_" + ($env:COMPUTERNAME.ToLower() -replace '[^a-z0-9]', '') }
if (-not $Name)    { $Name = ($Executor.Substring(0,1).ToUpper() + $Executor.Substring(1)) + "@$env:COMPUTERNAME" }

# 委托 setup_tray.ps1（新版：注册 NSSM 服务 + 计划任务 AgentBusTray + 启动）
& "$InstallDir\scripts\setup_tray.ps1" -InstallDir $InstallDir -Executor $Executor `
    -AgentId $agentId -Name $Name -Queue $Queue `
    -EnableShellControl:$EnableShellControl
if ($LASTEXITCODE -ne 0) { throw "通信节点安装失败（exit=$LASTEXITCODE）" }
Start-Sleep 5

Write-Host ""
Write-Host "== 加入完成 ==" -ForegroundColor Green
Write-Host "  agent_id : $agentId"
Write-Host "  服务     : AgentBusCore（NSSM，自动启动，崩溃 5s 重启）"
Write-Host "  托盘 UI  : AgentBusTray 计划任务（onlogon，登录即起）"
Write-Host "  在线验证 : 打开主机面板（http://<主机IP>:8000/）应看到 [$agentId] 在线"
Write-Host "  日志     : $LogFile"
Write-Host "  管理命令 : python scripts\agent_service.py status/stop/start/remove"