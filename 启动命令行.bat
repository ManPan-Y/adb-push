@echo off
chcp 65001 >nul
title ADB 无线推送工具 - 命令行
cd /d "%~dp0"

rem ---------------------------------------------
rem  查找「真正可用」的 Python（排除应用商店占位别名）
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
    echo [错误] 未检测到可用的 Python。
    echo   请安装 Python 3.10+ 并勾选 "Add python.exe to PATH"：
    echo     https://www.python.org/downloads/windows/
    echo.
    pause
    exit /b 1
)

echo 使用 Python：%PY%
echo ============================================================
echo  ADB 无线推送工具 - 命令行版
echo ------------------------------------------------------------
echo  常用命令：
echo    "%PY%" adb_push.py --auto                     自动发现并连接设备
echo    "%PY%" adb_push.py --devices                  列出设备
echo    "%PY%" adb_push.py --discover                 mDNS 发现设备
echo    "%PY%" adb_push.py --to-wireless              USB 转无线
echo    "%PY%" adb_push.py --pair                     无线配对
echo.
echo  传输（默认：差异同步 + 大小/哈希双校验 + 断点续传）：
echo    "%PY%" adb_push.py --source "D:\我的文件" --target "/sdcard/Download/我的文件"
echo    "%PY%" adb_push.py --pull --source "/sdcard/DCIM/Camera" --target "D:\照片备份"
echo    "%PY%" adb_push.py --source "D:\我的文件" --target "/sdcard/Download" --dry-run
echo    "%PY%" adb_push.py --source "D:\我的文件" --target "/sdcard/Download" --jobs 8
echo    "%PY%" adb_push.py --source "D:\我的文件" --target "/sdcard/Download" --mode overwrite --verify size
echo.
echo  并发：--jobs N（默认 4，1 = 关闭并行）
echo ============================================================
echo.

"%PY%" adb_push.py --help
echo.
pause
