@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0enable-lan.ps1" %*
set "launchExitCode=%errorlevel%"
echo.
pause
exit /b %launchExitCode%
