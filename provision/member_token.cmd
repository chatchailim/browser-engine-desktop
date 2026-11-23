@echo off
REM member_token.cmd — สร้าง token สมาชิก 1 คน (ตามสิทธิ์ 3 ระดับ)
REM ใช้: member_token.cmd <ชื่อสมาชิก> <admin|operator|viewer>
REM     ตัวอย่าง: member_token.cmd นพ operator
REM token แสดง 1 ครั้งบนจอ — ให้สมาชิกเก็บเอง (ไม่ส่งใน chat/email)
if "%~2"=="" (
  echo ใช้: member_token.cmd ^<ชื่อสมาชิก^> ^<admin^|operator^|viewer^>
  echo   operator = สั่งงาน AI ได้ + อนุมัติ + หยุดงาน (แตะคำสั่งระบบไม่ได้)
  echo   viewer   = ดูผลอย่างเดียว
  echo   admin    = ทั้งหมด (สำรองผู้ดูแล — ไม่ควรเกิน 2 คนต่อ Node)
  exit /b 2
)
cd /d "%~dp0.."
.venv\Scripts\python.exe -m service.roles add "%~1" "%~2"
echo.
echo --- ต่อไป: สมาชิกวาง token นี้ในหน้า /lab (sessionStorage) — สั่งงานได้ตามสิทธิ์
echo ตรวจรายชื่อทั้งหมด: .venv\Scripts\python.exe -m service.roles list