@echo off
%SystemRoot%\System32\chcp.com 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto NOVENV

echo Starting workbench on http://localhost:8501
echo Browser will open in a few seconds. Keep this window open; close it to stop.
echo.

rem Streamlit 用 headless 模式启动，否则首次运行会卡在"输入邮箱"那一问。
rem 浏览器由下面这条后台命令延迟打开，等端口起来了再开，避免白页。
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 7; Start-Process 'http://localhost:8501'"

.venv\Scripts\python.exe -m streamlit run app_streamlit.py --server.port 8501 --server.headless true --browser.gatherUsageStats false
if errorlevel 1 goto FAILED
goto END

:NOVENV
echo.
echo [ERROR] .venv not found. Run these first:
echo   py -3.11 -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
goto END

:FAILED
echo.
echo [ERROR] Workbench exited with an error. See the message above.
pause

:END
