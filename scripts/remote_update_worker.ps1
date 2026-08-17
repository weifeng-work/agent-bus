# 受控机远程更新脚本（由主控通过 shell_exec 触发，需求2 shell 通道的运维用法）
#
# 用法（主控机）:
#   1) 全量更新（下载新代码 + 替换 + 重启节点）:
#      comm_node.py --role hub --shell-exec --target <node> --cmd "curl -fsSL <gh>/scripts/remote_update_worker.ps1 -o C:\agent-bus\data\_upd.ps1 && powershell -ExecutionPolicy Bypass -File C:\agent-bus\data\_upd.ps1" --timeout 180
#   2) 仅重启（代码已手动更新）:
#      ... -cmd "powershell -ExecutionPolicy Bypass -File C:\agent-bus\data\_upd.ps1 -RestartOnly"
#
# 注意:
#   - 节点进程为 pythonw.exe（无控制台窗口），杀进程必须按命令行匹配（-match 'comm_node|_executor\.py'），
#     不能按进程名 Name='python.exe' 过滤（会漏掉 pythonw）。
#   - 本脚本执行到"杀进程"时，执行它的 comm_node 父进程被杀 → shell_exec 回执会超时（预期）。
#     更新是否成功以"节点重新上线 + 新版本行为"为准（主控隔 30s 后验证）。
#   - 保留 install_dir/data（运行时状态），仅覆盖代码文件。
param(
    [string]$InstallDir = "C:\agent-bus",
    [string]$Repo = "https://github.com/weifeng-work/agent-bus",
    [switch]$RestartOnly
)
$ErrorActionPreference = "Stop"

# 1. 停计划任务（避免更新中途 watchdog 拉回旧进程）
schtasks /change /tn AgentBusShell /disable 2>$null | Out-Null
schtasks /change /tn AgentBusShellWatchdog /disable 2>$null | Out-Null

if (-not $RestartOnly) {
    # 2. 下载新代码并替换（先下载复制，节点仍跑旧进程，不影响）
    Write-Host "downloading new code..."
    $zip = "$env:TEMP\ab-update.zip"
    $ex  = "$env:TEMP\ab-update"
    Invoke-WebRequest "$Repo/archive/refs/heads/main.zip" -OutFile $zip -UseBasicParsing
    if (Test-Path $ex) { Remove-Item $ex -Recurse -Force }
    Expand-Archive $zip -DestinationPath $ex -Force
    Copy-Item "$ex\agent-bus-main\*" $InstallDir -Recurse -Force
    Remove-Item $ex, $zip -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "code updated"
}

# 3. 杀节点进程（按命令行匹配，覆盖 python.exe 与 pythonw.exe）
Get-CimInstance Win32_Process |
    Where-Object { $_.CommandLine -match 'comm_node|_executor\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 1

# 4. 恢复计划任务并重启节点（新代码）
schtasks /change /tn AgentBusShell /enable
schtasks /change /tn AgentBusShellWatchdog /enable
Start-Process "$InstallDir\start_tray.bat"
Write-Host "node restarted"
