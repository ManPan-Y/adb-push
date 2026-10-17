@echo off
rem 旧名字保留兼容，实际入口是 启动.bat
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0启动.bat"
