# คู่มือผู้ดูแล Node — SPADA MyAI Harness (Node-1) 5 ต.ค. 2026

> คู่มือนี้ทำตามได้โดยไม่ต้องเป็นคนเขียนโค้ด · ทุกขั้นมี "ผลที่ต้องเห็น" และ "เมื่อไรต้องหยุด" — เขียนตามมาตรฐาน runbook OneManOS (Prerequisite Status ชัด + stop conditions)

## สิ่งที่ต้องมีก่อนเริ่ม (Prerequisites)

| ข้อ | สิ่งที่ต้องมี | สถานะขั้นต่ำ |
|---|---|---|
| P1 | Windows 10/11 + สิทธิ์ Administrator (ครั้งเดียวตอนติดตั้ง) | ✅ จำเป็น |
| P2 | Python 3.10+ (`python --version` | ✅ จำเป็น — สคริปต์ตรวจให้และหยุดถ้าไม่ถึง |
| P3 | ไฟล์: zip โค้ด (จาก SPADA) + `admin-system-config.*.json` ที่ผ่าน Approval แล้ว | ✅ จำเป็น |
| P4 | เน็ตตอนติดตั้ง (ดาวน์โหลดแพ็กเกจ + chromium ~150MB ครั้งแรก) | ✅ จำเป็นครั้งแรก |
| P5 | AI Brain (DGX vLLM ใน LAN หรือ cloud) | ⚠️ ถ้าไม่มี = โหมด planning ออฟไลน์ (server ยังขึ้น) |

## ขั้นติดตั้ง (7 ขั้น — สคริปต์ทำให้ พิสูจน์ได้บนหน้าจอ)

```powershell
cd <โฟลเดอร์ที่แตก zip>
.venv\Scripts\python.exe provision\provision_node.py --config provision\admin-system-config.myai-harness.example.json --target "C:\ProgramData\SPADA-MyAI-Harness"
```
- ขั้น 1-2: สคริปต์ตรวจ python + Config (มีของลับฝัง = **หยุดเอง** — พวกนี้ห้ามแก้ข้ามโดยไม่มี Approval)
- ขั้น 3-4: คัดลอกโค้ด + เขียน config.json จาก featureFlags ที่ผ่านอนุมัติ
- ขั้น 5: venv + requirements + chromium (ครั้งแรกใช้เวลา 3-6 นาที — ผ่อนตามเน็ต)
- ขั้น 6: machine token (อยู่ `C:\Users\<คุณ>\.browser-engine-desktop\token` — ไม่แสดงบนจอ)
- ขั้น 7: selftest ชุดหลัก (ต้องผ่าน 100% ถ้าไม่ผ่าน = **หยุด ห้ามใช้**) + เช็ค server ตอบ

**ผลที่ต้องเห็น**: กล่องเขียว "ติดตั้ง SPADA MyAI Harness เสร็จสมบูรณ์"
**ถ้าล้ม**: อ่าน [STOP] บรรทัดสุดท้าย — ส่งข้อความนั้นมาหาผู้ตรวจ/SPADA พร้อม log — **ห้าม**แก้ config ข้าม Approval เพื่องัดข้าม

## หลังติดตั้ง — ทุกวัน

1. เปิด: ดับเบิลคลิก `run.cmd` (หน้าต่างเก็บเปิดไว้) — ตรวจสถานะ: `service\CHECK-STATUS.cmd`
2. ผู้ดูแลใช้งาน: เปิด `http://127.0.0.1:8205/lab` → วาง token (ตัวดูแล = admin)
3. เพิ่มสมาชิก: `provision\member_token.cmd <ชื่อ> <operator|viewer>` — token แสดง **1 ครั้ง** ให้สมาชิกเก็บเอง
   - operator = สั่งงาน AI + อนุมัติ + หยุดงาน (แตะ shell ไม่ได้) · viewer = ดูอย่างเดียว
4. หยุด AI กลางวิ่ง: POST `/api/task-cancel` (หรือปิดหน้าต่าง server ฉุกเฉิน)
5. AI ทำไฟล์พัง (shell): POST `/api/shell-rollback` — กู้จากสแนปช็อตอัตโนมัติ

## การแก้กติกา (เช่น ปลด shell) — ต้องผ่าน Config Manager
- ห้ามแก้ `config.json` ตรง ๆ ในเครื่องสมาชิก — แก้ผ่าน Config Package → ผู้เตรียม(ต่างคน) → Approval → Version ใหม่
- ทุกธงมี `rollbackValue` — กลับคืนได้ทุกเวลาผ่าน Version ถัดไป (มีหลักฐาน Audit)

## การสำรอง/หมุนของลับ
- **สำรอง**: `C:\Users\<คุณ>\.browser-engine-desktop\` (token) + `<โค้ด>\data\keysign\` (กุญแจ) — เก็บใน Vault ของ Config Manager (ห้ามส่งแชท/email)
- **หมุน token**: ลบไฟล์ token → เปิด server ใหม่ → token ใหม่เกิดเอง → แจกใหม่ผ่าน copy-token.cmd
- **หมุน keysign**: โซนเดิมยังตรวจได้ด้วยกุญแจเก่า — เก็บไฟล์เก่าไว้ที่เดิมก่อนเปลี่ยน

## ความสามารถรายสิทธิ์ (สรุปตรงจากการพิสูจน์สด 9 กรณี)
| ทำอะไร | viewer | operator | admin |
|---|---|---|---|
| เห็นสถานะ/ผลงาน (/api/status, /api/tasks) | ✅ | ✅ | ✅ |
| สั่งงาน browser/desktop + อนุมัติ/ปฏิเสธ + หยุดงาน | ❌ 403 | ✅ | ✅ |
| รันคำสั่งระบบ (/api/shell-run) | ❌ 403 | ❌ 403 | ✅ (ถ้าธงเปิด) |
| กู้คืนไฟล์ shell rollback | ❌ | ✅ | ✅ |
| สร้าง token สมาชิก | ❌ | ❌ | ✅ ผ่าน CLI |

## ข้อจำกัดที่ต้องเข้าใจตรง (จากข้อเสนอ §5)
- Config Manager ตัวจริงยัง**ไม่มี Remote Deploy** — การย้ายไฟล์โค้ดมาเครื่อง Node ใช้ zip + SHA-256 ตรวจรับสองฝั่ง (เอาเลขแฮชจาก SPADA มาเทียบเอง)
- สิทธิ์ "UAC (คำสั่ง admin ของ Windows)" ปรากฏให้**คนกดเองเสมอ** — AI กดแทนไม่ได้เชิงเทคนิค (การออกแบบของ Microsoft)
- lsass/system (พื้นที่ OS วิกฤต) — **ปิดถาวรในโค้ด ไม่มีธงปลด** — เจอใครบอกปลดได้คือผิดชุดโค้ด