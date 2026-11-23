# OneVault Interactive Agent Engine ⚡

> สถานะตรวจล่าสุด 6 ตุลาคม 2026: **Pilot หน้าเว็บ/เดสก์ท็อป + ธง WO-018 + WO-019 (A1 tiering / B1 config guard / C1 สิทธิ์+ตรวจย้อน / M1–M4 ตัวจำ-สมองสำรอง-โหมดแชท) ผ่านบน server จริง :8205** — regression มาตรฐาน 18 โมดูล 248/248 OK (skipped=1 ตามเดิม) · Windows UIA ผ่าน live เฉพาะ read-only เพิ่ม [desktop input แบบมีคนกำกับ](service/DESKTOP-INPUT-TH.md) ดู [ขอบเขตและวิธีเปิดบริการ](service/README-TH.md) ก่อนใช้งาน ตัวเลขความเร็วด้านล่างไม่ใช่การรับรอง end-to-end SLA
## Multi-Layer Interactive Automation Engine (Client Edition) — browser-engine-desktop 0.5 (อัปเดต 6 ต.ค. 2026)

**OneVault Interactive Agent Engine** คือชุดขับเคลื่อนเบราว์เซอร์อัตโนมัติความเร็วสูง (High-Speed Interactive Automation) ตามสถาปัตยกรรมแยกส่วนระหว่าง **สมองคิด (Slow Brain - Planner AI บน DGX Qwen3.8-27B)** กับ **สมองขยับ (Fast Brain - Local Execution Engine บน Playwright)** พร้อมระบบการมองเห็น **Level 4 Vision (On-Premise VLM บน DGX Spark)** และการกำกับดูแลด้วย **OneVault Data & Fact Integrity Engine**

---

## 🌟 จุดเด่นและสถาปัตยกรรมหลัก

### 1. การค้นหาเป้าหมาย 5 ลำดับชั้น (5-Level Interaction Router)
ระบบจัดลำดับความแม่นยำและความเร็วจากบนลงล่าง:
1. **Level 1 — Direct API:** เรียก API ปลายทางตรง ๆ อย่างปลอดภัย (`Strategy.DIRECT_API`) ภายใต้กฎเหล็กความปลอดภัย 9 ข้อ (ปิดเป็นค่าเริ่มต้น, ตัววางแผนห้ามแต่ง API call เอง, จำกัดเฉพาะโฮสต์ที่อนุญาต, ห้าม redirect ข้ามโฮสต์, อ่านกุญแจจากไฟล์เท่านั้น, จัดชั้นความเสี่ยง Risk 0/2/3, ผ่านด่านอนุมัติ, มีคีย์กันซ้ำ, และบันทึก `FactApiCallExecuted` ใน OneVault)
2. **Level 2 — DOM Locator:** ใช้ Selector / Data-testid / ID / Index ที่สรุปจากหน้าเว็บ (< 15ms)
3. **Level 3 — Accessibility Tree:** ดึงโครงสร้างความหมาย UI (`role`, `name`, `bounds`) เมื่อ DOM ซับซ้อน
4. **Level 4 — Vision Grounding (VLM On-Premise):** เชื่อมต่อโมเดลภาพในองค์กร (Qwen2.5-VL 7B บน DGX Spark) สำหรับระบุพิกัดเป้าหมายที่เป็นภาพหรือ canvas โดยมีเกราะกำบัง LAN-only (ปิด link-local 169.254.x.x และ resolve โดเมนภายใน), บังคับเกณฑ์ความเชื่อมั่นขั้นต่ำ (อ่านจาก `vision_min_confidence` ใน config), ป้องกันภาพหลุดด้วย SHA-256, และมีนโยบาย `keep_alive: 5m` ระหว่างงาน พร้อมฟังก์ชันปลดปล่อย VRAM คืนสู่ 0 GiB หลังงานเสร็จ (`unload_model()`)
5. **Level 5 — Windows UIA (Read-Only Phase):** โมดูลอ่านโครงสร้าง UI Automation ของหน้าต่างเดสก์ท็อปที่ได้รับอนุญาต (`perception/desktop_tree.py`, `execution/windows_uia.py`, `policy/uia_policy.py`) ภายใต้กฎเหล็กความปลอดภัย GR1–GR9 *(เฟสนี้เป็นแบบอ่านอย่างเดียว ห้ามคลิก ห้ามพิมพ์ ห้ามส่ง input เด็ดขาด และยังไม่ต่อเข้ากับตัวจัดเส้นทางหรือ orchestrator)*

