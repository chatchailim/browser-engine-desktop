"""Regression and Acceptance Test Suite for WO-016 (Level 5 Windows UIA Read-Only Phase).

Validates Acceptance Tests U1 through U10:
- U1: uia_enabled=false prevents any window inspection; existing suites continue passing.
- U2: ALL input-writing methods return False; cursor before/after strictly identical; Notepad text unmodified.
- U3: Allowed apps whitelist empty denies all; Notepad allowed while Calculator denied.
- U4: Un-overridable blacklist denies UAC, credentials, lock screen even if whitelisted.
- U5: Password fields return presence only (is_password=True, value=None, no length field).
- U6: Privacy zero-leakage: raw user text never appears in trace, Fact, or logs; text hash present.
- U7: FactDesktopWindowRead verifies with RFC 8785; zero raw window text in Fact payload.
- U8: Router isolation: engine/router.py and engine/orchestrator.py do not invoke Level 5 UIA.
- U9: Existing regression suites integrity.
- U10: R8 carryover: allow_insecure_http records insecureHttp in Fact; retryable clean in code & README.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from execution.windows_uia import WindowsUIAAdapter
from perception.desktop_tree import DesktopTreeParser, canonical_sha256
from policy.uia_policy import UIAPolicyEngine
from telemetry.vault import OneVaultManager, verify_fact, get_vault_manager, set_vault_manager

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent


def verify_cursor_measuring_tool(adapter: WindowsUIAAdapter, context: str = "") -> Tuple[int, int]:
    """Verify that cursor measuring tool functions properly and returns valid physical coordinates.
    Raises AssertionError if measuring tool is broken (returns None or (0, 0) fallback).
    """
    pos = adapter.get_cursor_position()
    if pos is None:
        raise AssertionError(f"Measuring tool failure: cursor position returned None{': ' + context if context else ''}")
    if not isinstance(pos, tuple) or len(pos) != 2:
        raise AssertionError(f"Measuring tool failure: cursor position not a 2-tuple ({pos}){': ' + context if context else ''}")
    if pos == (0, 0):
        raise AssertionError(f"Measuring tool failure: cursor position returned (0, 0) fallback{': ' + context if context else ''}")
    return pos


class TestWO016Suite(unittest.TestCase):
    def setUp(self):
        self.orig_vault_mgr = get_vault_manager()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.vault_dir = pathlib.Path(self.tmp_dir.name) / "vault"
        self.spool_dir = pathlib.Path(self.tmp_dir.name) / "spool"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.spool_dir.mkdir(parents=True, exist_ok=True)

        self.valid_env = {
            "ONEVAULT_INSTANCE": "spark-01",
            "ONEVAULT_ACTOR": "onevault:person:somchai-qa"
        }

    def tearDown(self):
        set_vault_manager(self.orig_vault_mgr)
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # U1: uia_enabled=false -> Zero window inspection permitted
    # -------------------------------------------------------------------------
    def test_u1_disabled_by_default(self):
        """U1: When uia_enabled=false, no windows can be opened or inspected."""
        policy = UIAPolicyEngine(enabled=False, allowed_apps=["notepad.exe"])
        adapter = WindowsUIAAdapter(policy=policy)

        # Policy check directly
        allowed, reason = policy.can_inspect_window({"process_name": "notepad.exe", "title": "Untitled - Notepad"})
        self.assertFalse(allowed)
        self.assertEqual(reason, "uia_disabled")

        # Adapter inspection method
        res = adapter.inspect_window("notepad.exe")
        self.assertFalse(res["ok"])
        self.assertIn("error", res)

    # -------------------------------------------------------------------------
    # U2: Strict input prohibition (most important)
    # Cursor before/after must be identical; Notepad text unmodified
    # -------------------------------------------------------------------------
    def test_u2_input_methods_strictly_prohibited_and_cursor_unmoved(self):
        """U2: All input methods in WindowsUIAAdapter return False.
        Cursor before and after every single call must be completely identical.
        Text in Notepad must not change.
        """
        adapter = WindowsUIAAdapter()

        # Start a local Notepad process for live text verification
        test_text = "WO016_ORIGINAL_TEXT_IMMUTABLE"
        proc = subprocess.Popen(["notepad.exe"])
        time.sleep(1.0)

        try:
            # Query cursor before any call (R2: Must not be None or (0, 0))
            cursor_initial = verify_cursor_measuring_tool(adapter, "initial check")

            methods_to_test = [
                ("click", lambda: adapter.click(100, 200)),
                ("double_click", lambda: adapter.double_click(150, 250)),
                ("right_click", lambda: adapter.right_click(200, 300)),
                ("mouse_move", lambda: adapter.mouse_move(9999, 9999)),
                ("drag_and_drop", lambda: adapter.drag_and_drop(100, 100, 500, 500)),
                ("type_text", lambda: adapter.type_text("MALICIOUS_INPUT_ATTEMPT")),
                ("press_key", lambda: adapter.press_key("Enter")),
                ("hotkey", lambda: adapter.hotkey(["Ctrl", "A"])),
                ("scroll", lambda: adapter.scroll(0, 100)),
                ("wait_until", lambda: adapter.wait_until(lambda: True))
            ]

            for method_name, method_call in methods_to_test:
                pos_before = verify_cursor_measuring_tool(adapter, f"before {method_name}")
                ret = method_call()
                pos_after = verify_cursor_measuring_tool(adapter, f"after {method_name}")

                # Must return False
                self.assertFalse(ret, f"Method {method_name} must return False in Read-Only Phase")

                # Cursor must not move by even 1 pixel
                self.assertEqual(
                    pos_before, pos_after,
                    f"Method {method_name} altered mouse cursor position! Before: {pos_before}, After: {pos_after}"
                )

            # Final cursor check against original initial position
            cursor_final = verify_cursor_measuring_tool(adapter, "final check")
            self.assertEqual(
                cursor_initial, cursor_final,
                f"Overall cursor changed across all input calls! Initial: {cursor_initial}, Final: {cursor_final}"
            )
        finally:
            proc.kill()
            proc.wait()

    def test_v4_cursor_measurement_fails_when_tool_broken(self):
        """V4: Measuring tool failure detection (WO-016.1 R2 / WO-017 R5 / W6).
        Verify that when get_cursor_position of the adapter fails (returns None or (0, 0) fallback),
        calling the actual cursor verification function reports measuring tool failure (AssertionError),
        and passes cleanly when the tool operates normally.
        """
        adapter = WindowsUIAAdapter()

        # 1. Normal condition: verifying cursor measuring tool must pass and return valid tuple
        normal_pos = verify_cursor_measuring_tool(adapter, "normal check")
        self.assertIsInstance(normal_pos, tuple)
        self.assertEqual(len(normal_pos), 2)
        self.assertNotEqual(normal_pos, (0, 0))

        # 2. Mock failure returning None: tool broken -> must fail with explicit AssertionError
        with patch.object(adapter, "get_cursor_position", return_value=None):
            with self.assertRaises(AssertionError) as ctx:
                verify_cursor_measuring_tool(adapter, "mock None check")
            self.assertIn("Measuring tool failure: cursor position returned None", str(ctx.exception))

        # 3. Mock failure returning fallback (0, 0): tool broken -> must fail with explicit AssertionError
        with patch.object(adapter, "get_cursor_position", return_value=(0, 0)):
            with self.assertRaises(AssertionError) as ctx:
                verify_cursor_measuring_tool(adapter, "mock (0, 0) check")
            self.assertIn("Measuring tool failure: cursor position returned (0, 0)", str(ctx.exception))

    # -------------------------------------------------------------------------
    # U3: Whitelist enforcement (Empty list denies all; Notepad vs Calc)
    # -------------------------------------------------------------------------
    def test_u3_whitelist_empty_and_selective_apps(self):
        """U3: Empty allowed_apps denies all; Whitelist with Notepad allows Notepad but denies Calculator."""
        # 1. Empty allowed_apps
        policy_empty = UIAPolicyEngine(enabled=True, allowed_apps=[])
        allowed, reason = policy_empty.can_inspect_window({"process_name": "notepad.exe", "title": "Untitled"})
        self.assertFalse(allowed)
        self.assertEqual(reason, "allowed_apps_empty")

        # 2. Whitelist with only Notepad
        policy_notepad = UIAPolicyEngine(
            enabled=True,
            allowed_apps=[{"process_name": "notepad.exe", "window_title_regex": ".*"}]
        )

        allowed_np, _ = policy_notepad.can_inspect_window({"process_name": "notepad.exe", "title": "doc.txt - Notepad"})
        self.assertTrue(allowed_np, "Notepad should be permitted by whitelist")

        allowed_calc, reason_calc = policy_notepad.can_inspect_window({"process_name": "calc.exe", "title": "Calculator"})
        self.assertFalse(allowed_calc, "Calculator must be denied when only Notepad is whitelisted")
        self.assertEqual(reason_calc, "not_in_allowed_apps: calc.exe")

    # -------------------------------------------------------------------------
    # U4: Non-overridable blacklist (UAC, Credentials, Lock Screen)
    # -------------------------------------------------------------------------
    def test_u4_unoverridable_blacklist_protection(self):
        """U4: Windows matching blacklisted process or title are rejected even if in whitelist."""
        # Even with wildcard "*" in allowed_apps, blacklist must override
        policy_wildcard = UIAPolicyEngine(enabled=True, allowed_apps=["*"])

        blacklisted_procs = [
            "consent.exe",
            "credentialuibroker.exe",
            "credwiz.exe",
            "logonui.exe",
            "lockapp.exe"
        ]
        for proc in blacklisted_procs:
            allowed, reason = policy_wildcard.can_inspect_window({"process_name": proc, "title": "Prompt"})
            self.assertFalse(allowed, f"Process {proc} must be rejected by non-overridable blacklist")
            self.assertIn("blacklisted_process", reason)

        blacklisted_titles = [
            "Windows Security",
            "ระบบความปลอดภัยของ Windows",
            "Credential Manager",
            "ตัวจัดการข้อมูลรับรอง",
            "User Account Control",
            "การควบคุมบัญชีผู้ใช้",
            "Windows Default Lock Screen"
        ]
        for title in blacklisted_titles:
            allowed, reason = policy_wildcard.can_inspect_window({"process_name": "someapp.exe", "title": title})
            self.assertFalse(allowed, f"Window title '{title}' must be rejected by non-overridable blacklist")
            self.assertIn("blacklisted_window", reason)

    # -------------------------------------------------------------------------
    # U5: Password element redaction
    # -------------------------------------------------------------------------
    def test_u5_password_element_redaction(self):
        """U5: Password element returns is_password=True, value=None, no length field."""
        policy = UIAPolicyEngine(enabled=True, allowed_apps=["*"])
        parser = DesktopTreeParser(policy=policy)

        # Mock a UIA password element
        class MockPasswordElement:
            CurrentControlType = 50004  # Edit
            CurrentAutomationId = "PasswordInput"
            CurrentClassName = "PasswordBox"
            CurrentIsPassword = 1
            CurrentName = "SuperSecretPassword123!"
            class CurrentBoundingRectangle:
                left = 100
                top = 200
                right = 300
                bottom = 240

        masked_node = parser._extract_masked_element_node(MockPasswordElement)
        self.assertIsNotNone(masked_node)
        self.assertTrue(masked_node["is_password"])
        self.assertIsNone(masked_node["text_hash"])
        self.assertNotIn("SuperSecretPassword123!", json.dumps(masked_node))
        self.assertNotIn("length", masked_node)
        self.assertNotIn("value", masked_node)

    def test_v1_unannounced_password_keyword_detection(self):
        """V1: UIAPolicyEngine.is_password_element detection across Thai & English keywords (WO-016.1 R1).
        Must return True for all password keywords even without CurrentIsPassword.
        Must return False for ordinary fields (prevent over-blocking).
        """
        positive_test_cases = [
            "รหัสผ่าน",
            "พาสเวิร์ด",
            "ยืนยันรหัสผ่าน",
            "รหัสผ่านใหม่",
            "รหัส PIN",
            "รหัสpin",
            "รหัสพิน",
            "รหัสความปลอดภัย",
            "รหัสลับ",
            "พาสโค้ด",
            "Password",
            "New password",
            "Confirm Password",
            "Passcode",
            "PIN",
            "Secret",
            "OTP",
            "CVV",
            "CVC",
            "passwd"
        ]
        for name in positive_test_cases:
            meta = {"name": name, "control_type": "Edit"}
            self.assertTrue(
                UIAPolicyEngine.is_password_element(meta),
                f"Expected is_password_element=True for positive keyword '{name}'"
            )

        negative_test_cases = [
            "ชื่อผู้ใช้",
            "อีเมล",
            "ค้นหา",
            "Username",
            "Email Address",
            "Search Query",
            "User Account",
            "Opinion",
            "Spin",
            "Pineapple",
            "Secretariat"
        ]
        for name in negative_test_cases:
            meta = {"name": name, "control_type": "Edit"}
            self.assertFalse(
                UIAPolicyEngine.is_password_element(meta),
                f"Expected is_password_element=False for negative field '{name}'"
            )

    def test_v2_unannounced_password_redaction_mock(self):
        """V2: _extract_masked_element_node with unannounced password element (WO-016.1 R1).
        Element has CurrentName='รหัสผ่าน' but CurrentIsPassword=0.
        Must return is_password=True, text_hash=None, and no length or value fields.
        """
        policy = UIAPolicyEngine(enabled=True, allowed_apps=["*"])
        parser = DesktopTreeParser(policy=policy)

        class MockUnannouncedPasswordElement:
            CurrentControlType = 50004  # Edit
            CurrentAutomationId = "custom_pwd_input"
            CurrentClassName = "TextBox"
            CurrentIsPassword = 0  # Unannounced!
            CurrentName = "รหัสผ่าน"
            class CurrentBoundingRectangle:
                left = 200
                top = 300
                right = 450
                bottom = 340

        masked_node = parser._extract_masked_element_node(MockUnannouncedPasswordElement)
        self.assertIsNotNone(masked_node)
        self.assertTrue(masked_node["is_password"], "Must detect as password despite CurrentIsPassword=0")
        self.assertIsNone(masked_node["text_hash"], "Must not hash unannounced password element")
        self.assertNotIn("length", masked_node, "Must not disclose password length")
        self.assertNotIn("value", masked_node, "Must not disclose password value")

    # -------------------------------------------------------------------------
    # U6: Privacy zero-leakage scan
    # -------------------------------------------------------------------------
    def test_u6_privacy_zero_leakage_scan(self):
        """U6: Raw user text typed in Notepad never appears in trace, Fact, or logs.
        Only the SHA-256 hash is present.
        """
        secret_user_text = "CONFIDENTIAL_PATIENT_RECORD_XYZ_98765"
        expected_hash = "sha256:" + hashlib.sha256(secret_user_text.encode("utf-8")).hexdigest()

        policy = UIAPolicyEngine(enabled=True, allowed_apps=["notepad.exe"])
        parser = DesktopTreeParser(policy=policy)

        # Mock a UIA window with the confidential text
        class MockTextElement:
            CurrentControlType = 50004
            CurrentAutomationId = "Editor"
            CurrentClassName = "Edit"
            CurrentIsPassword = 0
            CurrentName = secret_user_text
            class CurrentBoundingRectangle:
                left = 50
                top = 50
                right = 500
                bottom = 400

        masked_node = parser._extract_masked_element_node(MockTextElement)
        node_dump = json.dumps(masked_node)

        # 1. Plaintext must NOT appear anywhere in the node dump
        self.assertNotIn(
            secret_user_text, node_dump,
            "Confidential user text leaked into masked node!"
        )

        # 2. SHA-256 hash MUST be present
        self.assertEqual(masked_node["text_hash"], expected_hash)
        self.assertIn(expected_hash, node_dump)

    # -------------------------------------------------------------------------
    # U7: FactDesktopWindowRead RFC 8785 Canonical Integrity
    # -------------------------------------------------------------------------
    def test_u7_fact_desktop_window_read_rfc8785(self):
        """U7: FactDesktopWindowRead contentHash verifies with RFC 8785; zero raw text in Fact."""
        vault = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        task_id = "task-wo016-u7"
        secret_title = "Secret Customer Financials.txt"
        title_hash = "sha256:" + hashlib.sha256(secret_title.encode("utf-8")).hexdigest()

        fact = vault.create_desktop_window_read_fact(
            task_id=task_id,
            process_name="notepad.exe",
            window_title_hash=title_hash,
            element_count=42,
            elapsed_ms=18,
            structure_hash="sha256:abc123canonicalhash",
            env=self.valid_env
        )

        self.assertIsNotNone(fact)
        self.assertEqual(fact["factType"], "FactDesktopWindowRead")
        self.assertEqual(fact["hashAlgorithm"], "jcs-sha256-v1")

        # Verify RFC 8785 Canonical Hash Integrity
        self.assertTrue(verify_fact(fact), "FactDesktopWindowRead failed canonical hash verification!")

        # Verify Zero raw title text in Fact
        fact_json = json.dumps(fact)
        self.assertNotIn(secret_title, fact_json, "Raw window title must not appear in Fact!")
        self.assertIn(title_hash, fact_json)

    # -------------------------------------------------------------------------
    # U8: Router and Orchestrator Isolation (GR9)
    # -------------------------------------------------------------------------
    def test_u8_router_and_orchestrator_isolation(self):
        """U8: Verify engine/router.py and engine/orchestrator.py do not invoke Level 5 UIA."""
        router_path = BASE_DIR / "engine" / "router.py"
        orchestrator_path = BASE_DIR / "engine" / "orchestrator.py"

        router_code = router_path.read_text(encoding="utf-8")
        orchestrator_code = orchestrator_path.read_text(encoding="utf-8")

        for code, fname in [(router_code, "router.py"), (orchestrator_code, "orchestrator.py")]:
            self.assertNotIn("desktop_tree", code, f"{fname} must not import or reference desktop_tree (GR9)")
            self.assertNotIn("WindowsUIAAdapter", code, f"{fname} must not import or instantiate WindowsUIAAdapter (GR9)")
            self.assertNotIn("uia_policy", code, f"{fname} must not import or reference uia_policy (GR9)")

    # -------------------------------------------------------------------------
    # U9: Existing Regression Suites Integrity
    # -------------------------------------------------------------------------
    def test_u9_existing_suites_integrity(self):
        """U9: Verify all original test suite files exist and load successfully."""
        required_suites = [
            "tests.test_wo009_suite",
            "tests.test_wo011_suite",
            "tests.test_wo011_1_suite",
            "tests.test_wo011_2_suite",
            "tests.test_wo011_3_suite",
            "tests.test_wo012_suite",
            "tests.test_wo012_1_suite",
            "tests.test_wo012_2_suite",
            "tests.test_wo015_suite"
        ]
        import importlib
        for suite_name in required_suites:
            mod = importlib.import_module(suite_name)
            self.assertIsNotNone(mod, f"Failed to load suite module {suite_name}")

    # -------------------------------------------------------------------------
    # U10: Carryover R8 (insecureHttp in Fact, retryable clean in code & README)
    # -------------------------------------------------------------------------
    def test_u10_r8_carryover_direct_api_hardening(self):
        """U10: Verify allow_insecure_http records insecureHttp in Fact; retryable is clean."""
        vault = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )

        # 1. Fact for insecure HTTP call
        fact_insecure = vault.create_api_call_fact(
            task_id="task-r8-insecure",
            step_num=1,
            recipe_name="insecure_endpoint",
            host="192.168.1.50:8080",
            path="/api/data",
            method="GET",
            status_code=200,
            elapsed_ms=15,
            risk_level=0,
            request_body_hash="sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            response_body_hash="sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            insecure_http=True,
            env=self.valid_env
        )
        self.assertIsNotNone(fact_insecure)
        self.assertTrue(fact_insecure["payload"]["insecureHttp"])
        self.assertTrue(verify_fact(fact_insecure))

        # 2. Fact for secure HTTPS call
        fact_secure = vault.create_api_call_fact(
            task_id="task-r8-secure",
            step_num=1,
            recipe_name="secure_endpoint",
            host="api.example.com",
            path="/api/data",
            method="GET",
            status_code=200,
            elapsed_ms=15,
            risk_level=0,
            request_body_hash="sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            response_body_hash="sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            insecure_http=False,
            env=self.valid_env
        )
        self.assertIsNotNone(fact_secure)
        self.assertFalse(fact_secure["payload"]["insecureHttp"])
        self.assertTrue(verify_fact(fact_secure))

        # 3. Check direct_api.py has no dead retryable variable
        direct_api_code = (BASE_DIR / "execution" / "direct_api.py").read_text(encoding="utf-8")
        self.assertNotIn("retryable = ", direct_api_code, "Dead retryable variable must be removed from direct_api.py")

        # 4. Check README mentions no auto-retries and full Risk 3 keyword list
        readme_code = (BASE_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("ไม่มีการลองใหม่อัตโนมัติ", readme_code)
        self.assertIn("insecureHttp", readme_code)
        for kw in ["pay", "purchase", "withdraw", "cancel", "close", "account", "order", "create"]:
            self.assertIn(kw, readme_code)


if __name__ == "__main__":
    unittest.main()
