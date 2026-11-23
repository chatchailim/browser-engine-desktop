@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\pythonw.exe" (
  echo Project Python is missing.
  pause
  exit /b 2
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0easy_desktop_test.py"
exit /b 0
