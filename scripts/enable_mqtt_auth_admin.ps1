# Run as Administrator (one-time): enable mosquitto auth and restart service.
#   powershell -ExecutionPolicy Bypass -File scripts\enable_mqtt_auth_admin.ps1
# Idempotent: safe to re-run. Rollback: copy the .bak conf back + Restart-Service mosquitto.
# NOTE: keep this file ASCII-only (Windows PowerShell 5.1 parses non-BOM files as ANSI).
$ErrorActionPreference = "Stop"
$conf = "C:\Program Files\mosquitto\mosquitto.conf"
$authDir = "C:\mosquitto-auth"

if (-not (Test-Path $authDir)) { throw "missing $authDir (run: python scripts/add_node.py --init first)" }

# Backup original conf (first run only)
$bak = "$conf.bak"
if (-not (Test-Path $bak)) { Copy-Item $conf $bak }

$content = Get-Content $conf -Raw
$changed = $false

if ($content -match "allow_anonymous\s+true") {
    $content = $content -replace "allow_anonymous\s+true", "allow_anonymous false"
    $changed = $true
} elseif ($content -notmatch "allow_anonymous") {
    $content += "`nallow_anonymous false`n"
    $changed = $true
}
if ($content -notmatch "(?m)^\s*password_file") {
    $content += "password_file $authDir\passwd`n"
    $changed = $true
}
if ($content -notmatch "(?m)^\s*acl_file") {
    $content += "acl_file $authDir\acl`n"
    $changed = $true
}

if ($changed) {
    Set-Content -Path $conf -Value $content -Encoding ascii
    Write-Host "mosquitto.conf updated (allow_anonymous=false / password_file / acl_file)"
} else {
    Write-Host "mosquitto.conf already in target state, nothing to change"
}

Restart-Service mosquitto -Force
Start-Sleep 2
$svc = Get-Service mosquitto
Write-Host "mosquitto service status: $($svc.Status)"
Write-Host "verify: anonymous publish should be refused -- mosquitto_pub -h 127.0.0.1 -t test -m x  (expect not authorised)"
