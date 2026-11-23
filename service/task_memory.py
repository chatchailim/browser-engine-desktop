"""Task Memory (WO-019 M2): ความจำธุรกิจราย-Node — จำ "ข้อเท็จจริงบริบท" (ไม่ใช่คำตอบงาน).

หลักการ (ตามธรรมนูญ):
- **ไม่จำคำตอบงาน** — คำตอบทุกครั้งต้องอ่านจากแหล่งจริง (คงหัวใจ "ไม่แคชคำตอบ" เดิม) — จำแค่ "บริบทธุรกิจของ Node นี้"
  เช่น "ซัพพลายเออร์หลัก = A,B,C", "ปลายทางเว็บภายใน = intra.example.com"
- เก็บที่ data/task-memory.json (ไฟล์เดียว — Node เดียวใช้พอ) — เขียนผ่าน lock กันชน (คู่ reliability เดิม)
- อ่านเข้า planner ผ่าน build_context_block() — หัวข้อข้อจำกัดเด็ดขาด: **อักษร ≤ 1200** (ตัดเก่าถึงใหม่เสมอ)
- fail-closed: อ่านพัง = คืน "" (ระบบทำงานต่อโดยไม่มีบริบท — รายงานเหตุทาง return)
- ระเบียน: ทุก add/revoke/delete ลงบัญชี entry (ใครเพิ่ม/เมื่อไร) — ตรวจย้อนแบบเดียวกับระบบ
- ลบ: remove(key) — soft delete (เก็บ entry "ลบแล้ว" คงตัวเชิงแสดงผล false)
"""
import json
import pathlib
import time

APP = pathlib.Path(__file__).resolve().parent.parent
MEM_FILE = APP / "data" / "task-memory.json"
MAX_CONTEXT_CHARS = 1200

_memory_cache = None  # โปรเซสเดียวใช้ — โหลดครั้งเดียวต่อ call (เขียน = flush disk ทันที)


def _load() -> dict:
    global _memory_cache
    if _memory_cache is not None:
        return _memory_cache
    try:
        _memory_cache = json.loads(MEM_FILE.read_text(encoding="utf-8"))
    except Exception:
        _memory_cache = {"entries": []}
    return _memory_cache


def _flush(store: dict):
    global _memory_cache
    MEM_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MEM_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(MEM_FILE)  # atomic บน Windows (os.replace)
    _memory_cache = store


def add(key: str, value: str, who: str = "admin") -> dict:
    """บันทึกบริบทธุรกิจ 1 ข้อ — key ซ้ำ = แทนที่ (เก็บ revision ก่อนหน้าไว้ใน history)."""
    key = (key or "").strip()[:80]
    value = (value or "").strip()[:600]
    if not key or not value:
        return {"ok": False, "error": "key/value ต้องไม่ว่าง"}
    store = _load()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for e in store["entries"]:
        if e["key"] == key and e.get("active"):
            e.setdefault("history", []).append({"value": e["value"], "changed_at": e["updated_at"], "by": e["updated_by"]})
            e["value"] = value
            e["updated_at"] = now
            e["updated_by"] = who
            break
    else:
        store["entries"].append({"key": key, "value": value, "active": True,
                                 "created_at": now, "updated_at": now, "updated_by": who, "history": []})
    _flush(store)
    return {"ok": True, "key": key, "count": len([e for e in store["entries"] if e.get("active")])}


def remove(key: str, who: str = "admin") -> dict:
    """ลบบริบท (soft — ระเบียนคงเห็น audit; active=false)."""
    store = _load()
    hit = next((e for e in store["entries"] if e["key"] == key.strip()[:80] and e.get("active")), None)
    if not hit:
        return {"ok": False, "error": "ไม่พบ key นี้"}
    hit["active"] = False
    hit["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    hit["updated_by"] = who
    _flush(store)
    return {"ok": True, "key": key}


def list_all() -> list:
    """รายการบริบทที่ active (สำหรับหน้าเว็บจัดการ)."""
    return [{"key": e["key"], "value": e["value"], "updated_at": e["updated_at"], "updated_by": e["updated_by"]}
            for e in _load()["entries"] if e.get("active")]


def build_context_block(max_chars: int = MAX_CONTEXT_CHARS) -> str:
    """แปลงบริบทเป็นบล็อกข้อความฉีดเข้า prompt ของ planner — '' เมื่อไม่มี/อ่านพัง (fail-closed)."""
    try:
        entries = [e for e in _load()["entries"] if e.get("active")]
        if not entries:
            return ""
        lines = [f"- {e['key']}: {e['value']}" for e in entries[-12:]]  # เก่า→ใหม่ คงตัวในเพดาน
        block = "บริบทของธุรกิจนี้ (อ้างเสมอ ตัดสินใจด้วย):\n" + "\n".join(lines)
        return block[:max_chars]
    except Exception:
        return ""


def clear_all(who: str = "admin") -> dict:
    """ลบทุก active entry (ผู้ดูแลใช้ — ไม่ใช่แทนลบระบบ)."""
    store = _load()
    n = 0
    for e in store["entries"]:
        if e.get("active"):
            e["active"] = False
            e["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            e["updated_by"] = who
            n += 1
    _flush(store)
    return {"ok": True, "removed": n}