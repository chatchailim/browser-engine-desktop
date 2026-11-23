@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title Browser Engine Desktop - Local Co-Pilot

rem Check for non-interactive flag
set "NO_PAUSE=0"
for %%A in (%*) do (
    if /i "%%~A"=="--no-pause" set "NO_PAUSE=1"
    if /i "%%~A"=="/no-pause" set "NO_PAUSE=1"
)

rem Delegate directly to run.ps1 for consistent pre-flight and bootstrap
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"

if %EXIT_CODE% neq 0 (
    echo.
    echo [!] Launcher exited with error code %EXIT_CODE%
    if "%NO_PAUSE%"=="0" pause
    exit /b %EXIT_CODE%
)

if "%NO_PAUSE%"=="0" pause
exit /b 0
