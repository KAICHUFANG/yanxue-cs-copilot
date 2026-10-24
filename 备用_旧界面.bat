@echo off
%SystemRoot%\System32\chcp.com 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto NOVENV

echo Starting legacy tkinter version...
.venv\Scripts\python.exe app_workbench.py
if errorlevel 1 goto FAILED
goto END

:NOVENV
echo [ERROR] .venv not found.
pause
goto END

:FAILED
echo [ERROR] App exited with an error.
pause

:END