### 1.1 สถาปัตยกรรม Level 1 Direct API & กฎเหล็กความปลอดภัย (WO-015 / WO-015.1)
การเชื่อมต่อ API ปลายทางตรงมีความเสี่ยงสูงเนื่องจากเกิดผลทันทีและย้อนกลับไม่ได้ ระบบจึงวางกลไกกำกับดูแลอย่างเข้มงวด:
- **ตัววางแผนห้ามแต่ง API call เอง (GR2):** ข้อกำหนดของ API ทั้งหมดต้องมาจาก "สูตรการเรียก" (Recipes) ที่ลูกค้ากำหนดไว้ล่วงหน้าในไฟล์ `api_recipes.json` เท่านั้น โมเดลภาษา (Planner) ทำได้เพียงเลือกชื่อสูตรและส่งค่าพารามิเตอร์ที่สูตรอนุญาต ห้ามนำ URL, Method หรือ Header มาจากเนื้อหาหน้าเว็บหรือข้อความที่โมเดลแต่งขึ้นโดยเด็ดขาด ป้องกันการถูกโจมตีด้วย Prompt Injection จากหน้าเว็บอันตราย
- **รายชื่อโฮสต์ที่อนุญาต & ปิดการ Redirect ข้ามโฮสต์ (GR3):** เรียกได้เฉพาะโฮสต์ใน `direct_api_allowed_hosts` (ค่าเริ่มต้นคือ `[]` รายการว่าง) รองรับทั้งการระบุชื่อโฮสต์โดเมนล้วน (เช่น `api.internal.local`) และการผูกเจาะจงพอร์ต (เช่น `127.0.0.1:8080`), ปฏิเสธ link-local `169.254.0.0/16` และหากเซิร์ฟเวอร์ตอบ 301/302 ข้ามโฮสต์ ระบบจะปฏิเสธคำขอทันที
- **การบังคับ HTTPS & การควบคุม allow_insecure_http:** การเรียกภายนอกถูกบังคับให้ใช้ `https://` เท่านั้น (อนุญาต `http://` เฉพาะ Loopback `127.0.0.1`, `localhost`, `::1`) เว้นแต่สูตรจะระบุ `allow_insecure_http: true` ชัดเจน โดยการเรียกที่ไม่เข้ารหัสจะถูกบันทึกร่องรอยในฟิลด์ `insecureHttp: true` ของ Fact `FactApiCallExecuted` ทุกครั้งเพื่อการตรวจสอบย้อนหลัง
- **ความลับและความปลอดภัยของโทเคน (GR4):** กุญแจและโทเคนอ่านจากไฟล์บนดิสก์ที่กำหนดเส้นทางไว้เท่านั้น (`auth_token_file`) จะไม่มีการบันทึกโทเคนลงใน trace, Fact, log หรือบนหน้าจอ
- **การจัดชั้นความเสี่ยง & ด่านอนุมัติ (GR5 & GR6):**
  - `GET` / `HEAD`: จัดเป็น **Risk 0 (Safe)**
  - `POST` / `PUT` / `PATCH` / `DELETE`: จัดเป็น **Risk 2 (Confirm)** เป็นอย่างต่ำ
  - หากชื่อสูตร, path หรือพารามิเตอร์มีคำกลุ่มการเงินหรือทำลายข้อมูล (`จ่าย`, `โอน`, `ชำระ`, `สั่งซื้อ`, `ยกเลิก`, `ลบ`, `ปิดบัญชี`, `ถอน`, `เติมเงิน`, `pay`, `purchase`, `withdraw`, `cancel`, `close`, `account`, `order`, `create`, `refund`, `transfer`, `payment`, `charge`, `delete`): จัดเป็น **Risk 3 (Approval)**
  - Action ที่ระดับความเสี่ยง >= 2 ต้องรอการอนุมัติผ่านระบบ `/api/approve/<task>/<step>` (หรือตรวจสอบที่ `/api/approvals`) ก่อนเสมอ หากไม่อนุมัติ คำขอจะไม่ถูกส่งออกเด็ดขาด (จำนวนคำขอที่ปลายทางเป็น 0)
- **คีย์กันซ้ำ (Idempotency Key) & ไม่มีการลองใหม่อัตโนมัติ (GR7):** คำขอที่เปลี่ยนสถานะจะแนบ `Idempotency-Key` ที่คำนวณจาก `task_id` และหมายเลขขั้นตอนเสมอ และไม่มีการลองใหม่อัตโนมัติ (No Automatic Retries) ทั้งสิ้น เพื่อป้องกันผลข้างเคียงซ้ำซ้อนที่ไม่พึงประสงค์
- **OneVault FactApiCallExecuted (GR8):** เมื่อเรียก API เสร็จสิ้น จะบันทึก Fact ชนิด `FactApiCallExecuted` ลง OneVault โดยเก็บเฉพาะชื่อสูตร, โฮสต์, path ที่ตัด query, รหัสสถานะ, เวลา, ระดับความเสี่ยง, สถานะการเข้ารหัส (`insecureHttp`) และค่าแฮช SHA-256 ของ Request Body และ Response Body (ไม่เก็บข้อมูลดิบหรือ header)
- **ปิดเป็นค่าเริ่มต้น (GR1):** ตั้งค่า `direct_api_enabled: false` ใน `config.json` เป็นค่าเริ่มต้น

