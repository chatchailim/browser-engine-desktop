@echo off
rem คัดลอก X-Desktop-Token ไปยังคลิปบอร์ด (ไว้วางในหน้า /lab)
setlocal
set "TOKEN_FILE=%USERPROFILE%\.browser-engine-desktop\token"
if not exist "%TOKEN_FILE%" (
  echo ยังไม่มีไฟล์ token — ให้เปิด server ก่อน (run.cmd) แล้วจะสร้างให้เอง
  pause
  exit /b 1
)
set /p TOKEN=<"%TOKEN_FILE%"
echo %TOKEN%|clip
echo คัดลอก token ไปยังคลิปบอร์ดแล้ว (%TOKEN:~0,4%...) — ไปวางในหน้า http://127.0.0.1:8205/lab
timeout /t 3 >nul