"""Roles (WO-018 step3): สิทธิ์ผู้ใช้ 3 ระดับ — เครื่องเดียวคุม "ใครทำอะไรได้".

โมเดล (fail-closed):
- ธง config roles_enabled=false (ค่าเริ่มต้น) → พฤติกรรมเดิม 100%: machine token = ทุกสิทธิ์ (admin)
- เปิดธง: machine token = admin เสมอ, token อื่นต้องมีไฟล์ data/roles/<ชื่อ>.token (มี role ใน <ชื่อ>.json)
- 3 ระดับ:
  admin    — ทุกอย่าง (shell-run/elevated/cancel/rollback/งาน/อนุมัติ)
  operator — สั่งงาน browser/desktop + อนุมัติ + หยุดงาน + rollback · ห้าม shell-run
  viewer   — อ่านอย่างเดียว (GET endpoints) · POST ใดๆ = 403
- token ผู้ใช้สร้างด้วย CLI: python -m service.roles add <ชื่อ> <role> — แสดง token ครั้งเดียว (เจ้าของเก็บเอง)
- WO-019 M1 Token lifecycle: token มี expires (วันที่ ISO — ไม่ใส่ = ไม่มีวันหมด) · revoke(name) ย้ายไป .revoked
  (ไฟล์ต้นอยู่เป็นหลักฐาน — resolve ไม่นับ) · resolve_role ตรวจ revoke ก่อนเสมอ — token ตาย = 401 ทันที
"""
import datetime
import json
import os
import pathlib
import secrets

APP = pathlib.Path(__file__).resolve().parent.parent
ROLES_DIR = APP / "data" / "roles"
VALID_ROLES = ("admin", "operator", "viewer")
ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}

# ขั้นต่ำสิทธิ์ต่อ endpoint (POST) — เส้นทางอื่นที่ไม่อยู่ในตาราง = admin เท่านั้น (fail-closed)
POST_MIN_ROLE = {
    "/api/task": "operator",
    "/api/desktop-task": "operator",
    "/api/chat-task": "operator",  # WO-019 M3: โหมดแชท — คุมซ้ำที่ตัวจำแนกเสี่ยง (is_easy_task) และ allowlist
    "/api/approve": "operator",
    "/api/reject": "operator",
    "/api/task-cancel": "operator",
    "/api/shell-rollback": "operator",
    "/api/shell-run": "admin",  # คุมสูงสุด — รันคำสั่งระบบ/ติดตั้ง
}


def enabled() -> bool:
    try:
        c = json.loads((APP / "config.json").read_text(encoding="utf-8"))
        return bool(c.get("roles_enabled", False))
    except Exception:
        return False


def _token_files():
    if not ROLES_DIR.is_dir():
        return []
    return sorted(ROLES_DIR.glob("*.token"))


def _machine_token() -> str:
    """machine token = ไฟล์เดียวกับที่ server.py ใช้ (~/.browser-engine-desktop/token) — อ่านตรง ไม่ import server (ตัด chain playwright/UIA ที่ Node สดไม่มี)."""
    p = pathlib.Path.home() / ".browser-engine-desktop" / "token"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _expired(meta: dict) -> bool:
    """WO-019 M1: token หมดอายุ — expires เป็น ISO date (ไม่ใส่ = ไม่มีวันหมด)."""
    exp = (meta or {}).get("expires") or ""
    if not exp:
        return False
    try:
        return datetime.date.fromisoformat(str(exp)[:10]) < datetime.date.today()
    except ValueError:
        return True  # รูปแบบเพี้ยน = ถือว่าหมดอายุ (fail-closed)


def _revoked(name_stem: str) -> bool:
    return (ROLES_DIR / f"{name_stem}.revoked").is_file()


def resolve_role(token: str) -> str:
    """คืน role ของ token: machine token = admin · token ที่ลงทะเบียน = ตามไฟล์ · ถูก revoke/หมดอายุ/ไม่รู้จัก = "" (ไม่ผ่าน)."""
    if not token:
        return ""
    import hmac as _hmac
    mt = _machine_token()
    if mt and _hmac.compare_digest(token, mt):
        return "admin"
    tok_b = token.encode("utf-8")
    for f in _token_files():
        try:
            stored = f.read_text(encoding="utf-8").strip().encode("utf-8")
            if _hmac.compare_digest(tok_b, stored):
                if _revoked(f.stem):
                    return ""
                meta = json.loads(f.with_suffix(".json").read_text(encoding="utf-8"))
                if _expired(meta):
                    return ""
                role = meta.get("role", "")
                return role if role in VALID_ROLES else ""
        except Exception:
            continue
    return ""


