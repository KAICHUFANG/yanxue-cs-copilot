@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 找不到虚拟环境 .venv
    echo 请先在 PowerShell 里执行：
    echo   py -3.11 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)
echo 正在启动山海科普客服工作台...
echo 左边说话，右边看状态。关闭这个黑窗口前不要关工作台。
start "" ".venv\Scripts\python.exe" app_workbench.py
