# agent-bus central repo one-shot init (design: docs/git_central_repo.md, G1/B8)
# Steps: create bare repo -> install pre-receive hook (force LF) -> seed push -> print daemon info
# NOTE: keep this file pure ASCII (PS 5.1 parses non-BOM ps1 as ANSI).
# Usage: powershell -ExecutionPolicy Bypass -File scripts\init_central_repo.ps1
#             [-RepoName agent-bus] [-SeedBranch main] [-SkipSeed]
param(
    [string]$RepoName = "agent-bus",
    [string]$SeedBranch = "main",
    [switch]$SkipSeed
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot          # agent-bus install dir
$reposDir = Join-Path $root "data\git_repos"
$bare = Join-Path $reposDir "$RepoName.git"
$hookSrc = Join-Path $PSScriptRoot "git_hooks\pre-receive"

Write-Host "[init] root = $root"

# 1. base-path dir
New-Item -ItemType Directory -Force -Path $reposDir | Out-Null
Write-Host "[init] base-path ready: $reposDir"

# 2. bare repo (idempotent)
if (Test-Path (Join-Path $bare "HEAD")) {
    Write-Host "[init] bare repo exists, skip init: $bare"
} else {
    git init --bare $bare
    if ($LASTEXITCODE -ne 0) { throw "git init --bare failed" }
    Write-Host "[init] bare repo created: $bare"
}
# HEAD must point at the real seed branch (default master never exists here;
# broken HEAD makes clone warn and can degenerate push negotiation).
git --git-dir $bare symbolic-ref HEAD "refs/heads/$SeedBranch"
Write-Host "[init] HEAD -> refs/heads/$SeedBranch"

# 3. install hook (copy + force LF; CRLF breaks sh; hook stays UTF-8, B2)
$hooksDir = Join-Path $bare "hooks"
New-Item -ItemType Directory -Force -Path $hooksDir | Out-Null
$content = Get-Content -Raw -Encoding UTF8 -Path $hookSrc
$content = $content -replace "`r`n", "`n"
$hookDst = Join-Path $hooksDir "pre-receive"
[System.IO.File]::WriteAllText($hookDst, $content, (New-Object System.Text.UTF8Encoding $false))
Write-Host "[init] hook installed (LF): $hookDst"

# 4. seed push from the working repo ('central' as second remote; origin untouched)
#    NB: run git with EAP=Continue + $LASTEXITCODE checks; PS 5.1 treats native
#    stderr as terminating errors under EAP=Stop.
if (-not $SkipSeed) {
    Push-Location $root
    try {
        $ErrorActionPreference = "Continue"
        $cur = (git config --get remote.central.url)
        if ($LASTEXITCODE -ne 0 -or -not $cur) {
            git remote add central $bare | Out-Null
            Write-Host "[init] remote 'central' added -> $bare"
        } elseif ($cur.Trim() -ne $bare) {
            git remote set-url central $bare | Out-Null
            Write-Host "[init] remote 'central' re-pointed to $bare"
        }
        git push central $SeedBranch 2>&1 | ForEach-Object { Write-Host "  $_" }
        if ($LASTEXITCODE -ne 0) { throw "seed push failed (branch=$SeedBranch)" }
        Write-Host "[init] seed pushed: $SeedBranch -> central"
        $ErrorActionPreference = "Stop"
    } finally {
        Pop-Location
    }
}

Write-Host ""
Write-Host "[done] daemon url: git://192.168.31.186/$RepoName.git"
Write-Host "[done] start daemon: scripts\start_git_daemon.bat (G2: logon-independent task)"
