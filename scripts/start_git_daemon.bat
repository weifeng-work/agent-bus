@echo off
rem agent-bus git daemon launcher (G1 manual; G2 -> logon-independent task, design 3.3)
rem Binds WLAN IP only (B6); --enable=receive-pack allows push (D1 anonymous).
rem NB: git 2.55 removed legacy --enable-receive-pack; use --enable=receive-pack.
rem NOTE: keep this file pure ASCII (cmd parses bat as ANSI/GBK).
setlocal
set GIT_DAEMON=C:\Program Files\Git\mingw64\libexec\git-core\git-daemon.exe
set BASE_PATH=%~dp0..\data\git_repos
"%GIT_DAEMON%" --reuseaddr --verbose --listen=192.168.31.186 --base-path="%BASE_PATH%" --export-all --enable=receive-pack --port=9418
