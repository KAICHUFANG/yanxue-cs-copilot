@echo off
%SystemRoot%\System32\chcp.com 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto NOVENV

echo Starting workbench...
.venv\Scripts\python.exe app_workbench.py
if errorlevel 1 goto FAILED
goto END

:NOVENV
echo.
echo [ERROR] .venv not found.
echo Run these first:
echo   py -3.11 -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
goto END

:FAILED
echo.
echo [ERROR] Workbench exited with an error. See the message above.
pause

:END