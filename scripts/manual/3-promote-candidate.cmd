@echo off
setlocal
cd /d "%~dp0\..\.."
".venv-host\Scripts\python.exe" "scripts\manual_ops.py" promote
echo.
pause
