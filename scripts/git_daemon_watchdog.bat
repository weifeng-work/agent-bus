@echo off
rem agent-bus git daemon watchdog (G2, design 3.3)
rem Runs every minute via scheduled task AgentBusGitDaemon (SYSTEM, logon-independent).
rem Logic: if nothing LISTENING on 9418 -> kill stale git-daemon (own process only) -> relaunch.
rem NOTE: keep this file pure ASCII (cmd parses bat as ANSI/GBK).
setlocal
set ROOT=%~dp0..
set LOG=%ROOT%\data\git_daemon.watchdog.log

netstat -ano | findstr "LISTENING" | findstr ":9418 " >nul
if %errorlevel%==0 goto :end

echo %date% %time% [watchdog] 9418 not listening, recovering >> "%LOG%"
rem kill stale daemon process if any (our own infra process; port should be free for rebind)
taskkill /F /IM git-daemon.exe >> "%LOG%" 2>&1
rem relaunch detached; daemon verbose output appended to git_daemon.log
start "" cmd /c ""%ROOT%\scripts\start_git_daemon.bat" >> "%ROOT%\data\git_daemon.log" 2>&1"
echo %date% %time% [watchdog] start issued >> "%LOG%"

:end
endlocal
