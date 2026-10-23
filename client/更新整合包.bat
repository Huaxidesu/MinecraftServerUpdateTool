@echo off
chcp 65001 >nul
title Minecraft 整合包更新工具

REM 防呆：本 bat 被单独拷到桌面等位置时，更新器不在身边，直接给出明确提示。
if not exist "%~dp0update\updater.py" (
    echo.
    echo ❌ 找不到更新程序（update\updater.py）。
    echo.
    echo 本工具必须放在整合包根目录里运行，也就是和 update 文件夹待在一起。
    echo 如果想从桌面启动，请在本文件上 右键 → 发送到 → 桌面快捷方式，
    echo 不要把本文件单独复制到桌面。
    echo.
    pause
    exit /b 1
)

echo 正在启动更新程序...
echo.

REM 检查是否有内置 Python
if exist "%~dp0update\python\python.exe" (
    "%~dp0update\python\python.exe" "%~dp0update\updater.py"
) else (
    REM 使用系统 Python
    python "%~dp0update\updater.py"
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
