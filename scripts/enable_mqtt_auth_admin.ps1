# 以管理员身份运行（一次性）：启用 mosquitto 认证并重启服务
#   powershell -ExecutionPolicy Bypass -File scripts\enable_mqtt_auth_admin.ps1
# 幂等：重复运行无害。回滚：把备份 conf 拷回 + Restart-Service mosquitto。
$ErrorActionPreference = "Stop"
$conf = "C:\Program Files\mosquitto\mosquitto.conf"
$authDir = "C:\mosquitto-auth"

if (-not (Test-Path $authDir)) { throw "缺少 $authDir（先运行 python scripts/add_node.py --init）" }

# 备份原配置（仅首次）
if (-not (Test-Path "$conf.bak")) { Copy-Item $conf "$conf.bak" }

$content = Get-Content $conf -Raw
$changed = $false

if ($content -match "allow_anonymous\s+true") {
    $content = $content -replace "allow_anonymous\s+true", "allow_anonymous false"
    $changed = $true
} elseif ($content -notmatch "allow_anonymous") {
    $content += "`nallow_anonymous false`n"
    $changed = $true
}
if ($content -notmatch "password_file") {
    $content += "password_file $authDir\passwd`n"
    $changed = $true
}
if ($content -notmatch "acl_file") {
    $content += "acl_file $authDir\acl`n"
    $changed = $true
}

if ($changed) {
    Set-Content -Path $conf -Value $content -Encoding ascii
    Write-Host "mosquitto.conf 已更新（allow_anonymous=false / password_file / acl_file）"
} else {
    Write-Host "mosquitto.conf 已是目标状态，无需修改"
}

Restart-Service mosquitto -Force
Start-Sleep 2
$svc = Get-Service mosquitto
Write-Host "mosquitto 服务状态: $($svc.Status)"
Write-Host "验证: 无凭据应被拒 —— mosquitto_pub -h 127.0.0.1 -t test -m x  (应报 not authorised)"
