@echo off
setlocal
cd /d "%~dp0.."
".venv\Scripts\python.exe" service\check_status.py
set "TASK_EXIT=%ERRORLEVEL%"
pause
exit /b %TASK_EXIT%
