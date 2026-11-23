"""Config Guard (WO-019 B1a): กติกา "แก้กติกาต้องผ่าน Version+Approval" กลายเป็นระบบจริง.

หลักการ (fail-open เฉพาะ "ตรวจไม่ได้" — เดินหน้าได้ตามค่าจริงเสมอ, fail-closed เชิงการแจ้งเตือน):
- ตอน server เปิด: คำนวณ SHA-256 ของ config.json ลงระเบียน (config_baseline.json — ไฟล์เดียวข้าม restart)
- ทุกโอกาสอ่าน config ของส่วน guard (get_cfg_guarded): เทียบ hash ปัจจุบันกับ baseline
  - ตรง = ปกติ
  - **ไม่ตรง = มีการแก้ config.json ตรงข้ามกระบวนการ** → ตอบ warn + บันทึก event log (config_changed_outside_approval)
    + นำเสนอ "ค่าใหม่ยังใช้จริงได้" ตามความคุ้มค่า (ตามหลัก: AI ไม่สิทธิ์ตัดสินกลับค่า — ผู้ดูแลตัดสินเองจากแจ้งเตือน)
- เปิดครั้งแรกที่ไม่มี baseline = จดเป็น baseline แรก (เชิงนิรันดร์)
- provision_node ติดตั้ง = ผูก baseline ตาม Config Version ที่อนุมัติ (B1b — hash เดียวกัน)
"""
import hashlib
import json
import time
import pathlib

APP = pathlib.Path(__file__).resolve().parent.parent
BASELINE_FILE = APP / "data" / "config_baseline.json"

_config_hash_cache = None
_last_check = None


def compute_hash(cfg_path=None) -> str:
    p = pathlib.Path(cfg_path) if cfg_path else APP / "config.json"
    try:
        raw = p.read_bytes()
        return hashlib.sha256(raw).hexdigest()
    except OSError:
        return ""


def anchor_baseline(reason: str = "boot"):
    """จด baseline ปัจจุบัน (ครั้งแรกของเครื่อง / ติดตั้งใหม่ผ่าน provision / owner อนุมัติ Version ใหม่แล้ว)."""
    global _last_check
    h = compute_hash()
    if not h:
        return {"ok": False, "error": "config_not_readable"}
    BASELINE_FILE.parent.mkdir(parents=True, exist_ok=True)
    prev = read_baseline()
    BASELINE_FILE.write_text(json.dumps({
        "config_sha256": h,
        "anchored_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "anchor_reason": reason,
        "previous_hash": (prev or {}).get("config_sha256"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    _last_check = None  # WO-019: anchor = ล้างแคชตรวจ — สั่งตรวจสดทันที (กัน "แก้ตรง+จด" ใน 5 วิเดียวกันถูกแคชกลืน)
    return {"ok": True, "config_sha256": h, "anchored": reason, "anchor_reason": reason,
            "previous_hash": (prev or {}).get("config_sha256")}


def read_baseline() -> dict:
    try:
        return json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def check_integrity() -> dict:
    """เทียบ hash ปัจจุบันกับ baseline — **ตรวจสดเสมอ** (ไม่แคช: config เล็ก sha256 เร็ว ~1ms —
    บั๊กจริงที่เคยจับจาก live proof: แคช 5 วิ กลืนการแก้ตรง → รายงาน intact ผิด — จึงขจัดแคช)."""
    return _do_check()


def _do_check() -> dict:
    cur = compute_hash()
    if not cur:
        return {"ok": False, "state": "config_unreadable"}
    base = read_baseline()
    if not base.get("config_sha256"):
        # ครั้งแรกของเครื่อง — จดแล้วถือว่าตรง
        r = anchor_baseline("first_boot")
        return {"ok": True, "state": "baseline_anchored", "config_sha256": cur, **(r or {})}
    if cur == base["config_sha256"]:
        return {"ok": True, "state": "intact", "config_sha256": cur}
    return {
        "ok": False, "state": "outside_of_approval_flow",
        "config_sha256": cur, "baseline_sha256": base["config_sha256"],
        "baseline_anchored_at": base.get("anchored_at"),
        "message": ("config.json ถูกแก้ตรง (นอก Approval/Version flow) — "
                    "ค่าใหม่ยังใช้จริง; ถ้าการแก้นี้มีผู้อนุมัติแล้ว ให้ผู้ดูแลรัน: "
                    "python -c \"from service.config_guard import anchor_baseline; print(anchor_baseline('approved-version-<n>'))\" "
                    "เพื่อจด baseline ใหม่ — ห้ามแก้ตรงโดยไม่มีหลักฐาน"),
    }


def anchor_version(version_note: str):
    """จด baseline ใหม่หลังผ่าน Approval (หน้าที่ผู้ดูแล/ผู้อนุมัติ — ไม่ใช่ AI)."""
    return anchor_baseline(f"approved-version: {version_note}")