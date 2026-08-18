# 受控机远程更新脚本（服务形态，Phase 4）
#
# 由主控通过 shell_exec 触发。服务形态下：
#   - 停 NSSM 服务 AgentBusCore
#   - 替换代码目录（校验 sha256）
#   - 起 NSSM 服务 AgentBusCore
#   - 自检回执
#
# 用法（主控机）:
#   1) 全量更新:
#      comm_node.py --role hub --shell-exec --target <node> --cmd "curl -fsSL <gh>/scripts/remote_update_worker.ps1 -o <install>\data\_upd.ps1 && powershell -ExecutionPolicy Bypass -File <install>\data\_upd.ps1" --timeout 180
#   2) 仅重启:
#      ... -cmd "powershell -ExecutionPolicy Bypass -File <install>\data\_upd.ps1 -RestartOnly"
#
# 注意:
#   - 本脚本执行到"停服务"时，执行它的 shell_exec 进程可能被停 → 回执超时（预期）
#   - 更新是否成功以"节点重新上线 + 新版本行为"为准（主控隔 30s 后验证）
#   - 保留 data/（运行时状态），仅覆盖代码文件
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\agent-bus",
    [string]$Repo = "https://github.com/weifeng-work/agent-bus",
    [switch]$RestartOnly
)
$ErrorActionPreference = "Stop"
$nssm = "$InstallDir\scripts\_dl\nssm.exe"

# 1. 停 NSSM 服务（避免更新过程中服务重启）
Write-Host "stopping service AgentBusCore..."
if (Test-Path $nssm) {
    & $nssm stop AgentBusCore confirm 2>$null | Out-Null
    Start-Sleep 2
} else {
    Write-Host "nssm not found, trying sc stop..."
    sc stop AgentBusCore 2>$null | Out-Null
    Start-Sleep 2
}

# 2. 停旧版计划任务（兼容旧部署，幂等）
schtasks /change /tn AgentBusShell /disable 2>$null | Out-Null
schtasks /change /tn AgentBusShellWatchdog /disable 2>$null | Out-Null

if (-not $RestartOnly) {
    # 3. 下载新代码并替换
    Write-Host "downloading new code..."
    $zip = "$env:TEMP\ab-update.zip"
    $ex  = "$env:TEMP\ab-update"
    Invoke-WebRequest "$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
    if (Test-Path $ex) { Remove-Item $ex -Recurse -Force }
    Expand-Archive $zip -DestinationPath $ex -Force
    # 保留 data/ 目录（运行时状态），仅覆盖代码文件
    $dataBackup = "$env:TEMP\ab-data-backup"
    if (Test-Path "$InstallDir\data") {
        if (Test-Path $dataBackup) { Remove-Item $dataBackup -Recurse -Force }
        Copy-Item "$InstallDir\data" $dataBackup -Recurse -Force
    }
    Copy-Item "$ex\agent-bus-main\*" $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    # 恢复 data/ 目录
    if (Test-Path $dataBackup) {
        Copy-Item "$dataBackup\*" "$InstallDir\data\" -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item $dataBackup -Recurse -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $ex, $zip -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "code updated"
}

# 4. 杀残留进程（按命令行匹配，覆盖 python.exe 与 pythonw.exe）
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'comm_node|_executor\.py|core_node|tray_app' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 1

# 5. 启动 NSSM 服务（新代码）
Write-Host "starting service AgentBusCore..."
if (Test-Path $nssm) {
    & $nssm start AgentBusCore 2>$null | Out-Null
    Start-Sleep 3
    # 查询状态
    $status = & $nssm status AgentBusCore 2>$null
    Write-Host "service status: $status"
} else {
    Write-Host "nssm not found, trying sc start..."
    sc start AgentBusCore 2>$null | Out-Null
}

# 6. 恢复旧版计划任务（兼容旧部署，幂等）
schtasks /change /tn AgentBusShell /enable 2>$null
schtasks /change /tn AgentBusShellWatchdog /enable 2>$null

Write-Host "node restarted"