### 1.2 สถาปัตยกรรม Level 5 Windows UIA เฟสที่ 1 (อ่านอย่างเดียว) & กฎเหล็กความปลอดภัย (WO-016)
การทำงานในระดับ Windows UI Automation เป็นส่วนที่มีความเสี่ยงสูงสุดเพราะออกนอกกรอบของเบราว์เซอร์ไปคุมระบบปฏิบัติการและโปรแกรมอื่นได้ ในเฟสที่ 1 นี้จึงจำกัดให้เป็น **การอ่านอย่างเดียว (Read-Only) ห้ามคลิกห้ามพิมพ์โดยเด็ดขาด**:
- **ข้อห้ามสูงสุด ห้ามส่ง Input เด็ดขาด (GR1):** เมธอดเขียน input ทุกตัวของ `WindowsUIAAdapter` (`click`, `double_click`, `right_click`, `mouse_move`, `drag_and_drop`, `type_text`, `press_key`, `hotkey`, `scroll`) คืนค่า `False` และบันทึกนโยบายห้าม โดย `mouse_move` ถูกถอด `SetCursorPos` ออก ตำแหน่งเคอร์เซอร์ก่อน/หลังเรียกต้องเท่ากัน และข้อความในหน้าต่างต้องไม่เปลี่ยน การเปิดให้คลิกหรือพิมพ์จะเป็นใบงานแยกในอนาคตที่ต้องได้รับอนุมัติจาก CHATCHAI ก่อนเท่านั้น
- **ปิดเป็นค่าเริ่มต้น (GR2):** ตั้งค่า `uia_enabled: false` ใน `config.json` เมื่อปิดระบบจะไม่เปิดดูหน้าต่างใด ๆ ทั้งสิ้น
- **รายชื่อโปรแกรมที่อนุญาต (GR3):** อ่านได้เฉพาะหน้าต่างที่ตรงกับ `uia_allowed_apps` ใน `config.json` (ระบุ process name และ window title regex) โดยค่าเริ่มต้นเป็นรายการว่าง `[]` แปลว่าอ่านอะไรไม่ได้เลยจนกว่าลูกค้าจะกำหนดเอง
- **รายการห้ามที่รายชื่ออนุญาตลบล้างไม่ได้ (GR4):** หน้าต่างยกระดับสิทธิ์ (UAC / `consent.exe`), หน้าต่างข้อมูลรับรอง (`credentialuibroker.exe`, `credwiz.exe`, `Windows Security`, `ระบบความปลอดภัยของ Windows`), หน้าจอล็อก (`logonui.exe`, `lockapp.exe`), และโปรเซสที่มีสิทธิ์สูงกว่ากระบวนการของเรา ถูกปฏิเสธทันทีแม้จะระบุไว้ในรายชื่ออนุญาต
- **ช่องรหัสผ่าน (GR5):** องค์ประกอบที่เป็นช่องรหัสผ่าน (`CurrentIsPassword == True`) ห้ามอ่านค่าเด็ดขาด โดยจะบันทึกเพียงสถานะว่ามีช่องชนิดนี้อยู่ (`is_password: True`, `value: None`) ไม่เก็บแม้แต่ความยาวของรหัสผ่าน
- **การปกป้องความเป็นส่วนตัว (GR6):** Trace และ Fact เก็บได้เฉพาะ Role, ค่าแฮช SHA-256 ของข้อความ, พิกัดและขนาด (`bounds`), และ Automation ID โดยไม่มีการเก็บข้อความจริงหรือภาพหน้าจอของหน้าต่าง
- **ห้ามส่งข้อมูลออกนอกเครื่อง (GR7):** ข้อมูลโครงสร้างหน้าต่างเดสก์ท็อปคงอยู่เฉพาะบนเครื่องลูกข่าย ไม่ส่งออกไปยังโมเดลบนคลาวด์ภายนอก
- **หลักฐานการอ่าน FactDesktopWindowRead (GR8):** ทุกครั้งที่มีการอ่านหน้าต่าง จะบันทึก Fact ชนิด `FactDesktopWindowRead` ลง OneVault ด้วยสัญญา C1/C2 (RFC 8785) บันทึกชื่อโปรเซส, แฮชชื่อหน้าต่าง, จำนวนองค์ประกอบ, และแฮชโครงสร้าง
- **แยกตัวจัดเส้นทาง (GR9):** ห้ามต่อ Level 5 เข้ากับ `engine/router.py` หรือ `engine/orchestrator.py` ในใบงานนี้ ระบบหลักยังคงค้นหาเป้าหมายเฉพาะ Level 1–4 เหมือนเดิม

### 2. แยกสมองคิด (Slow Brain) ออกจากสมองขยับ (Fast Brain)
- **Slow Brain (Planner AI บน DGX Qwen3.8-27B):** คิดวางแผนระดับงาน (Task Plan) พร้อมบริบทสรุปหน้าเว็บ (Page Summary ไม่เกิน 6,000 ตัวอักษร) พร้อมความสามารถ Replan ไม่เกิน 2 ครั้งเมื่องานสะดุด
- **Fast Brain (Local Execution Engine):** จัดการขยับเมาส์ พิมพ์ข้อความ ดึงข้อมูล (extract) และตรวจสถานะ (verify) ด้วยความเร็วเฉลี่ยระดับ **0.04–50ms ต่อ Action**

