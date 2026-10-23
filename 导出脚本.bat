@echo off
chcp 65001 >nul
title Minecraft 整合包更新工具

echo 正在启动更新程序...
echo.

REM 检查是否有内置 Python
if exist "%~dp0lib\python\python.exe" (
    "%~dp0lib\python\python.exe" "%~dp0lib\manifest_exporter.py"
) else (
    REM 使用系统 Python
    python "%~dp0lib\manifest_exporter.py"
    if errorlevel 9009 (
        echo.
        echo ❌ 错误: 未找到 Python
        echo 请安装 Python 或下载包含 Python 的完整版本
        pause
        exit /b 1
    )
)

if errorlevel 1 (
    echo.
    echo 更新过程中出现错误！
    pause
)