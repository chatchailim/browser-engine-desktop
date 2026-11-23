# Browser Engine Desktop — บริการทดลองใช้ในเครื่อง

ประเมิน 25 กันยายน 2026: **Local single-user pilot / ยังไม่ใช่ computer use เต็มรูปแบบ และยังไม่รับรอง production**

## เปิดใช้และหยุด

1. ดับเบิลคลิก `START-LOCAL.cmd` ในโฟลเดอร์นี้ ใช้ Python `.venv` ของโครงการที่มี dependency แล้ว ไม่ติดตั้งเพิ่มอัตโนมัติ
2. เปิด `http://127.0.0.1:8205` ใน browser หากเปลี่ยน port ใน config ให้ใช้ค่าที่ตั้งไว้
3. Dashboard/API ใช้ token ของผู้ใช้ Windows ที่ `%USERPROFILE%\.browser-engine-desktop\token` ห้ามส่ง token ผ่านรายงานหรือ commit
4. ใช้ `CHECK-STATUS.cmd` ตรวจ `status=RUNNING` และ `model_ready=true` ก่อนส่งงาน แยก server เปิดอยู่กับ model พร้อมใช้งาน
5. ส่งงานผ่าน Dashboard ทีละงาน รอจบก่อนส่งงานถัดไป หากมี approval/takeover ให้ผู้ใช้ตัดสินใจในหน้า Dashboard
6. หยุดด้วย Ctrl+C ที่หน้าต่าง server เมื่อไม่มีงานค้าง ไม่ใช้ taskkill กับ PID ที่ไม่ทราบเจ้าของ

นี่คือ local HTTP process ไม่ใช่ Windows Service/ระบบ autostart และยังไม่ได้เปิดค้างไว้โดย Codex การเริ่ม server ไม่ได้เริ่ม browser task หรือเปิด desktop input

## ขอบเขตที่ให้ใช้

| ความสามารถ | สถานะหลักฐาน | ค่าเปิดใช้งานปัจจุบัน |
|---|---|---|
| Browser DOM / accessibility / browser input | มี implementation และ public web/fixture live evidence ตาม WO-017.11 | ใช้ผ่าน browser task |
| Approval / rejection / takeover-resume | ผ่าน local fixtures ผู้อนุมัติเป็น test supervisor | มี API/dashboard flow |
| Direct API recipes | local mock GET/POST ผ่าน ไม่ใช่รับรอง business endpoint จริง | disabled |
| Vision fallback | มี implementation/หลักฐานเก่า ไม่ได้ revalidate ใน readiness รอบนี้ | disabled |
| Windows UIA read-only | live Notepad ผ่าน อ่าน 29 elements / content hash / cleanup | disabled ใน config ปกติ; ยังเป็น component/rehearsal ไม่ใช่ HTTP desktop service |
| Windows click/type/drag/key | เพิ่ม supervised console ตามคำขอผู้ใช้ ผ่านเฉพาะ offline tests; ยังรอ live input acceptance | opt-in แยกต่างหาก ดู [คู่มือ](DESKTOP-INPUT-TH.md); hotkeys ยังบล็อก |
| Browser ↔ native desktop switching | UIA ยังไม่ต่อ orchestrator/router งานเดียวกัน | unavailable |
| OneVault facts | พิสูจน์ใน rehearsal ที่เปิดชั่วคราว | disabled ใน config ปกติ |

ไฟล์เปิดบริการนี้ไม่เปลี่ยน feature flags และไม่ยกเลิกคำสั่งหยุดงาน Suno/เครดิตของผู้ใช้

## สิ่งที่ยังต้องทำก่อนเปิดเป็นบริการ production

- Atomic task reservation/lock และ unique task IDs: ปัจจุบัน server ตรวจ ACTIVE_TASK_ID ก่อน start thread แต่ worker ตั้งค่าทีหลัง มีช่อง race; task id ใช้ seconds อาจซ้ำ จึงจำกัด operator ทีละงาน
- Validate API payload และจัดการ malformed JSON/type/range ให้คืน HTTP 4xx แน่นอน ไม่หลุด exception
- Durable task status/restart recovery และกำหนดพฤติกรรมงานค้าง: TASKS อยู่ใน memory และ worker เป็น daemon
- ทดสอบ authenticated service lifecycle/start-stop และ model connectivity ในเครื่องเป้าหมาย ไม่ใช้ runner ผ่านแทน API acceptance
- Benchmark เทียบ baseline เดิมกับงาน/โมเดล/environment เดียวกันซ้ำหลายรอบ; ยังไม่มีหลักฐานรับรองเร็วกว่า
- Desktop input แบบมีคนกำกับมี implementation แล้ว ต้องตรวจ live input/stop/verification และ integration เพิ่มก่อนเป็น computer use เต็มรูปแบบ ห้ามเปิด input โดยข้าม console approval

## หลักฐาน

- `coordination/codex-anti/codex-output/SERVICE-READINESS/audit.json`
- `coordination/codex-anti/codex-output/SERVICE-READINESS/ASSESSMENT-TH.md`
- `coordination/codex-anti/codex-output/WO-017-CLOSEOUT/LIVE-ACCEPTANCE-TH.md`
- `coordination/codex-anti/anti-output/WO-017.11/` สำหรับ A–D คนละรอบกับ E

สำหรับการเรียกจากระบบอื่น: POST `/api/task` รับ url, goal, max_steps และ profile (ถ้าจำเป็น); ติดตาม GET `/api/task/<task_id>` และ GET `/api/approvals`; ทุก `/api/*` ต้องมี `X-Desktop-Token` ไม่มี endpoint desktop-input ที่พร้อมให้เรียก