def role_at_least(role: str, min_role: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(min_role, 99)


def min_role_for(path: str) -> str:
    path = (path or "").split("?")[0]
    for prefix, mr in POST_MIN_ROLE.items():
        if path == prefix or path.startswith(prefix):
            return mr
    return "admin"


def add_user(name: str, role: str, expires: str = "") -> dict:
    """สร้าง token ผู้ใช้ใหม่ (CLI/หน้าเว็บ) — คืน token ครั้งเดียวให้เจ้าของเก็บ · expires: '2026-12-31' (ไม่ใส่ = ไม่มีวันหมด)."""
    if role not in VALID_ROLES:
        return {"error": f"role ต้องเป็นหนึ่งใน {VALID_ROLES}"}
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_").strip()
    if not safe:
        return {"error": "ชื่อผู้ใช้ว่าง/มีอักขระไม่ถูกต้อง"}
    meta = {"role": role, "created_at": datetime.date.today().isoformat()}
    if expires:
        try:
            datetime.date.fromisoformat(str(expires)[:10])
        except ValueError:
            return {"error": "expires ต้องเป็นวันที่ ISO เช่น 2026-12-31"}
        meta["expires"] = str(expires)[:10]
    ROLES_DIR.mkdir(parents=True, exist_ok=True)
    rev = ROLES_DIR / f"{safe}.revoked"
    if rev.is_file():
        rev.unlink()  # สร้างชื่อเดิมซ้ำ = หมายถึง re-issue หลังเคย revoke
    tok = secrets.token_hex(32)
    tf = ROLES_DIR / f"{safe}.token"
    tf.write_text(tok, encoding="utf-8")
    (ROLES_DIR / f"{safe}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(tf, 0o600)
    except OSError:
        pass
    return {"name": safe, "role": role, "token": tok, "expires": meta.get("expires") or "", "file": str(tf)}


def revoke_user(name: str, reason: str = "") -> dict:
    """WO-019 M1: ระงับ token ทันที — token เดิมคืน 401 ทุกเรียกต่อไป (ไฟล์ .token คงอยู่เป็นหลักฐาน — resolve ไม่นับ)."""
    safe = "".join(ch for ch in name if ch.isalnum() or ch in "-_").strip()
    tf = ROLES_DIR / f"{safe}.token"
    if not tf.is_file():
        return {"error": "ไม่พบสมาชิกชื่อนี้"}
    (ROLES_DIR / f"{safe}.revoked").write_text(
        json.dumps({"revoked_at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "reason": (reason or "")[:200]}, ensure_ascii=False), encoding="utf-8")
    return {"ok": True, "name": safe, "revoked": True,
            "note": "token เดิมใช้ไม่ได้ทันที — ไฟล์ต้นคงเก็บเป็นหลักฐาน (สร้างใหม่ได้ด้วย add ชื่อเดิม)"}


def list_users() -> list:
    out = []
    for f in _token_files():
        try:
            meta = json.loads(f.with_suffix(".json").read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        st = "active"
        if _revoked(f.stem):
            st = "revoked"
        elif _expired(meta):
            st = "expired"
        out.append({"name": f.stem, "role": meta.get("role", "?"), "state": st,
                    "expires": meta.get("expires") or "", "created_at": meta.get("created_at") or ""})
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "add":
        r = add_user(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "viewer",
                     sys.argv[4] if len(sys.argv) > 4 else "")
        print(json.dumps(r, ensure_ascii=False))
    elif len(sys.argv) >= 3 and sys.argv[1] == "revoke":
        print(json.dumps(revoke_user(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""), ensure_ascii=False))
    elif len(sys.argv) >= 2 and sys.argv[1] == "list":
        print(json.dumps(list_users(), ensure_ascii=False, indent=2))
    else:
        print("ใช้: python -m service.roles add <ชื่อ> <admin|operator|viewer> [expires 2026-12-31] | revoke <ชื่อ> [เหตุผล] | list")