### 3. Conditional Macro Engine & State Verifier
- รองรับการรันชุดคำสั่งต่อเนื่อง และรองรับ Action แบบอ่านอย่างเดียว (`extract` เพื่อดึงคำตอบพร้อมหลักฐาน selector/text ไม่เกิน 300 ตัวอักษร, `verify` เพื่อตรวจข้อความโดยไม่คลิก)
- มีระบบ **Risk Gate (Risk 0 ถึง Risk 3)** บังคับตรวจก่อนทุก Action: คำสั่งระดับ Risk 2/3 ต้องได้รับการอนุมัติผ่าน API `POST /api/approve/<task>/<step>` เท่านั้น
- **V9 Inverted Risk Classifier (Deny-by-Default):** เมื่ออยู่ในบริบทการเงิน/ชำระเงิน (`checkout`, `payment`, `cart`, `order`, `billing`) ทุกการกระทำที่ไม่ใช่การอ่านอย่างเดียว (รวมถึงปุ่มไอคอนไม่มีชื่อ, ปุ่มไม่ทราบที่มา, และคำสั่งการเงิน) จะถูกจัดเป็น Risk >= 2 ทันที เพื่อป้องกันการหลุดคลิกโดยพลการ
- **Vision Coordinate Safety Barrier:** พิกัดจาก Vision ถูกห้ามใช้กับ Action ที่ระดับความเสี่ยง >= 2 โดยเด็ดขาด หากองค์ประกอบความเสี่ยงสูงหาจาก DOM หรือ Accessibility ไม่ได้ ระบบจะคืน `no_grounded_target` และไม่คลิกเด็ดขาด
- ทุกการกระทำมีการตรวจสอบ **Precondition** และ **Postcondition** และบันทึก Structured Trace เป็น JSONL ใน `data/traces/<task_id>.jsonl` โดยเก็บภาพหน้าจอเป็นค่าแฮช SHA-256 เท่านั้น

### 4. สถาปัตยกรรม OneVault & Fact Integrity (0.4)
ระบบเชื่อมต่อกับ OneVault สำหรับเก็บหลักฐานการทำงานดิจิทัลที่ไม่สามารถปลอมแปลงได้ (Tamper-evident Evidence):
- **ชนิดของ Fact (Fact Types):**
  1. `FactWebTaskExecuted`: ออกเมื่อภารกิจอัตโนมัติสำเร็จ บันทึก `task_id`, `url`, `goal`, `steps_count`, สรุปผลการทำงาน และค่าแฮช SHA-256
  2. `FactWebActionApproved`: ออกเมื่อมีการอนุมัติการกระทำความเสี่ยงสูง (Risk 2/3) ผ่านระบบ บันทึก `approval_id`, `task_id`, `step_index`, `risk_level`, `approved_by`, เวลา และแฮช
  3. `FactDataErased`: ออกเมื่อมีการลบข้อมูลประวัติการทำงานตามสิทธิ PDPA เพื่อเป็นหลักฐานรับรองการทำลายข้อมูล
- **สัญญา C1/C2 (Contracts):**
  - **C1 Contract:** บังคับสภาพแวดล้อม `ONEVAULT_INSTANCE` (regex: `^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$`) และ `ONEVAULT_ACTOR` (regex: `^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$`) หากไม่ตั้งค่าหรือรูปแบบไม่ถูกต้อง ระบบจะไม่ส่ง Fact ไปยังคลังกลางเพื่อรักษาความถูกต้องของข้อมูล
  - **C2 Contract:** โครงสร้าง Fact มาตรฐาน ต้องมี `factId`, `factType`, `schemaVersion`, `occurredAt`, `contentHash` (SHA-256) และฟิลด์เมทาดาทาครบถ้วน
- **Spool Queue & Non-Blocking Design:**
  - Fact ทั้งหมดจะถูกบันทึกแบบ At-Least-Once ลงในโฟลเดอร์คิวท้องถิ่น (`data/vault_spool/`) ก่อนเสมอ
  - การส่งข้อมูลไปยังคลังกลาง (Central Repository) ทำงานแบบ Asynchronous ในเธรดเบื้องหลังพร้อม Timeout ไม่บล็อกงานของผู้ใช้แม้คลังกลางจะชะลอหรือหยุดให้บริการ

### 5. นโยบายคุ้มครองข้อมูลส่วนบุคคล (PDPA) & การจำกัดอายุข้อมูล 90 วัน
- **สิทธิในการขอให้ลบข้อมูล (Right to Erasure):**
  - รองรับการลบประวัติงานและภาพหน้าจอที่เกี่ยวข้องทั้งหมดตามราย `task_id` ผ่าน API `POST /api/pdpa/erase`
  - พร้อมออกเอกสารรับรอง `FactDataErased` เข้าคิว Spool อัตโนมัติ
- **การจำกัดอายุข้อมูล 90 วัน (90-Day Retention):**
  - ระบบตั้งค่าเริ่มต้นล้างไฟล์ Trace ที่มีอายุเกิน 90 วัน (`retention_days=90`) อัตโนมัติผ่าน `POST /api/retention/purge`
  - **การบังคับยืนยันก่อนล้างทั้งหมด (Mandatory PURGE_ALL Confirmation):** หากระบุ `retention_days=0` (เพื่อล้างข้อมูลประวัติทั้งหมด) ระบบบังคับต้องส่ง `{"confirm": "PURGE_ALL"}` ใน Payload หากไม่มีจะปฏิเสธคำขอด้วยรหัส 400 ทันที
