@echo off
chcp 65001 >nul
title YHGame 推送 - WinUI3
cd /d "%~dp0"

if exist "dist\YHGamePush\YHGamePush.exe" (
    start "" "dist\YHGamePush\YHGamePush.exe"
    exit /b 0
)

echo 首次运行需要编译（只做一次，之后直接启动）。
echo 需要 .NET SDK 8 或更高版本。
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
if errorlevel 1 (
    echo.
    echo [错误] 编译失败，请查看上面的输出。
    pause
    exit /b 1
)

start "" "%~dp0dist\YHGamePush\YHGamePush.exe"
exit /b 0
