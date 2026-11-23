@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Project Python environment missing. Run the project setup launcher first.
  pause
  exit /b 2
)
echo Browser Engine Desktop - local single-user pilot
echo Dashboard: http://127.0.0.1:8205 (unless config port was changed)
echo This starts the server only. No browser task is submitted.
echo Keep this window open. Press Ctrl+C here to stop this server.
".venv\Scripts\python.exe" server.py
set "TASK_EXIT=%ERRORLEVEL%"
pause
exit /b %TASK_EXIT%
