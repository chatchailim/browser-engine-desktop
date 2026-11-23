"""SPADA KeySign (อุปกรณ์ฮาร์ดแวร์) — ผู้ลงนามการอนุมัติ Risk >= 2 และคำสั่ง shell (T2 2026-11-23).

KeySign = SPADA Personal Key Token (RP2350 · เฟิร์มแวร์ spada-pkt-g2) พูดโปรโตคอล SPADA-PKT-v1
ต้นฉบับโปรโตคอลอยู่ที่ SPADAV5 scripts/ops/personal-key-token/pkt-proto.mjs (doc 345 §5) — ไฟล์นี้ต้องตรงกับต้นฉบับ

ขอบเขต (ตรงไปตรงมา):
- พิสูจน์ได้: มีคนอยู่ที่เครื่องและกดปุ่มบนอุปกรณ์ที่ลงทะเบียนไว้ (user presence) และลายเซ็นผูกกับ "สิ่งที่อนุมัติ" (contextHash)
- พิสูจน์ไม่ได้: อุปกรณ์เป็นของแท้จากโรงงาน (ไม่มี device attestation) — ดู doc 346 §9
- ไม่ใช่ chain-sign (service.chain_sign) ซึ่งเป็นกุญแจซอฟต์แวร์ในไฟล์

ตรวจครบสี่ข้อทุกครั้ง (verify_assertion): ลายเซ็นตรง pubkey ที่ลงทะเบียน · challenge เป็นของที่โฮสต์ออกเองและยังไม่หมดอายุ ·
counter เพิ่มจากครั้งก่อน · contextHash ตรงกับสิ่งที่กำลังอนุมัติ — ขาดข้อใด = ปฏิเสธ (fail-closed)

config:
  spada_keysign_policy: "off" (ค่าเริ่มต้น) | "enforce"
  spada_keysign_port:   "COM7" (พอร์ต CDC ของอุปกรณ์)
  spada_keysign_scope:  ["approve", "shell"] (ค่าเริ่มต้นทั้งสอง)

ลงทะเบียนอุปกรณ์กับเครื่องนี้ (ผู้ดูแล — อุปกรณ์ต้อง enroll กุญแจไว้แล้วตาม runbook 347):
  python -m service.spada_keysign register --port COM7 --owner "<ชื่อผู้ถือ>"
  python -m service.spada_keysign status
  python -m service.spada_keysign revoke --reason "<เหตุผล>"
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Protocol

APP = pathlib.Path(__file__).resolve().parent.parent
REG_DIR = APP / "data" / "spada-keysign"
TOKEN_FILE = REG_DIR / "token.json"

# ── ค่าคงที่ของโปรโตคอล SPADA-PKT-v1 — ห้ามแก้โดยไม่ขึ้นเวอร์ชัน ─────────────────────
PROTO_PREFIX = b"SPADA-PKT-v1"
CHALLENGE_LEN = 32
CONTEXT_LEN = 32
PUBKEY_LEN = 32
SIG_LEN = 64
COUNTER_LEN = 4
BOARD_UID_LEN = 8
PRESENCE_TIMEOUT_S = 15.0
CHALLENGE_TTL_S = 15.0

CMD_GET_INFO = 0x01
CMD_GET_PUBKEY = 0x03
CMD_SIGN = 0x04

ST_OK = 0x00
ST_NAME = {0x00: "OK", 0x01: "NEED_PRESENCE", 0x02: "TIMEOUT", 0x03: "NO_KEY",
           0x04: "BAD_REQUEST", 0x05: "LOCKED", 0x06: "RATE_LIMITED"}

POLICIES = ("off", "enforce")
SCOPES = ("approve", "shell")


class KeySignError(Exception):
    """ลงนามด้วย KeySign ไม่สำเร็จ — code สั้นสำหรับ API/ผลลัพธ์ · ไม่มีความลับในข้อความ."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ── เฟรม ───────────────────────────────────────────────────────────────────────

