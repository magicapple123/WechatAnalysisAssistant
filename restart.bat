@echo off
setlocal
title WechatAnalysisAssistant
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-web.ps1"
if errorlevel 1 (
    echo.
    echo [ERROR] Startup failed. Review the message above.
    pause
    exit /b 1
)

echo.
echo Startup completed. The backend is running in the background.
echo Run restart.bat again whenever you want to restart it.
timeout /t 3 /nobreak >nul
exit /b 0
