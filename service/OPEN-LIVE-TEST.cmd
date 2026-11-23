@echo off
setlocal
title Browser Engine - PICK NOTEPAD
cd /d "%~dp0.."
echo ============================================================
echo   BROWSER ENGINE - LIVE TEST
echo ============================================================
echo Keep a NEW empty Notepad window open for this test.
echo When asked, type ENABLE and press Enter.
echo Then choose the Untitled Notepad from the numbered list.
echo No countdown during window selection. Stay in this console.
echo No input is sent before your approval.
echo.
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Project Python is missing.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" service\desktop_console.py --enable-input --choose-window --allow-exe "C:\Windows\System32\notepad.exe"
set "LIVE_TEST_EXIT=%ERRORLEVEL%"
echo.
echo Test console ended with exit code %LIVE_TEST_EXIT%.
pause
exit /b %LIVE_TEST_EXIT%
