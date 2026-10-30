@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  Shanhai Kexue - Customer Service Workbench (dev launcher)
rem  Requires .venv in this folder (see README > Quick start).
rem  NOTE: keep this file ASCII-only. cmd mis-splits multi-byte
rem        characters in .bat files; Chinese text goes to .txt.
rem ============================================================

if not exist ".venv\Scripts\python.exe" goto NOVENV

echo.
echo  Starting the workbench. Your browser will open in a few seconds.
echo  Address: http://localhost:8501
echo  Close this window to stop the service.
echo.

start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 7; Start-Process 'http://localhost:8501'"

.venv\Scripts\python.exe -m streamlit run app_streamlit.py --server.port 8501 --server.headless true --browser.gatherUsageStats false
if errorlevel 1 goto FAILED
goto END

:NOVENV
echo.
echo  [ERROR] .venv not found. Run these first:
echo    py -3.11 -m venv .venv
echo    .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
goto END

:FAILED
echo.
echo  [ERROR] Failed to start. Send a screenshot of this window.
pause

:END
