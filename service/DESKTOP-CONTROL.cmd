@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" exit /b 2
echo Experimental supervised input. Read service\DESKTOP-INPUT-TH.md first.
echo Without explicit arguments this stays disabled.
".venv\Scripts\python.exe" service\desktop_console.py %*
set "DESKTOP_EXIT=%ERRORLEVEL%"
pause
exit /b %DESKTOP_EXIT%
