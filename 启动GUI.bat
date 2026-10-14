@echo off
chcp 65001 >nul
title ADB 无线推送工具
cd /d "%~dp0"

rem ---------------------------------------------
rem  查找「真正可用」的 Python
rem  注意：Windows 应用商店的 python.exe 是占位别名，
rem        用 `python -c "import sys"` 才能真正验证它可用。
rem ---------------------------------------------
set "PY="

where python >nul 2>nul && python -c "import sys" >nul 2>nul && set "PY=python"

if not defined PY (
    where py >nul 2>nul && py -c "import sys" >nul 2>nul && set "PY=py"
)

if not defined PY (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
        "C:\Python313\python.exe"
        "C:\Python312\python.exe"
        "C:\Python311\python.exe"
        "C:\Python310\python.exe"
        "D:\Python313\python.exe"
        "D:\Python312\python.exe"
        "D:\Python311\python.exe"
        "D:\Python310\python.exe"
    ) do if not defined PY if exist %%P set "PY=%%~P"
)

if not defined PY (
    echo.
    echo [错误] 未检测到可用的 Python，无法启动图形界面。
    echo.
    echo   如果你看到过「Python was not found ... Microsoft Store」的提示，
    echo   说明系统里只有应用商店的占位别名，并不是真正安装了 Python。
    echo.
    echo   请安装 Python 3.10+ 并勾选 "Add python.exe to PATH"：
    echo     https://www.python.org/downloads/windows/
    echo.
    echo   安装后如仍报错，可关闭「应用执行别名」：
    echo     设置 - 应用 - 高级应用设置 - 应用执行别名，
    echo     关闭其中的 python.exe / python3.exe。
    echo.
    pause
    exit /b 1
)

echo 使用 Python：%PY%
"%PY%" gui.py
if errorlevel 1 (
    echo.
    echo [提示] 程序异常退出，错误码 %errorlevel%。
    pause
)
