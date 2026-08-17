# agent-bus Windows 节点一键安装（WorkBuddy 桌面端执行器）
#
# 在目标 Windows 机上以【普通权限】PowerShell 运行（零参数，全部自动探测）:
#   irm https://raw.githubusercontent.com/weifeng-work/agent-bus/main/scripts/install_windows.ps1 | iex
#
# 自定义安装时下载后运行:
#   powershell -ExecutionPolicy Bypass -File install_windows.ps1 -AgentId workbuddy_pc2 -Name "WorkBuddy@PC2"
#
# 做了什么:
#   1. 检测/安装 Python（winget）
#   2. 下载项目 zip 解压到 C:\agent-bus（无需 git）
#   3. pip 安装 paho-mqtt pywinauto
#   4. 生成 start_executor.bat（pythonw 无窗口 + 日志到 %LOCALAPPDATA%\agent-bus-executor.log）
#   5. 注册"登录时自启"计划任务并立即启动
#   出站连接 MQTT(1883)，不开任何入站端口、无需防火墙配置。
param(
    [string]$BrokerHost = "192.168.31.186",
    [int]$BrokerPort = 1883,
    [string]$AgentId = "",
    [string]$Name = "",
    [string]$InstallDir = "C:\agent-bus"
)

$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if (-not $AgentId) { $AgentId = "workbuddy_" + ($env:COMPUTERNAME.ToLower() -replace '[^a-z0-9]', '') }
if (-not $Name)    { $Name = "WorkBuddy@$env:COMPUTERNAME" }
$LogFile = "$env:LOCALAPPDATA\agent-bus-executor.log"

Write-Host "== agent-bus Windows 节点安装 ==" -ForegroundColor Cyan
Write-Host "  节点: $AgentId ($Name)  总线: ${BrokerHost}:$BrokerPort  目录: $InstallDir"

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
    "https://github.com/weifeng-work/agent-bus/archive/refs/heads/main.zip",
    "https://ghproxy.net/https://github.com/weifeng-work/agent-bus/archive/refs/heads/main.zip"
)) {
    try { Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing; $dl = $true; break } catch {}
}
if (-not $dl) { throw "下载失败：GitHub 与镜像均不可达。可在能上网的机器下载 main.zip 后拷贝为 $zip 重跑" }
Expand-Archive -Path $zip -DestinationPath "$env:TEMP\agent-bus-extract" -Force
Move-Item "$env:TEMP\agent-bus-extract\agent-bus-main" $InstallDir
Remove-Item "$env:TEMP\agent-bus-extract", $zip -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  [2/5] 代码就绪: $InstallDir"

# ---------- 3. 依赖 ----------
Write-Host "  [3/5] 安装 Python 依赖 (paho-mqtt pywinauto)..."
& cmd /c "$py -m pip install --quiet paho-mqtt pywinauto"
if ($LASTEXITCODE -ne 0) { throw "pip 安装失败，检查网络后重跑" }
Write-Host "  [3/5] 依赖就绪"

# ---------- 4. 启动器（pythonw 无窗口） ----------
Write-Host "  [4/5] 生成启动器..."
# 解析 pythonw（无控制台，避免受控机弹黑窗）
$pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
if (-not $pyw) {
    $pw = Get-Command pyw -ErrorAction SilentlyContinue
    if ($pw) { $pyw = "pyw" } else { $pyw = "pythonw" }
}
$exeArgs = @(
    "executor\workbuddy_executor.py",
    "--agent-id", $AgentId,
    "--name", $Name,
    "--broker-host", $BrokerHost,
    "--broker-port", $BrokerPort,
    "--workdir", "$InstallDir\data\executor_work"
)
# 仅手动双击用；已改用 pythonw，不弹黑窗
$bat = @"
@echo off
cd /d $InstallDir
start "" pythonw.exe executor\workbuddy_executor.py --agent-id $AgentId --name `"$Name`" --broker-host $BrokerHost --broker-port $BrokerPort --workdir `"$InstallDir\data\executor_work`" > "$LogFile" 2>&1
"@
Set-Content -Path "$InstallDir\start_executor.bat" -Value $bat -Encoding ASCII

# ---------- 5. 计划任务（登录自启，Hidden，无黑窗） ----------
# PowerShell 原生 Register-ScheduledTask：不受 schtasks /tr 261 字符上限限制，且 Settings.Hidden
Write-Host "  [5/5] 注册计划任务..."
$exeArgsStr = ($exeArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join " "
$act = New-ScheduledTaskAction -Execute $pyw -Argument $exeArgsStr -WorkingDirectory $InstallDir
$trig = New-ScheduledTaskTrigger -AtLogOn
$set = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 0)
Register-ScheduledTask -TaskName "AgentBusWorkBuddy" -Action $act -Trigger $trig -Settings $set -Force | Out-Null
Start-Process -FilePath $pyw -ArgumentList $exeArgs -WindowStyle Hidden -WorkingDirectory $InstallDir
Start-Sleep 5
Write-Host ""
Write-Host "== 安装完成 ==" -ForegroundColor Green
Write-Host "  日志: $LogFile"
Write-Host "  验证: 打开 http://${BrokerHost}:8000/ 应看到 [$AgentId] 在线"
Write-Host "  自启: 已注册计划任务 AgentBusWorkBuddy（本用户登录时）"

# ---------- 前置条件提醒 ----------
Write-Host ""
$wb = Get-Process -Name "WorkBuddy" -ErrorAction SilentlyContinue
if ($wb) {
    $admin = $wb | Where-Object { $_.Path -and (Test-Path $_.Path) }
    Write-Host "[OK] WorkBuddy 正在运行" -ForegroundColor Green
} else {
    Write-Host "[!] WorkBuddy 未在运行——首个任务会经 deeplink 冷启动（需已安装且曾登录）" -ForegroundColor Yellow
}
Write-Host "[!] 两个已知约束:"
Write-Host "    - WorkBuddy 必须以普通权限运行（管理员进程会拦截 UIA 输入注入）"
Write-Host "    - 目标机锁屏时 GUI 任务会失败（执行器会回传明确错误而非挂死）"