def encode_request(cmd: int, payload: bytes = b"") -> bytes:
    if not 0 <= cmd <= 0xFF:
        raise ValueError("cmd ไม่ถูกต้อง")
    if len(payload) > 0xFFFF:
        raise ValueError("payload ยาวเกิน")
    return bytes([cmd]) + len(payload).to_bytes(2, "big") + payload


def decode_response(buf: bytes):
    if len(buf) < 3:
        raise KeySignError("bad_frame", "เฟรมคำตอบสั้นเกินไป")
    length = int.from_bytes(buf[1:3], "big")
    if len(buf) != 3 + length:
        raise KeySignError("bad_frame", "ความยาวเฟรมไม่ตรง")
    return buf[0], buf[3:]


def build_signed_message(counter: int, challenge: bytes, context_hash: bytes) -> bytes:
    """message = "SPADA-PKT-v1" || 0x00 || counter(4 BE) || challenge(32) || contextHash(32) — 81 ไบต์."""
    if not 0 <= counter <= 0xFFFFFFFF:
        raise ValueError("counter ไม่ถูกต้อง")
    if len(challenge) != CHALLENGE_LEN or len(context_hash) != CONTEXT_LEN:
        raise ValueError("ความยาว challenge/contextHash ไม่ถูกต้อง")
    return PROTO_PREFIX + b"\x00" + counter.to_bytes(COUNTER_LEN, "big") + challenge + context_hash


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def context_hash_of(context: dict) -> bytes:
    """contextHash = sha256 ของ JSON แบบ canonical ของ "สิ่งที่กำลังอนุมัติ" — ต้องมี purpose เสมอ."""
    if not isinstance(context, dict) or not context.get("purpose"):
        raise ValueError("context ต้องเป็น dict ที่มี purpose")
    return hashlib.sha256(canonical(context)).digest()


def token_id_of(pubkey: bytes) -> str:
    """tokenId = 16 ตัวแรกของ sha256(pubkey) — ตรงกับ pkt-proto.mjs tokenIdFromPubkey."""
    return hashlib.sha256(pubkey).hexdigest()[:16]


# ── ทางส่งข้อมูล ──────────────────────────────────────────────────────────────

class Transport(Protocol):
    def request(self, frame: bytes) -> bytes: ...