- **ข้อกำหนดรูปแบบ Task ID:**
  - `task_id` ในทุก API ต้องเป็นไปตาม regex อย่างเคร่งครัด: `^[A-Za-z0-9._-]{1,128}$`
  - ป้องกันช่องโหว่ Path Traversal (`..`, `/`, `\`) และอักขระแปลกปลอม

### 6. Human Takeover Mode & Anti-Bot Safety Guard
- ตรวจจับหน้ากั้นบอทจากสัญญาณเฉพาะทาง:
  - iframe ของ `challenges.cloudflare.com`, `recaptcha`, `hcaptcha`
  - หน้า Cloudflare "Just a moment" ที่มีสถานะ 403/503 หรือ Ray ID
  - พาธ `/verify` บนโดเมน Shopee
  - ความเชื่อมั่นของโมเดลภาพต่ำกว่าเกณฑ์ (`low_vision_confidence < min_confidence`)
- เมื่อตรวจพบ ระบบจะหยุดรอสัญญาณ Takeover (`wait_if_paused` สูงสุด 5 นาที) และเมื่อผู้ใช้แก้เสร็จแล้วกด Resume ระบบจะอ่านหน้าเว็บใหม่และทำงานต่อจนจบ

### 7. มาตรการความปลอดภัยและการเข้าถึง (Security Hardened)
- API ฟังเฉพาะ `127.0.0.1:8205` เท่านั้น ปิดการเข้าถึงจากเครือข่ายภายนอก
- ทุก REST Endpoint บังคับส่ง header `X-Desktop-Token` ตรงกับ token ใน `%USERPROFILE%\.browser-engine-desktop\token`
- ไฟล์ token ถูกจำกัดสิทธิ์ระดับไฟล์ (ACL) ให้เฉพาะผู้ใช้ปัจจุบัน (Current User Only) ห้ามแสดงค่า token ในหน้าจอหรือใน log
- ปลายทางโมเดลภาพบังคับเป็น Private LAN เท่านั้น ปฏิเสธคลาวด์ภายนอกทันที (Zero Leakage)
- จัดเก็บภาพดิบชั่วคราวเฉพาะใน `data/screenshots/` และล้างทิ้งอัตโนมัติภายใน 24 ชม.

---

## 🖥️ คำแนะนำขนาดหน้าจอ (Screen Resolution Guidelines)

จากผลการทดสอบความแม่นยำของโมเดลสายตา (Vision Grounding) บน DGX Spark:
- **ขนาดหน้าจอขั้นต่ำ (Minimum Required):** `640x400` พิกเซล — ระบบมีฟังก์ชัน Dynamic Viewport Scaling 2x อัตโนมัติเพื่อขยายภาพให้โมเดลอ่านพิกัดได้แม่นยำ
- **ขนาดหน้าจอที่แนะนำ (Recommended):** `1280x800` พิกเซล หรือ `1920x1080` พิกเซล ขึ้นไป — เพื่อความแม่นยำสูงสุดในการจัดวางองค์ประกอบและการจำแนกข้อความ (ความแม่นยำ 100%)

---

## ⚠️ ข้อจำกัดสำหรับลูกค้าและข้อตกลงการใช้งาน (Limitations & Constraints)

1. **ฮาร์ดแวร์ GPU สำหรับ Level 4 Vision:** โมเดลภาพ Qwen2.5-VL 7B ทำงานบนเครื่อง DGX Spark (VRAM >= 24GB) ภายในวง LAN องค์กร หากเครื่องลูกข่ายไม่มีการเชื่อมต่อ DGX ระบบจะทำงานเฉพาะ Level 2–3 (DOM + Accessibility) เท่านั้น
2. **ข้อจำกัด Level 5 Windows UIA (เฟสอ่านอย่างเดียว):** ปัจจุบันทำงานในโหมดอ่านโครงสร้างอย่างเดียว (Read-Only) ห้ามคลิก ห้ามพิมพ์ ห้ามขยับเมาส์ หรือส่ง input ใด ๆ ทั้งสิ้น การเปิดให้คลิกหรือพิมพ์เป็นข้อตกลงและใบงานแยกในอนาคตที่ต้องได้รับอนุมัติจาก CHATCHAI ก่อนเท่านั้น และในเฟสนี้ยังไม่มีการต่อเข้ากับ Interaction Router หรือ Orchestrator หลัก รายชื่อโปรแกรมที่อนุญาต `uia_allowed_apps` รองรับ wildcard `"*"` ซึ่งจะอนุญาตทุกโปรแกรมบนเดสก์ท็อป (ยกเว้นหน้าต่างต้องห้ามถาวร เช่น UAC และ Credential Manager) โดยลูกค้าควรระบุชื่อโปรเซสหรือกรองชื่อหน้าต่างเฉพาะที่จำเป็นตามหลักสิทธิ์ขั้นต่ำ (Least Privilege)
3. **ข้อกำหนด Level 1 Direct API:** เชื่อมต่อเข้ากับ Orchestrator หลักแล้วผ่าน Action `api_call` (`Strategy.DIRECT_API`) ภายใต้กฎเหล็กความปลอดภัย 9 ข้อ (ปิดเป็นค่าเริ่มต้น, เรียกผ่านสูตรที่กำหนดล่วงหน้าเท่านั้น, จำกัดโฮสต์ที่อนุญาต, ห้าม redirect ข้ามโฮสต์, อ่านโทเคนจากไฟล์, จัดชั้นความเสี่ยง Risk 0/2/3, ผ่านด่านอนุมัติ, บังคับ HTTPS, และบันทึก `FactApiCallExecuted` ใน OneVault)
4. **ห้ามใช้พิกัดภาพกับงานเสี่ยงสูง:** พิกัดจากโมเดลภาพห้ามใช้สั่งคลิกในงานระดับ Risk 2 ขึ้นไป (เช่น การสั่งซื้อ โอนเงิน หรือลบข้อมูล) เด็ดขาด

---

## 🚀 การเปิดใช้งาน (Getting Started)

ระบบรองรับการเปิดใช้งานบนเครื่อง Windows ทันทีโดยไม่ต้องติดตั้ง `uv` ล่วงหน้า:

### เปิดใช้งานผ่าน Launcher
1. ดับเบิลคลิกที่ไฟล์ **`run.cmd`** (หรือคลิกขวาเลือก Run with PowerShell ที่ **`run.ps1`**)
2. ระบบจะตรวจสอบความพร้อมอัตโนมัติ:
   - ตรวจหา Python 3.10+ ในเครื่อง
   - สร้างสภาพแวดล้อมจำลอง `.venv` และติดตั้ง `playwright`, `pillow`, และเบราว์เซอร์ `chromium` ให้อัตโนมัติในการเปิดครั้งแรก (พร้อมระบบตรวจสอบและติดตั้งซ่อมแซมอัตโนมัติหากแพ็กเกจไม่ครบ)
   - **หมายเหตุเรื่องเวลาการรันครั้งแรก:**
     - บนเครื่องที่เคยมี Chromium ในแคชของระบบอยู่แล้ว: การเปิดครั้งแรกใช้เวลาประมาณ **20–22 วินาที**
     - บนเครื่องใหม่ที่ไม่เคยดาวน์โหลด Chromium มาก่อน: ตัวติดตั้งจะต้องดาวน์โหลด Chromium เพิ่มเติมประมาณ 150 MB ซึ่งอาจใช้เวลา **2–5 นาที** ขึ้นอยู่กับความเร็วการเชื่อมต่ออินเทอร์เน็ต
   - ตรวจสอบพอร์ต 8205 และการเชื่อมต่อ DGX Spark
   - สร้าง Desktop Token และกำหนดสิทธิ์ความปลอดภัยให้เฉพาะผู้ใช้ปัจจุบัน
3. หน้าจอควบคุม (Dashboard) จะเปิดขึ้นที่ **`http://127.0.0.1:8205`** อัตโนมัติ
4. การเปิดใช้งานครั้งถัดไปจะเปิดได้ทันทีภายในไม่กี่วินาที (< 4 วินาที) โดยไม่ติดตั้งซ้ำ

---

## 📡 รายการ API Endpoints (Complete API Reference)

ทุก endpoint ภายใต้ `/api/*` ต้องแนบ Header: `X-Desktop-Token: <token_value>` เสมอ

| Method | Endpoint | สิทธิ์เข้าถึง | คำอธิบาย | ตัวอย่าง Payload / ตอบกลับ |
|---|---|---|---|---|
| `GET` | `/api/status` | บังคับ Token | ตรวจสอบสถานะการทำงานของเครื่องยนต์และ DGX AI Brain | `{ "status": "online", "vllm_ready": true, "vllm_message": "พร้อมใช้งาน (vLLM online)", "channel": "chrome" }` |
| `GET` | `/api/vault/status` | บังคับ Token | ตรวจสอบสถานะคลัง OneVault, คิว Spool, และการซิงค์ | `{ "enabled": false, "spool_pending": 0, "last_synced_at": null }` |
| `GET` | `/api/tasks` | บังคับ Token | เรียกดูรายการงานทั้งหมดที่เคยรัน | `[ { "id": "task_1", "goal": "ค้นหาสินค้า", "status": "completed" } ]` |
| `GET` | `/api/task/<task_id>` | บังคับ Token | เรียกดูรายละเอียดและสถานะของงานตาม `task_id` | `{ "id": "task_1", "steps": [...], "status": "completed" }` |
| `GET` | `/api/approvals` | บังคับ Token | เรียกดูรายการคำสั่งระดับความเสี่ยงสูงที่รอการอนุมัติ | `[ { "task_id": "...", "step": 3, "risk_level": 2 } ]` |
| `POST` | `/api/task` | บังคับ Token | เริ่มต้นสั่งให้ Agent ทำงานอัตโนมัติ | Body: `{"url": "https://...", "goal": "ค้นหาสินค้า", "max_steps": 15}` |
| `POST` | `/api/approve/<task>/<step>` | บังคับ Token | อนุมัติคำสั่งระดับความเสี่ยงสูงให้ดำเนินการต่อ | URL params: `task_id`, `step` (เช่น `/api/approve/task_1/3`) |
| `POST` | `/api/reject/<task>/<step>` | บังคับ Token | ปฏิเสธคำสั่งระดับความเสี่ยงสูง | URL params: `task_id`, `step` (เช่น `/api/reject/task_1/3`) |
| `POST` | `/api/takeover` | บังคับ Token | สั่งหยุดพัก Agent และเข้าสู่โหมดแทรกแซงโดยมนุษย์ | Body: `{"reason": "User requested manual control"}` |
| `POST` | `/api/resume` | บังคับ Token | สั่งให้ Agent กลับมาทำงานต่อหลังมนุษย์แทรกแซงเสร็จ | Response: `{"status": "agent_resumed"}` |
| `POST` | `/api/vault/sync` | บังคับ Token | สั่งส่ง Fact ที่ค้างในคิว Spool ไปยังคลัง OneVault ทันที | Response: `{"status": "sync_complete", "flushed": 0, "failed": 0, "pending": 0}` |
| `POST` | `/api/pdpa/erase` | บังคับ Token | ลบข้อมูล Trace, ภาพถ่าย และไฟล์ดาวน์โหลดตามสิทธิ PDPA | Body: `{"task_id": "task_12345", "reason": "PDPA Request"}` |
| `POST` | `/api/retention/purge` | บังคับ Token | ล้างข้อมูล Trace ที่เก่ากว่าจำนวนวันที่กำหนด (90 วัน) | Body: `{"retention_days": 90}` *(หาก days=0 ต้องแนบ `{"confirm": "PURGE_ALL"}`)* |
| `POST` | `/api/open-browser` | บังคับ Token | เปิดเบราว์เซอร์ Chrome สำหรับให้ผู้ใช้ล็อกอินล่วงหน้า | Body: `{"url": "https://...", "profile": "default_user"}` |
| `POST` | `/api/shell-run` | บังคับ Token | รันคำสั่ง OS จริง (ธง `shell_execution_enabled`; sandbox+chain-sign+manifest ครบตามธง) | Body: `{"command": "dir", "cwd": null, "elevated": false}` → exit_code/stdout/stderr/duration_ms |
| `POST` | `/api/chat-task` | operator ขึ้นไป | **โหมดแชท**: ข้อความธรรมชาติ → งาน (ธง tiering) — ไม่มี host/URL ในข้อความ = 400 ระบบไม่เดาเว็บแทนผู้ใช้ · เสี่ยง/นอก allowlist = 403 | Body: `{"goal": "หาราคาหนังสือเล่มแรก", "host": "books.toscrape.com", "max_steps": 3}` → `{task_id, status:"queued", mode:"chat"}` |
| `GET` | `/api/memory` | ทุก role | อ่านบริบทธุรกิจราย Node (Task Memory — จำข้อเท็จจริง ไม่จำคำตอบ) | `[{"key": "ซัพพลายเออร์หลัก", "value": "..."}]` |
| `POST` | `/api/memory/add` · `/api/memory/remove` | **admin เท่านั้น** | เพิ่ม/ลบ (soft delete) บริบทธุรกิจ — ฉีดเข้า planner รอบถัดไปอัตโนมัติ (เพดาน 1200 อักษร) | Body: `{"key": "...", "value": "..."}` |
| `GET` | `/api/roles` | ทุก role | รายชื่อสมาชิก+สถานะ active/revoked/expired (ไม่มี token ในผลเด็ดขาด) | `[{"name", "role", "state", "expires"}]` |
| `POST` | `/api/roles/add` | **admin เท่านั้น** | สร้าง token ผู้ใช้ใหม่ (แสดงครั้งเดียว — เจ้าของจดสำรองทันที) · expires fail-closed | Body: `{"name", "role": "viewer\|operator\|admin", "expires": "2026-12-31" หรือ ""}` |
| `POST` | `/api/roles/revoke` | **admin เท่านั้น** | ระงับ token ทันที — token เดิมใช้ไม่ได้ใน request ถัดไป (401) · re-issue = add ชื่อเดิมซ้ำ | Body: `{"name": "...", "reason": "..."}` |
| `GET` | `/api/audit/list` · `GET /api/audit/manifest?file=app-YYYYMMDD.jsonl` | ทุก role | ระเบียนคำสั่ง+ตรวจลายเซ็นย้อน (chain-sign) — verify_chain 1 คลิกจากหน้า /audit | ไฟล์ละเมิด path ตอบ 400 |

---

## 🧪 การรันชุดทดสอบ (Automated Regression Test Suites)

รันชุดทดสอบความถูกต้องผ่านคำสั่ง Python Unittest มาตรฐานโดยตรง:

```powershell
cd D:\karaok\ai-platform-kit\apps\browser-engine-desktop

# 1. รันชุดทดสอบรายโมดูล (ระบุจำนวนข้อตามจริง รวม 118 ข้อ):
python -m unittest tests.test_wo009_suite          # 16 ข้อ (ความปลอดภัย, Token, Anti-Bot, Live Web)
python -m unittest tests.test_wo011_suite          #  9 ข้อ (Level 4 Vision Grounding, LAN-only)
python -m unittest tests.test_wo011_1_suite        #  6 ข้อ (DGX Model Call, Parser, VRAM Benchmark)
python -m unittest tests.test_wo011_2_suite        #  7 ข้อ (Coordinate Normalization, Precision)
python -m unittest tests.test_wo011_3_suite        #  3 ข้อ (Multi-Resolution Scaling, 640x400)
python -m unittest tests.test_wo012_suite          # 10 ข้อ (OneVault Integration, C1/C2 Contracts)
python -m unittest tests.test_wo012_1_suite        #  6 ข้อ (PDPA Erasure, Sanitization, Non-Blocking)
python -m unittest tests.test_wo012_2_suite        #  5 ข้อ (Purge Confirmation, Regex Task ID)
python -m unittest tests.test_wo015_suite          # 14 ข้อ (Level 1 Direct API Hardening & Idempotency)
python -m unittest tests.test_wo016_suite          # 13 ข้อ (Level 5 Windows UIA Read-Only & Password Shield)
python -m unittest tests.test_wo017_negative_suite # 23 ข้อ (Negative Fault Injections, Sentinel, Sandbox Persistence & Exception Safety)
python -m unittest tests.test_launcher             #  6 ข้อ (Windows Launcher run.cmd Exit Code & Sandbox)

# 2. รันชุดมาตรฐานล่าสุดทั้งหมดพร้อมกัน (18 โมดูล — 248 ข้อ, skipped=1 ไฟล์ TAMPERED ตามเดิม):
python -m unittest tests.test_wo019_m1_token_lifecycle tests.test_wo019_m2_task_memory tests.test_wo019_m3_chat tests.test_wo019_m4_endpoints tests.test_wo019_c1_ui tests.test_wo019_planner_tier tests.test_wo019_config_guard tests.test_step3_roles_ed25519 tests.test_step2_sandbox_keysign tests.test_reliability tests.test_recipe_cache tests.test_codex_closeout tests.test_autonomous_session tests.test_guarded_desktop tests.test_launcher tests.test_wo017_12_window_ownership tests.test_wo017_negative_suite tests.test_wo018_shell_unlock

# 3. ชุดโครงเดิม (รายโมดูล — โมดูลเก่าบางชุดถูกแทนที่ด้วยชุดใหม่ด้านบนแล้ว):
python -m unittest tests.test_wo009_suite tests.test_wo011_suite tests.test_wo011_1_suite tests.test_wo011_2_suite tests.test_wo011_3_suite tests.test_wo012_suite tests.test_wo012_1_suite tests.test_wo012_2_suite tests.test_wo015_suite tests.test_wo016_suite tests.test_wo017_negative_suite tests.test_launcher
```

---

## ⚙️ โครงสร้างโฟลเดอร์โครงการ

```text
apps/browser-engine-desktop/
├── server.py                 # Local Web Dashboard & Authenticated REST API (port 8205 บน 127.0.0.1)
├── desktop_engine.py         # Entrypoint รันงาน Multi-Layer Interactive Task
├── config.json               # ค่าคอนฟิก (vLLM URL, planner_timeout_seconds=90, port=8205, direct_api_enabled)
├── api_recipes.example.json  # ตัวอย่างสูตรการเรียก API ปลายทางล่วงหน้า (Level 1 Direct API Recipes)
├── run.cmd                   # Launcher สำหรับ Command Prompt (พร้อมสร้าง .venv อัตโนมัติ)
├── run.ps1                   # Launcher สำหรับ PowerShell (พร้อมสร้าง .venv อัตโนมัติ)
├── README.md                 # เอกสารระบบและการใช้งาน รุ่น 0.5 (สถานะ 6 ต.ค. 2026 — รวมโหมดแชท/Task Memory/revoke roles)
│
├── engine/
│   ├── orchestrator.py       # คุมวงจร Slow Brain (Planner) กับ Fast Brain (Execution) พร้อม Replan
│   ├── router.py             # 5-Level Interaction Router พร้อมรองรับ [index] และ CSS Selector
│   ├── planner.py            # สื่อสารกับ Qwen3.8-27B สร้าง Task & Replan พร้อมตรวจจับ Timeout
│   ├── verifier.py           # State Verifier (Pre/Post-conditions, Dynamic Wait)
│   └── recovery.py           # ตัวตรวจจับ Popup, Cookie (Reject only)
│
├── perception/
│   ├── dom.py                # Page Summary (<=6,000 ตัวอักษร), DOM Tree & Interactive Elements
│   ├── accessibility.py      # ดึง UI Semantics จาก Chrome Accessibility Tree (CDP)
│   ├── screenshot.py         # Capture pipeline (Fast WebP/JPEG + ROI Cropper)
│   └── change_detection.py   # Screen Hash & Frame Diff
│
├── execution/
│   ├── adapter_base.py       # Abstract Base Adapter สำหรับ Browser และ Desktop
│   ├── browser.py            # BrowserAdapter (Playwright CDP Mouse/Keyboard/Ripple Effect)
│   ├── direct_api.py         # DirectApiClient (Level 1 Direct API: ปลอดภัย, Deny-by-default, Idempotency)
│   ├── windows_uia.py        # DesktopAdapter (Windows UI Automation Stubs - โครงร่างระยะถัดไป)
│   ├── coordinates.py        # Coordinate Context (Viewport, DPI scale, Scroll offset)
│   └── macro.py              # Conditional Macro Runner (ตรวจ no_grounded_target อย่างเข้มงวด)
│
├── policy/
│   ├── risk.py               # Risk Policy Engine (จัดชั้นความเสี่ยง Risk 0 ถึง Risk 3)
│   ├── approval.py           # ระบบขออนุมัติ Action (Risk 2/3 และ Profile ล็อกอิน)
│   └── takeover.py           # Human Takeover Manager (ตรวจจับ Bot Wall เฉพาะสัญญาณจริง + Resume)
│
├── models/
│   ├── client.py             # vLLM Client (LAN DGX Qwen3.8-27B พร้อม planner_timeout_seconds)
│   ├── vision.py             # VisionClient (Level 4 VLM On-Premise บน DGX Spark)
│   └── schemas.py            # Schemas สำหรับ Plans, Actions (EXTRACT, VERIFY, API_CALL), Traces
│
├── telemetry/
│   ├── trace.py              # บันทึก Structured JSONL Trace ใน data/traces/<task_id>.jsonl
│   ├── vault.py              # OneVault Integration Manager & Spool Queue Synchronizer
│   └── retention.py          # Data Retention & Purge Policy Manager (PDPA 90 วัน)
│
├── tests/                    # ชุดทดสอบอัตโนมัติครบ 12 ชุด (รวม 118 ข้อ)
└── static/                   # Dashboard UI พร้อมช่องกรอก Token และปุ่ม Take Control
```
