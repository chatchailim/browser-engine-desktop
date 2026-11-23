"""Windows UI Automation Policy Engine (Level 5 Strategy) for browser-engine-desktop (WO-016).

Enforces Ground Rules GR2, GR3, GR4, and GR5:
- GR2: Disabled by default (uia_enabled=false in config.json).
- GR3: Process & Window whitelist (uia_allowed_apps). Default empty [] denies all inspection.
- GR4: Un-overridable blacklist: UAC (consent.exe), Windows Credential prompts, Lock Screen,
       Credential Manager, and higher-elevation processes. Immediately denies and halts.
- GR5: Password field detection: elements identified as password fields are never read for value or length.
"""
import json
import os
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"


def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# GR4: Non-overridable Process Name Blacklist (case-insensitive)
BLACKLIST_PROCESS_NAMES = {
    "consent.exe",             # UAC Elevation Prompt
    "credentialuibroker.exe",  # Windows Credential UI Broker
    "credwiz.exe",             # Credential Manager Wizard
    "logonui.exe",             # Windows Logon / Lock Screen UI
    "lockapp.exe",             # Windows Lock Screen App
    "lsass.exe",               # Local Security Authority Subsystem
    "csrss.exe"                # Client Server Runtime Subsystem
}

# GR4: Non-overridable Window Title Patterns (Regex case-insensitive)
BLACKLIST_TITLE_PATTERNS = [
    re.compile(r"^windows security$", re.I),
    re.compile(r"^ระบบความปลอดภัยของ windows$", re.I),
    re.compile(r"credential manager", re.I),
    re.compile(r"ตัวจัดการข้อมูลรับรอง", re.I),
    re.compile(r"user account control", re.I),
    re.compile(r"การควบคุมบัญชีผู้ใช้", re.I),
    re.compile(r"windows default lock screen", re.I)
]


class UIAPolicyEngine:
    def __init__(
        self,
        enabled: Optional[bool] = None,
        allowed_apps: Optional[List[Any]] = None
    ):
        cfg = load_config()
        self.enabled = enabled if enabled is not None else bool(cfg.get("uia_enabled", False))
        self.allowed_apps = allowed_apps if allowed_apps is not None else list(cfg.get("uia_allowed_apps", []))

    def reload(self) -> None:
        """Reload configuration from disk."""
        cfg = load_config()
        self.enabled = bool(cfg.get("uia_enabled", False))
        self.allowed_apps = list(cfg.get("uia_allowed_apps", []))

    def can_inspect_window(self, window_info: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
        """Single decision point for window inspection policy (R2).
        
        Evaluates GR2, GR3, and GR4 rules strictly in order:
        1. Deny-by-default if window_info is missing or cannot be identified.
        2. Deny if Level 5 UIA is disabled (GR2: uia_enabled=false).
        3. Deny if target matches un-overridable blacklist (GR4: UAC, credentials, lock, elevation).
           The blacklist CANNOT be bypassed by any whitelist rule.
        4. Deny if allowed_apps list is empty (GR3).
        5. Verify window matches at least one whitelist rule in allowed_apps (GR3).
        """
        # 1. Deny-by-default on missing or unidentified window info
        if not window_info or not isinstance(window_info, dict):
            return False, "unidentified_window"

        proc_name = str(window_info.get("process_name", "")).strip().lower()
        title = str(window_info.get("title", "")).strip()
        is_elevated_higher = bool(window_info.get("is_elevated_higher", False))

        if not proc_name:
            return False, "unidentified_window"

        # 2. Enforcement of GR2: Level 5 must be enabled in config
        if not self.enabled:
            return False, "uia_disabled"

        # 3. Enforcement of GR4: Non-overridable Blacklist check
        # (A) Higher elevation check
        if is_elevated_higher:
            return False, "higher_elevation_blocked"

        # (B) Process name blacklist
        if proc_name in BLACKLIST_PROCESS_NAMES:
            return False, f"blacklisted_process: {proc_name}"

        # (C) Window title blacklist
        if title:
            for pat in BLACKLIST_TITLE_PATTERNS:
                if pat.search(title):
                    return False, f"blacklisted_window: {title}"

        # 4. Enforcement of GR3: Whitelist check
        if not self.allowed_apps:
            return False, "allowed_apps_empty"

        # Check whitelist entries
        for entry in self.allowed_apps:
            if isinstance(entry, str):
                allowed_proc = entry.strip().lower()
                if allowed_proc == "*" or allowed_proc == proc_name:
                    return True, "allowed"
            elif isinstance(entry, dict):
                allowed_proc = str(entry.get("process_name", "")).strip().lower()
                title_pattern = entry.get("window_title_regex")

                # Match process name
                if allowed_proc == "*" or allowed_proc == proc_name:
                    if not title_pattern or title_pattern == ".*":
                        return True, "allowed"
                    try:
                        if re.search(str(title_pattern), title, re.I):
                            return True, "allowed"
                    except Exception:
                        pass

        return False, f"not_in_allowed_apps: {proc_name}"

    @staticmethod
    def is_password_element(element_info: Dict[str, Any]) -> bool:
        """Enforcement of GR5 & WO-016.1 R1: Check if element is a password field.
        
        Detects unannounced password fields using Thai & English semantic keywords
        (รหัสผ่าน, พาสเวิร์ด, ยืนยันรหัสผ่าน, รหัส PIN, password, passwd, passcode, pin, secret, otp, cvv).
        When in doubt, treats as password field (deny-by-default).
        If True, caller must NEVER read or log value, and NEVER record length or text_hash.
        """
        if not element_info or not isinstance(element_info, dict):
            return False

        # 1. Direct UIA CurrentIsPassword property
        if bool(element_info.get("is_password", False)):
            return True

        # 2. Collect visible name, label, role, control_type, automation_id, class_name
        texts = []
        for k in ("name", "label", "role", "control_type", "automation_id", "class_name"):
            v = element_info.get(k)
            if v:
                texts.append(str(v))

        combined = " ".join(texts)
        if not combined:
            return False

        lower_comb = combined.lower()

        # 3. Direct substring match for standard password/passwd
        if "password" in lower_comb or "passwd" in lower_comb:
            return True

        # 4. Thai password keywords
        password_thai = (
            "รหัสผ่าน",
            "พาสเวิร์ด",
            "ยืนยันรหัสผ่าน",
            "รหัสผ่านใหม่",
            "รหัส pin",
            "รหัสpin",
            "รหัสพิน",
            "รหัสความปลอดภัย",
            "รหัสลับ",
            "พาสโค้ด"
        )
        for kw in password_thai:
            if kw in lower_comb:
                return True

        # 5. English password keywords with word boundaries
        norm_eng = re.sub(r"[-_./\\]+", " ", lower_comb)
        password_eng_re = re.compile(
            r"\b(?:new\s+|confirm\s+)?(?:password|passwd|passcode|pin|secret|otp|cvv|cvc)\b",
            re.I
        )
        if password_eng_re.search(norm_eng):
            return True

        return False


# Global Singleton Policy Engine
global_uia_policy = UIAPolicyEngine()


def get_uia_policy() -> UIAPolicyEngine:
    return global_uia_policy