def _ps_script(port: str, hex_in: str, timeout_ms: int) -> str:
    # ค่าที่ฝังมีสามอย่างและถูกจำกัดรูปแบบแล้ว (COM\d+ · hex · จำนวนเต็ม) — แทรกคำสั่งอื่นไม่ได้
    if not re.fullmatch(r"COM[0-9]{1,3}", port, re.IGNORECASE):
        raise KeySignError("bad_port", "ชื่อพอร์ตไม่ถูกรูปแบบ")
    if not re.fullmatch(r"(?:[0-9a-f]{2})*", hex_in):
        raise KeySignError("bad_frame", "hex ไม่ถูกรูปแบบ")
    if not 1000 <= timeout_ms <= 300_000:
        raise KeySignError("bad_timeout", "timeout ไม่อยู่ในช่วง")
    return f"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {{
  $sp = New-Object System.IO.Ports.SerialPort '{port.upper()}', 115200, 'None', 8, 'One'
  $sp.NewLine = [string][char]10
  $sp.ReadTimeout = {timeout_ms}
  $sp.WriteTimeout = {timeout_ms}
  $sp.DtrEnable = $true
  $sp.RtsEnable = $true
  $sp.Open()
  try {{
    Start-Sleep -Milliseconds 400
    $sp.DiscardInBuffer()
    $drainDeadline = (Get-Date).AddMilliseconds(600)
    while ((Get-Date) -lt $drainDeadline) {{
      if ($sp.BytesToRead -gt 0) {{ $null = $sp.ReadExisting(); $drainDeadline = (Get-Date).AddMilliseconds(400) }}
      else {{ Start-Sleep -Milliseconds 50 }}
    }}
    $sp.DiscardInBuffer()
    $sp.WriteLine('{hex_in}')
    $line = $sp.ReadLine()
    Write-Output ("OK " + $line.Trim())
  }} finally {{ $sp.Close() }}
}} catch {{
  Write-Output ("ERR " + $_.Exception.Message)
}}
"""


class PwshSerialTransport:
    """อุปกรณ์จริงผ่าน CDC (COM port) — หนึ่งบรรทัด = หนึ่งเฟรม hex · เปิด/ปิดพอร์ตต่อคำสั่ง (ตาม pkt-transport.mjs)."""

    def __init__(self, port: str, timeout_s: float = 30.0):
        if not port:
            raise KeySignError("no_port", "ยังไม่ได้ตั้ง spada_keysign_port")
        self.port = port
        self.timeout_s = timeout_s

    def request(self, frame: bytes) -> bytes:
        script = _ps_script(self.port, frame.hex(), int(self.timeout_s * 1000))
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True, text=True, timeout=self.timeout_s + 10,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as e:
            raise KeySignError("transport_failed", f"เรียก PowerShell ไม่สำเร็จ: {type(e).__name__}")
        line = (out.splitlines() or [""])[-1]
        if line.startswith("ERR "):
            raise KeySignError("device_unreachable", "เปิดพอร์ตหรืออ่านจากอุปกรณ์ไม่ได้")
        if not line.startswith("OK "):
            raise KeySignError("device_unreadable", "คำตอบจากอุปกรณ์อ่านไม่ออก")
        hex_out = line[3:].strip()
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", hex_out):
            raise KeySignError("device_unreadable", "อุปกรณ์ตอบไม่เป็น hex")
        return bytes.fromhex(hex_out)


class SimDevice:
    """ตัวจำลองในโปรเซส — ใช้ในชุดทดสอบเท่านั้น (ของจริงสร้างกุญแจในชิป ไม่ออกจากชิป).

    presence: callable คืน True = มีคนกดปุ่มทันเวลา · False = ไม่กด (TIMEOUT)
    """

    def __init__(self, presence: Callable[[], bool] = lambda: True, counter: int = 0):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        self._priv = Ed25519PrivateKey.generate()
        self.pubkey = self._priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.counter = counter
        self.presence = presence
        self.board_uid = secrets.token_bytes(BOARD_UID_LEN)

    def request(self, frame: bytes) -> bytes:
        cmd, length = frame[0], int.from_bytes(frame[1:3], "big")
        payload = frame[3:3 + length]
        if cmd == CMD_GET_PUBKEY:
            return encode_request(ST_OK, self.pubkey)
        if cmd == CMD_GET_INFO:
            info = bytes([1, 0, 2, 1]) + self.board_uid + b"\x01" + self.counter.to_bytes(4, "big")
            return encode_request(ST_OK, info)
        if cmd == CMD_SIGN:
            if len(payload) != CHALLENGE_LEN + CONTEXT_LEN:
                return encode_request(0x04)
            if not self.presence():
                return encode_request(0x02)
            self.counter += 1  # เขียน counter ก่อนเซ็นเสมอ (เหมือนเฟิร์มแวร์)
            msg = build_signed_message(self.counter, payload[:CHALLENGE_LEN], payload[CHALLENGE_LEN:])
            return encode_request(ST_OK, self.counter.to_bytes(4, "big") + self._priv.sign(msg))
        return encode_request(0x04)


# ── ทะเบียนบนเครื่อง (อุปกรณ์ที่ผูกกับเครื่องนี้ + counter ล่าสุด) ───────────────────

_reg_lock = threading.Lock()


def load_token(path: pathlib.Path = None) -> Optional[dict]:
    p = path or TOKEN_FILE
    if not p.is_file():
        return None
    try:
        t = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(t, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(t.get("pubkey", ""))):
        return None
    return t


def save_token(token: dict, path: pathlib.Path = None) -> None:
    p = path or TOKEN_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# ── ตรวจครบสี่ข้อ ──────────────────────────────────────────────────────────────

def verify_assertion(*, pubkey: bytes, counter: int, challenge: bytes, context_hash: bytes, sig: bytes,
                     expected_challenge: bytes, challenge_issued_at: float, last_counter: int,
                     expected_context_hash: bytes, now: float = None) -> Dict[str, object]:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    import hmac
    now = time.time() if now is None else now
    checks = {"signature": False, "challenge": False, "counter": False, "context": False}
    reasons = []
    try:
        Ed25519PublicKey.from_public_bytes(pubkey).verify(sig, build_signed_message(counter, challenge, context_hash))
        checks["signature"] = True
    except (InvalidSignature, ValueError):
        reasons.append("ลายเซ็นไม่ตรงกับ pubkey ที่ลงทะเบียน")
    if not hmac.compare_digest(challenge, expected_challenge):
        reasons.append("challenge ไม่ใช่ค่าที่โฮสต์ออกให้")
    # อายุสูงสุด = เวลารอคนกดปุ่ม (15 วิ) + ค่าเปิดพอร์ต/ล้างบัฟเฟอร์ของ transport — ต่ออายุซ้ำไม่ได้ เพราะ challenge ใช้ครั้งเดียว
    elif now - challenge_issued_at > CHALLENGE_TTL_S + PRESENCE_TIMEOUT_S:
        reasons.append("challenge หมดอายุ")
    else:
        checks["challenge"] = True
    if counter > last_counter:
        checks["counter"] = True
    else:
        reasons.append("counter ไม่เพิ่ม — อาจเป็นการ replay")
    if hmac.compare_digest(context_hash, expected_context_hash):
        checks["context"] = True
    else:
        reasons.append("contextHash ไม่ตรงกับสิ่งที่กำลังอนุมัติ")
    return {"ok": all(checks.values()), "checks": checks, "reasons": reasons}


# ── ผู้ลงนาม ──────────────────────────────────────────────────────────────────

@dataclass
class Assertion:
    token_id: str
    counter: int
    challenge: str
    context_hash: str
    sig: str
    signed_at: float = field(default_factory=time.time)

    def as_record(self) -> dict:
        return {"scheme": "spada-pkt-v1", "tokenId": self.token_id, "counter": self.counter,
                "challenge": self.challenge, "contextHash": self.context_hash, "sig": self.sig}


class KeySignSigner:
    """ขอลายเซ็นจากอุปกรณ์ 1 ครั้ง ต่อ 1 สิ่งที่อนุมัติ แล้วตรวจครบสี่ข้อก่อนคืนผล — ผิดข้อใดโยน KeySignError."""

    def __init__(self, transport: Transport, token_path: pathlib.Path = None):
        self.transport = transport
        self.token_path = token_path or TOKEN_FILE

    def sign(self, context: dict) -> Assertion:
        with _reg_lock:  # หนึ่งคำขอเซ็นต่อครั้ง — counter ต้องเดินตามลำดับ
            token = load_token(self.token_path)
            if token is None:
                raise KeySignError("not_registered", "ยังไม่ได้ลงทะเบียนอุปกรณ์ KeySign กับเครื่องนี้")
            if token.get("revoked"):
                raise KeySignError("revoked", "อุปกรณ์ KeySign ถูกเพิกถอนแล้ว")
            pubkey = bytes.fromhex(token["pubkey"])
            expected_ctx = context_hash_of(context)
            challenge = secrets.token_bytes(CHALLENGE_LEN)
            issued = time.time()
            status, payload = decode_response(self.transport.request(encode_request(CMD_SIGN, challenge + expected_ctx)))
            if status != ST_OK:
                raise KeySignError("device_" + ST_NAME.get(status, "error").lower(),
                                   f"อุปกรณ์ตอบ {ST_NAME.get(status, status)}")
            if len(payload) != COUNTER_LEN + SIG_LEN:
                raise KeySignError("bad_frame", "payload ของ SIGN ยาวไม่ถูกต้อง")
            counter = int.from_bytes(payload[:COUNTER_LEN], "big")
            sig = payload[COUNTER_LEN:]
            v = verify_assertion(pubkey=pubkey, counter=counter, challenge=challenge, context_hash=expected_ctx,
                                 sig=sig, expected_challenge=challenge, challenge_issued_at=issued,
                                 last_counter=int(token.get("lastCounter", 0)), expected_context_hash=expected_ctx)
            if not v["ok"]:
                raise KeySignError("verify_failed", "; ".join(v["reasons"]))
            token["lastCounter"] = counter
            token["lastSignedAt"] = int(time.time())
            save_token(token, self.token_path)
            return Assertion(token_id=token_id_of(pubkey), counter=counter, challenge=challenge.hex(),
                             context_hash=expected_ctx.hex(), sig=sig.hex())


# ── นโยบายจาก config ───────────────────────────────────────────────────────────

def policy_from_cfg(cfg: dict) -> str:
    v = str(cfg.get("spada_keysign_policy", "off")).lower()
    return v if v in POLICIES else "off"


def scope_from_cfg(cfg: dict) -> tuple:
    raw = cfg.get("spada_keysign_scope", list(SCOPES))
    if not isinstance(raw, list):
        return SCOPES
    return tuple(s for s in raw if s in SCOPES)


def required_for(cfg: dict, scope: str) -> bool:
    return policy_from_cfg(cfg) == "enforce" and scope in scope_from_cfg(cfg)


# ตัวสร้าง signer — ชุดทดสอบแทนด้วยตัวจำลองได้ (set_signer_factory)
_signer_factory: Optional[Callable[[dict], KeySignSigner]] = None


def set_signer_factory(factory: Optional[Callable[[dict], KeySignSigner]]) -> None:
    global _signer_factory
    _signer_factory = factory


def signer_from_cfg(cfg: dict) -> KeySignSigner:
    if _signer_factory is not None:
        return _signer_factory(cfg)
    return KeySignSigner(PwshSerialTransport(str(cfg.get("spada_keysign_port", ""))))


def sign_if_required(cfg: dict, scope: str, context: dict) -> Optional[dict]:
    """คืน None เมื่อไม่ต้องเซ็น · คืน record ของ assertion เมื่อเซ็นและตรวจผ่าน · โยน KeySignError เมื่อต้องเซ็นแต่ไม่ผ่าน."""
    if not required_for(cfg, scope):
        return None
    return signer_from_cfg(cfg).sign(context).as_record()


# ── CLI ผู้ดูแล ────────────────────────────────────────────────────────────────

def _cli(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m service.spada_keysign")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register", help="ผูกอุปกรณ์ที่ enroll แล้วเข้ากับเครื่องนี้ (อ่าน pubkey)")
    r.add_argument("--port", required=True)
    r.add_argument("--owner", required=True)
    sub.add_parser("status", help="ดูอุปกรณ์ที่ผูกไว้")
    v = sub.add_parser("revoke", help="เพิกถอนอุปกรณ์ที่ผูกไว้ทันที")
    v.add_argument("--reason", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "register":
        t = PwshSerialTransport(a.port)
        status, pub = decode_response(t.request(encode_request(CMD_GET_PUBKEY)))
        if status != ST_OK or len(pub) != PUBKEY_LEN:
            print(f"อ่าน pubkey ไม่ได้ (สถานะ {ST_NAME.get(status, status)}) — อุปกรณ์ enroll แล้วหรือยัง (runbook 347)")
            return 2
        old = load_token()
        if old and old.get("pubkey") == pub.hex() and not old.get("revoked"):
            print(f"ผูกไว้แล้ว tokenId={token_id_of(pub)}")
            return 0
        save_token({"tokenId": token_id_of(pub), "pubkey": pub.hex(), "owner": a.owner, "lastCounter": 0,
                    "registeredAt": int(time.time()), "revoked": False})
        print(f"ผูกอุปกรณ์แล้ว tokenId={token_id_of(pub)} · ต่อไปตั้ง spada_keysign_policy=enforce")
        return 0
    if a.cmd == "status":
        t = load_token()
        print(json.dumps({k: t.get(k) for k in ("tokenId", "owner", "lastCounter", "revoked", "revokedReason")}
                         if t else {"registered": False}, ensure_ascii=False))
        return 0
    if a.cmd == "revoke":
        t = load_token()
        if not t:
            print("ไม่มีอุปกรณ์ที่ผูกไว้")
            return 2
        t["revoked"], t["revokedReason"], t["revokedAt"] = True, a.reason, int(time.time())
        save_token(t)
        print(f"เพิกถอนแล้ว tokenId={t.get('tokenId')}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
