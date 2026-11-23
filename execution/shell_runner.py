"""Shell/Command Runner (WO-018 R1 + step2 sandbox/chain-sign) — รันคำสั่ง OS จริงจาก engine.

กฎ (ตามใบงาน/ธรรมนูญ + ความจริงของ Windows):
- CONFIG-GATED: ธง shell_execution_enabled=false (ค่าเริ่มต้น) → ปฏิเสธทุกคำสั่ง
- sandbox_mode (ธงใหม่): on = รอบคำสั่งมีสแนปช็อต+stash+diff+กู้คืนได้ทาง /api/shell-rollback
- chain_sign_policy (เดิม keysign_policy): off/audit/enforce — เซ็น intent ทุกคำสั่ง (chain) — enforce: ไม่มีกุญแจ=ปฏิเสธ
- elevated: Start-Process -Verb RunAs — UAC ปรากฏให้ผู้ใช้กดเอง
"""
import json
import os
import pathlib
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

APP = pathlib.Path(__file__).resolve().parent.parent
CONFIG_FILE = APP / "config.json"
MANIFEST_DIR = APP / "data" / "action-manifest"
SECRET_HINTS = ("token", "password", "passwd", "secret", "apikey", "api_key")


@dataclass
class ShellResult:
    ok: bool
    exit_code: Optional[int]
    stdout: str
    stderr: str
    duration_ms: int
    command: str
    cwd: str
    denided_reason: Optional[str] = None
    manifest_entry: Optional[Dict[str, Any]] = None


def _cfg():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_ALLOWED_SHELL_PREFIXES_OFF = None  # (reserved: future restricted-mode allowlist)


def _redact(text: str) -> str:
    if not text:
        return text
    low = text.lower()
    for hint in SECRET_HINTS:
        idx = low.find(hint)
        while idx != -1:
            eq = text.find(":", idx)
            eq2 = text.find("=", idx)
            cut = min(c for c in (eq, eq2) if c != -1) if (eq != -1 or eq2 != -1) else -1
            if cut != -1 and cut - idx < 30:
                end = text.find("\n", cut)
                text = text[:cut + 1] + " *** " + (text[end:] if end != -1 else "")
                low, idx = text.lower(), -1
                break
            idx = low.find(hint, idx + 1)
    return text


