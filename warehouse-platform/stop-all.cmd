@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-all.ps1" %*
set "stopExitCode=%errorlevel%"
echo.
pause
exit /b %stopExitCode%
