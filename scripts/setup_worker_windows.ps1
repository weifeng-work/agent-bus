# Agent Bus Windows 子设备一键加入（通用智能体执行器）
#
# 在目标 Windows 机器上以【普通权限】PowerShell 运行（目标机上智能体给一句提示词即可触发）:
#
#   片段一（推荐，让智能体照做）—— 用户只需对本机智能体说：
#     "用 https://github.com/weifeng-work/agent-bus 把 agent-bus 作为工作节点装到我这台
#      Windows。先把 scripts/setup_worker_windows.ps1 下载并运行，加入队伍，再启动一个
#      codebuddy 执行器。队伍口令是：<口令>"
#
#   片段二（手动/直接命令）:
#   irm https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/setup_worker_windows.ps1 | iex
#
# 参数示例（不传时全部自动探测/交互）:
#   powershell -ExecutionPolicy Bypass -File setup_worker_windows.ps1 `
#     -Passphrase "我的队伍口令" -Executor codebuddy
#   powershell -ExecutionPolicy Bypass -File setup_worker_windows.ps1 `
#     -Host 192.168.31.186 -Passphrase "口令" -Executor opencode
#
# 做了什么:
#   1. 检测/安装 Python（winget，3.12）；无 git 也可（直接下载主分支 zip）
#   2. 下载项目到 C:\agent-bus
#   3. pip 安装依赖（requirements.txt + pywinauto）
#   4. 调用 scripts/join_team.py 发现队伍 → 输口令 → 拿凭据 → 验证上线（含 --passphrase 非交互）
#   5. 启动所选执行器（codebuddy / opencode / workbuddy）并注册"登录时自启"计划任务
#   只出站连 MQTT(1883)+HTTP(8000) 与发现用 UDP(41830)，不开入站端口、无需防火墙配置。
#
param(
    [string]$Passphrase = "",
    [string]$HostIP = "",
    [string]$Executor = "codebuddy",
    [string]$Name = "",
    [string]$InstallDir = "C:\agent-bus"
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Repo = "https://github.com/weifeng-work/agent-bus"
$LogFile = "$env:LOCALAPPDATA\agent-bus-executor.log"

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
if (Test-Path $InstallDir) { Remove-Item $InstallDir -Recurse -Force }
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
$phArg = if ($Passphrase) { '--passphrase "' + $Passphrase + '"' } else { "" }
$nameArg = if ($Name) { '--name "' + $Name + '"' } else { "" }
Push-Location $InstallDir
try {
    if ($Passphrase) {
        & cmd /c "cd /d $InstallDir && $py scripts\join_team.py $hostArg $phArg $nameArg"
    } else {
        & cmd /c "cd /d $InstallDir && $py scripts\join_team.py $hostArg $nameArg"
    }
    if ($LASTEXITCODE -ne 0) { throw "加入队伍失败（exit=$LASTEXITCODE）" }
} finally {
    Pop-Location
}
Write-Host "  [4/5] 已加入队伍"

# ---------- 5. 启动执行器 ----------
Write-Host "  [5/5] 生成并启动执行器（$Executor）..."
# 从加入写出的设备身份读取 agent_id（host-xxxx），保证稳定
$devJson = "$env:USERPROFILE\.config\agent-bus\device.json"
$agentId = ""
if (Test-Path $devJson) {
    try { $agentId = (Get-Content $devJson -Raw | ConvertFrom-Json).agent_id } catch {}
}
if (-not $agentId) { $agentId = $Executor + "_" + ($env:COMPUTERNAME.ToLower() -replace '[^a-z0-9]', '') }
if (-not $Name)    { $Name = ($Executor.Substring(0,1).ToUpper() + $Executor.Substring(1)) + "@$env:COMPUTERNAME" }

$bat = @"
@echo off
cd /d $InstallDir
start "" /min cmd /c "$py executor\${Executor}_executor.py --agent-id $agentId --name `"$Name`" > `"$LogFile`" 2>&1"
"@
Set-Content -Path "$InstallDir\start_executor.bat" -Value $bat -Encoding ASCII
schtasks /create /tn "AgentBus$Executor" /tr "`"$InstallDir\start_executor.bat`"" /sc onlogon /f | Out-Null
Start-Process "$InstallDir\start_executor.bat"
Start-Sleep 5

Write-Host ""
Write-Host "== 加入完成 ==" -ForegroundColor Green
Write-Host "  agent_id : $agentId"
Write-Host "  执行器   : $Executor（已注册登录自启任务 AgentBus$Executor）"
Write-Host "  在线验证 : 打开主机面板（http://<主机IP>:8000/）应看到 [$agentId] 在线"
Write-Host "  日志     : $LogFile"
Write-Host "  加入配置 : $env:USERPROFILE\.config\agent-bus\"