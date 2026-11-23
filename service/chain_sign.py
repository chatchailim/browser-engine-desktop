"""Chain-sign (WO-018 step2+step3; เปลี่ยนชื่อจาก keysign ตาม T1 2026-11-23): ลายเซ็น 2 ชั้น — กุญแจอสมมาตร Ed25519 + HMAC ใบเดิม (โซ่เก่ายังตรวจได้).

รูปแบบลายเซ็นใน manifest:
- "ed25519:<hex>" — ตรวจด้วยกุญแจสาธารณะ data/keysign/user_ed25519.pub (แชร์ไฟล์ pub เพื่อตรวจที่เครื่องอื่นได้)
- "<hex 64>" — HMAC-SHA256 ใบเดิม (backward compatible)
ขอบเขต (ตรงไปตรงมา):
- ลายเซ็นครอบ "intent" ของคำสั่ง (seq/time/source/kind/detail) — ผล (exit/stdout) ไม่อยู่ในลายเซ็น
- chain: ลายเซ็นรายการถัดไปพันกับลายเซ็นก่อน — แก้/ลบ/แทรกกลางโซ่ = ตรวจเจอ
- นโยบาย chain_sign_policy (รับชื่อเดิม keysign_policy): off | audit (เซ็นไปต่อ) | enforce (ไม่มีกุญแจ/เซ็นไม่ได้ = ปฏิเสธคำสั่ง)
"""
import hashlib
import hmac as hmac_mod
import json
import os
import pathlib
import secrets
import time

APP = pathlib.Path(__file__).resolve().parent.parent
# ชื่อโฟลเดอร์คงเป็น data/keysign เพื่อให้กุญแจและโซ่เดิมใช้ต่อได้ (ไม่ย้ายไฟล์กุญแจ)
# คำว่า "KeySign" สงวนไว้เรียกอุปกรณ์ SPADA KeySign เท่านั้น — ดู service/spada_keysign.py
KS_DIR = APP / "data" / "keysign"
GENESIS = "GENESIS"


def ensure_key() -> bytes:
    """กุญแจ HMAC ใบเดิม (user.key)."""
    KS_DIR.mkdir(parents=True, exist_ok=True)
    kf = KS_DIR / "user.key"
    if kf.is_file():
        try:
            return bytes.fromhex(kf.read_text(encoding="utf-8").strip())
        except Exception:
            pass
    key = secrets.token_bytes(32)
    kf.write_text(key.hex(), encoding="utf-8")
    try:
        os.chmod(kf, 0o600)
    except OSError:
        pass
    return key


def ensure_keypair():
    """คู่กุญแจ Ed25519: user_ed25519.key (seed 32B) + .pub — สร้างครั้งเดียว ใช้ซ้ำ."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    KS_DIR.mkdir(parents=True, exist_ok=True)
    kf, pf = KS_DIR / "user_ed25519.key", KS_DIR / "user_ed25519.pub"
    if kf.is_file() and pf.is_file():
        try:
            priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(kf.read_text(encoding="utf-8").strip()))
            return priv, pf.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    priv = Ed25519PrivateKey.generate()
    seed = priv.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    kf.write_text(seed.hex(), encoding="utf-8")
    pf.write_text(pub.hex(), encoding="utf-8")
    try:
        os.chmod(kf, 0o600)
    except OSError:
        pass
    return priv, pub.hex()


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sign_entry(entry_base: dict, key: bytes, prev_sig: str) -> str:
    """HMAC ใบเดิม (hex ล้วน)."""
    payload = canonical(entry_base) + b"|" + prev_sig.encode("utf-8")
    return hmac_mod.new(key, payload, hashlib.sha256).hexdigest()


def sign_entry_asym(entry_base: dict, priv, prev_sig: str) -> str:
    """Ed25519 — คืน "ed25519:<hex>" (ตรวจข้ามเครื่องด้วยกุญแจสาธารณะ)."""
    payload = canonical(entry_base) + b"|" + prev_sig.encode("utf-8")
    return "ed25519:" + priv.sign(payload).hex()


def pubkey_hex() -> str:
    pf = KS_DIR / "user_ed25519.pub"
    return pf.read_text(encoding="utf-8").strip() if pf.is_file() else ""


def key_available() -> bool:
    return (KS_DIR / "user.key").is_file() or (KS_DIR / "user_ed25519.key").is_file()


POLICIES = ("off", "audit", "enforce")


def policy_from_cfg(cfg: dict) -> str:
    """อ่านนโยบายจาก config: chain_sign_policy ก่อน แล้วค่อยรับคีย์เดิม keysign_policy (ช่วงเปลี่ยนผ่าน)."""
    raw = cfg.get("chain_sign_policy", cfg.get("keysign_policy", "off"))
    v = str(raw).lower()
    return v if v in POLICIES else "off"


def policy() -> str:
    try:
        c = json.loads((APP / "config.json").read_text(encoding="utf-8"))
        return policy_from_cfg(c)
    except Exception:
        return "off"


def _verify_ed25519(sig_hex: str, base: dict, prev: str, pubkey_str: str) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        payload = canonical(base) + b"|" + prev.encode("utf-8")
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_str)).verify(bytes.fromhex(sig_hex), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def verify_chain(manifest_path, key: bytes = None, pubkey: str = None) -> dict:
    """เดิน manifest ทั้งไฟล์: ตรวจลายเซ็น (ed25519/HMAC ตามรูปแบบ) + chain ต่อเนื่อง."""
    path = pathlib.Path(manifest_path)
    if not path.is_file():
        return {"exists": False, "entries": 0, "valid": None}
    key = key or ensure_key()
    pubkey = pubkey or pubkey_hex()
    prev = GENESIS
    entries, bad = 0, []
    schemes = {"ed25519": 0, "hmac": 0, "unsigned": 0}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entries += 1
        try:
            e = json.loads(line)
        except Exception:
            bad.append({"line": entries, "reason": "unparseable"})
            break
        sig = e.get("signature")
        base = {k: e.get(k) for k in ("seq", "timestamp", "source", "kind", "detail")}
        if sig is None:
            schemes["unsigned"] += 1
            continue  # ช่วง policy off — chain เดินต่อ
        if isinstance(sig, str) and sig.startswith("ed25519:"):
            if not (pubkey and _verify_ed25519(sig[len("ed25519:"):], base, prev, pubkey)):
                bad.append({"seq": e.get("seq"), "reason": "ed25519_mismatch"})
                break
            schemes["ed25519"] += 1
        else:
            expected = sign_entry(base, key, prev)
            if not hmac_mod.compare_digest(expected, str(sig)):
                bad.append({"seq": e.get("seq"), "reason": "signature_mismatch"})
                break
            schemes["hmac"] += 1
        prev = sig
    return {"exists": True, "entries": entries, "valid": not bad, "first_problem": bad[0] if bad else None,
            "schemes": schemes, "pubkey_head": pubkey[:16], "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}