class Manifest:
    """R4: บันทึกทุก action ลง app-YYYYMMDD.jsonl พร้อม signature ว่างไว้รับ chain-sign ภายหลัง."""

    def __init__(self, engine_version="0.4-wo018"):
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        self.path = MANIFEST_DIR / ("app-" + time.strftime("%Y%m%d") + ".jsonl")
        self.seq = 0
        self.engine_version = engine_version

    def record(self, source: str, kind: str, detail: Dict[str, Any], result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.seq += 1
        entry = {
            "seq": int(time.time() * 1000) * 1000 + self.seq,  # คงที่ต่อเดือน+รัน
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": source,  # ai | human | server
            "kind": kind,
            "detail": detail,
            "result": result or {},
            "machine": os.environ.get("COMPUTERNAME", "unknown"),
            "engine_version": self.engine_version,
            "signature": None,  # chain-sign-ready: เซ็นทับฟิลด์นี้ตามนโยบาย
        }
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        return entry


class ShellRunner:
    """เรียกจาก engine/AI — ธงอนุมัติที่ผู้ใช้แก้ใน config เท่านั้น."""

    def __init__(self, manifest: Optional[Manifest] = None):
        cfg = _cfg()
        self.enabled = bool(cfg.get("shell_execution_enabled", False))
        self.elevated_allowed = bool(cfg.get("shell_elevated_allowed", False))
        self.timeout_s = int(cfg.get("shell_timeout_seconds", 600))
        # step2: sandbox เฉพาะ cwd ที่อยู่ใต้ shell_sandbox_dir เท่านั้น + chain-sign policy
        self.sandbox_mode = bool(cfg.get("shell_sandbox_mode", False))
        self.sandbox_dir = pathlib.Path(cfg.get("shell_sandbox_dir", str(APP / "data" / "shell-sandbox")))
        from service import chain_sign as _cs
        self.chain_sign_policy = _cs.policy_from_cfg(cfg)  # T1: chain_sign_policy ก่อน แล้วรับ keysign_policy เดิม
        self.keysign_policy = self.chain_sign_policy  # ชื่อเดิม — คงไว้ให้โค้ดเก่าอ่านได้
        self._cfg = cfg  # T2: นโยบาย SPADA KeySign (ฮาร์ดแวร์) อ่านจาก config ชุดเดียวกัน
        self.manifest = manifest or Manifest()
        self._sandbox = None
        self._snapshot_before = None
        self._stash = None
        self._last_diff = None
        self._last_before = None

    def _enter_sandbox(self):
        if not self.sandbox_mode:
            return None
        from service.shell_sandbox import ShellSandbox
        self._sandbox = ShellSandbox(self.sandbox_dir)
        self._snapshot_before = self._sandbox.snapshot()
        self._stash, _mb = self._sandbox.stash(self._snapshot_before)
        return self._sandbox

    def _exit_sandbox(self, entry):
        if not self._sandbox:
            return None
        after = self._sandbox.snapshot()
        diff = self._sandbox.diff(self._snapshot_before, after)
        entry["result"]["sandbox"] = {"changed": sum(len(v) for v in diff.values()),
                                      "created": len(diff["created"]), "modified": len(diff["modified"]),
                                      "deleted": len(diff["deleted"]), "stash": str(self._stash) if self._stash else None}
        self._last_diff, self._last_before = diff, self._snapshot_before
        # persist สถานะ rollback (request ถัดไป /api/shell-rollback อ่านจากไฟล์ — ไม่พึ่งหน่วยความจำของโปรเซส)
        try:
            state = {"stash": str(self._stash) if self._stash else None,
                     "before": self._snapshot_before, "diff": diff,
                     "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            (APP / "data" / "shell-sandbox-state.json").write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return entry["result"]["sandbox"]

    def _last_signature_from_file(self) -> str:
        """prev signature จริง = บรรทัดล่าสุดของไฟล์ manifest (กัน chain หักข้ามโปรเซส/รันใหม่)."""
        try:
            lines = self.manifest.path.read_text(encoding="utf-8").strip().splitlines()
            if lines:
                return json.loads(lines[-1]).get("signature") or "GENESIS"
        except Exception:
            pass
        return "GENESIS"

    def run(self, command: str, cwd: Optional[str] = None, elevated: bool = False, shell: Optional[str] = None) -> ShellResult:
        command = (command or "").strip()
        detail = {"command": command[:300], "cwd": cwd or ".", "elevated": bool(elevated)}
        if not command:
            return ShellResult(False, None, "", "", 0, command, cwd or ".", "empty_command")
        if not self.enabled:
            return ShellResult(False, None, "", "", 0, command, cwd or ".", "shell_execution_disabled")
        if elevated and not self.elevated_allowed:
            return ShellResult(False, None, "", "", 0, command, cwd or ".", "shell_elevated_not_allowed")
        if self.chain_sign_policy == "enforce":
            from service import chain_sign
            if not chain_sign.key_available():
                return ShellResult(False, None, "", "", 0, command, cwd or ".", "chain_sign_enforce_no_key")
        # T2: SPADA KeySign ฮาร์ดแวร์ — คนต้องกดปุ่มบนอุปกรณ์ก่อนคำสั่งนี้วิ่ง (ลายเซ็นผูกกับคำสั่ง+cwd+elevated เต็มข้อความ)
        from service import spada_keysign as _sk
        try:
            hw = _sk.sign_if_required(self._cfg, "shell", {"purpose": "bed.shell-run/v1", "command": command,
                                                            "cwd": cwd or "", "elevated": bool(elevated)})
        except _sk.KeySignError as e:
            return ShellResult(False, None, "", "", 0, command, cwd or ".", f"spada_keysign_denied:{e.code}")
        if hw is not None:
            detail["spada_keysign"] = hw  # อยู่ใน base ที่ chain-sign เซ็นทับ — แก้ย้อนหลังจับได้

        # step2: แซนด์บ็อกซ์ครอบจริงเมื่อ cwd อยู่ใต้ shell_sandbox_dir (ไม่อยู่ = รันปกติ ไม่ snapshot + กำกับใน manifest)
        sandboxed = False
        if self.sandbox_mode and cwd:
            try:
                sandboxed = pathlib.Path(cwd).resolve() in self.sandbox_dir.resolve().parents or \
                    pathlib.Path(cwd).resolve() == self.sandbox_dir.resolve()
            except OSError:
                sandboxed = False
        if self.sandbox_mode and not sandboxed:
            detail["sandbox_skipped_cwd_outside"] = True

        if sandboxed:
            self._enter_sandbox()
        t0 = time.monotonic()
        entry = None
        if elevated:
            # Windows จะโชว์ UAC prompt ให้ผู้ใช้กด (AI กดแทนไม่ได้ — secure desktop)
            ps = (f"Start-Process -Verb RunAs -Wait cmd.exe /c \""
                  f"{command.replace(chr(34), chr(39))} > '%TEMP%\\hermes_shell_out.txt' 2>&1 & "
                  f"(exit $LASTEXITCODE)\"")
            proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=self.timeout_s)
            exit_code = proc.returncode
            out_path = os.path.join(os.environ.get("TEMP", ""), "hermes_shell_out.txt")
            try:
                stdout = pathlib.Path(out_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                stdout = ""
            stderr = proc.stderr or ""
        else:
            # ธงเปิด = คำสั่งใดก็ได้ (ตามใบอนุมัติผู้ใช้) — shell กำหนดเองได้ทางพารามิเตอร์
            use_shell = shell is None or shell != "list"
            argv = command if not use_shell else None
            proc = subprocess.run(argv if argv is not None else command,
                                  shell=(argv is None), cwd=cwd,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=self.timeout_s)
            exit_code, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
        duration_ms = int((time.monotonic() - t0) * 1000)
        res = {"exit_code": exit_code, "duration_ms": duration_ms}
        # step2: sandbox diff หลังรัน (บันทึกในผล Manifest + เก็บบริบท rollback ให้ /api/shell-rollback)
        # step2: อ่าน prev signature "ก่อน record" — บรรทัดตัวเองยังไม่ถูกเขียน (prev = ลายเซ็นจริงล่าสุดของไฟล์ ไม่ใช่บรรทัดตัวเองที่ยังว่าง)
        # (บั๊กเดิมจาก TestSpec คำสั่ง verify_chain จับ: เซ็นหลัง record → prev=GENESIS ผิด → โซ่หัก ed25519_mismatch)
        prev_sig = self._last_signature_from_file() if self.chain_sign_policy in ("audit", "enforce") else None
        entry = self.manifest.record("ai", "shell_run", detail, res)
        if sandboxed:
            try:
                self._exit_sandbox(entry)
            except Exception:
                pass
        # step2: chain-sign — ตามนโยบาย: off=ไม่เซ็น / audit|enforce=เซ็น intent (chain กับรายการก่อน)
        sig = None
        if self.chain_sign_policy in ("audit", "enforce"):
            try:
                from service import chain_sign
                prev = prev_sig or "GENESIS"  # จากการอ่านก่อน record (ตัวเอียงใช้ตัวเองไม่พัน) — chain ต่อเนื่องจริง
                base = {k: entry.get(k) for k in ("seq", "timestamp", "source", "kind", "detail")}
                try:  # ชั้นใหม่: Ed25519 (กุญแจอสมมาตร — ตรวจข้ามเครื่องด้วย .pub)
                    priv, _pub = chain_sign.ensure_keypair()
                    sig = chain_sign.sign_entry_asym(base, priv, prev)
                except Exception:
                    sig = None
                if sig is None:  # ชั้นสำรอง: HMAC ใบเดิม
                    sig = chain_sign.sign_entry(base, chain_sign.ensure_key(), prev)
                self.manifest.last_signature = sig
            except Exception:
                sig = None
                if self.chain_sign_policy == "enforce":
                    return ShellResult(False, exit_code, _redact(stdout)[-4000:], _redact(stderr)[-2000:],
                                       duration_ms, command, cwd or str(pathlib.Path.cwd()), "chain_sign_failed")
        entry["signature"] = sig
        try:  # เขียนทับบรรทัดสุดท้ายของ manifest ด้วย entry ฉบับเต็ม (result.sandbox + signature ครบหน้าเดียว)
            lines = self.manifest.path.read_text(encoding="utf-8").splitlines()
            if lines:
                lines[-1] = json.dumps(entry, ensure_ascii=False)
                self.manifest.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError:
            pass
        return ShellResult(
            ok=(exit_code in (0, 3010)),  # winget ที่ติดตั้งเสร็จ ถอด exit 3010
            exit_code=exit_code,
            stdout=_redact(stdout)[-4000:],
            stderr=_redact(stderr)[-2000:],
            duration_ms=duration_ms,
            command=command,
            cwd=cwd or (str(pathlib.Path.cwd())),
            manifest_entry={"seq": entry["seq"], "signature": sig,
                            "sandbox": (entry.get("result") or {}).get("sandbox"),
                            "rollback_available": self.sandbox_mode and self._stash is not None},
        )


class ElevatedResultFile:
    """ผล elevated อ่านจากไฟล์ (อธิบายใน notes_technical_limits)."""

    @staticmethod
    def read(temp_name="hermes_shell_out.txt"):
        try:
            return pathlib.Path(os.environ.get("TEMP", ""), temp_name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


__all__ = ["ShellRunner", "ShellResult", "Manifest", "_redact"]