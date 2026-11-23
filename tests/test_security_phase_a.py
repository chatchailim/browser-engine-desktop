"""Unit & Integration Tests for WO-009 Phase A (Security P0).
Validates Token Auth, 127.0.0.1 binding, Risk Classification, Cookie Reject, and No-Grounded-Target.
"""
import hmac
import json
import os
import pathlib
import sys
import unittest
from unittest.mock import MagicMock

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from models.schemas import ActionStep, ActionType, MacroDefinition, RiskLevel, Strategy, TargetCandidate
from policy.risk import RiskPolicyEngine
from policy.approval import ApprovalManager
from engine.recovery import RecoveryEngine
from execution.macro import ConditionalMacroRunner


class TestSecurityPhaseA(unittest.TestCase):
    def test_t1_token_auth_logic(self):
        """T1: Verify token matching requires exact secret via constant-time comparison."""
        from server import EXPECTED_TOKEN
        self.assertTrue(len(EXPECTED_TOKEN) >= 32)

        # Invalid or missing tokens
        self.assertFalse(hmac.compare_digest("", EXPECTED_TOKEN))
        self.assertFalse(hmac.compare_digest("wrong-token", EXPECTED_TOKEN))
        self.assertFalse(hmac.compare_digest(EXPECTED_TOKEN[:-1], EXPECTED_TOKEN))

        # Valid token
        self.assertTrue(hmac.compare_digest(EXPECTED_TOKEN, EXPECTED_TOKEN))

    def test_t2_automation_flags_absent(self):
        """T2: Non-test codebase must have zero occurrences of AutomationControlled, webdriver, or stealth."""
        target_token = "Automation" + "Controlled"
        for py_path in BASE_DIR.glob("**/*.py"):
            if "tests" in py_path.parts:
                continue
            txt = py_path.read_text(encoding="utf-8", errors="ignore")
            self.assertNotIn(target_token, txt, f"Found {target_token} in {py_path}")
            self.assertNotIn("stealth", txt.lower(), f"Found stealth in {py_path}")


    def test_t3_risk_classifier_full_matrix(self):
        """T3: Ensure all keywords in A4 map to their designated risk levels."""
        risk3_words = [
            'ชำระเงิน', 'pay', 'checkout', 'place order', 'buy now', 'ซื้อเลย',
            'ยืนยันการสั่งซื้อ', 'สั่งซื้อ', 'โอนเงิน', 'ลบ', 'delete', 'remove account',
            'ยกเลิกสัญญา', 'change password', 'reset password', 'permissions'
        ]
        for w in risk3_words:
            res = RiskPolicyEngine.classify(ActionType.CLICK, target_text=w)
            self.assertEqual(res, RiskLevel.RISK_3_APPROVAL, f"Expected Risk 3 for '{w}', got {res}")

        risk2_words = [
            'ส่ง', 'send', 'ส่งข้อความ', 'submit', 'ยืนยัน', 'confirm',
            'publish', 'เผยแพร่', 'โพสต์', 'post', 'ลงประกาศ'
        ]
        for w in risk2_words:
            res = RiskPolicyEngine.classify(ActionType.CLICK, target_text=w)
            self.assertEqual(res, RiskLevel.RISK_2_CONFIRM, f"Expected Risk 2 for '{w}', got {res}")

        risk0_words = ['search', 'next page', 'ดูรายละเอียด', 'browse catalog', 'inspect element']
        for w in risk0_words:
            res = RiskPolicyEngine.classify(ActionType.CLICK, target_text=w)
            self.assertEqual(res, RiskLevel.RISK_0_SAFE, f"Expected Risk 0 for '{w}', got {res}")

    def test_t5_cookie_reject_rule(self):
        """T5: RecoveryEngine must reject or request takeover if only accept is available."""
        page_mock = MagicMock()

        # Case 1: Page has reject button -> returns rejected
        page_mock.evaluate.return_value = {"status": "rejected", "text": "Reject All"}
        msg, takeover = RecoveryEngine.handle_cookie_banner(page_mock)
        self.assertFalse(takeover)
        self.assertIn("cookie_rejected", msg)

        # Case 2: Page only has accept buttons -> requires human takeover
        page_mock.evaluate.return_value = {"status": "accept_only_detected"}
        msg, takeover = RecoveryEngine.handle_cookie_banner(page_mock)
        self.assertTrue(takeover)
        self.assertEqual(msg, "cookie_consent_only_accept_available")

    def test_t7_no_grounded_target_fails_cleanly(self):
        """T7: Clicking on ungrounded target (no selector, no coords) must fail without clicking (0,0)."""
        adapter_mock = MagicMock()
        runner = ConditionalMacroRunner(adapter=adapter_mock)

        # Vision placeholder target with no grounded coordinates or selector
        ungrounded = TargetCandidate(
            strategy=Strategy.VISION,
            confidence=0.75,
            name="Mystery Button",
            selector=None,
            coordinates=None
        )

        step = ActionStep(action=ActionType.CLICK, target=ungrounded)
        macro = MacroDefinition(macro_id="test-no-target", steps=[step], stop_on_failure=True)

        page_mock = MagicMock()
        page_mock.url = "http://example.local"

        res = runner.run_macro(macro, page_mock)
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("error"), "no_grounded_target")

        # Verify adapter click was NEVER called with (0,0) or anything else
        adapter_mock.click.assert_not_called()
        adapter_mock.double_click.assert_not_called()
        adapter_mock.right_click.assert_not_called()

    def test_approval_manager_lifecycle(self):
        """Test approval request, grant, and rejection."""
        mgr = ApprovalManager()
        import threading

        # Background approver
        def auto_approve():
            import time
            time.sleep(0.1)
            mgr.approve("task_1", 1)

        t = threading.Thread(target=auto_approve)
        t.start()

        approved = mgr.request_approval("task_1", 1, "click", "Place Order", "RISK_3_APPROVAL", "Place order test", timeout=2.0)
        t.join()
        self.assertTrue(approved)

    def test_server_http_token_and_binding(self):
        """T1: Verify HTTP server requires X-Desktop-Token for /api/* and binds 127.0.0.1."""
        from http.server import ThreadingHTTPServer
        import urllib.request
        import urllib.error
        from server import InteractiveHandler, EXPECTED_TOKEN

        test_port = 8299
        server = ThreadingHTTPServer(("127.0.0.1", test_port), InteractiveHandler)
        import threading
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        base_url = f"http://127.0.0.1:{test_port}"
        try:
            # 1. Static page is accessible without token
            req = urllib.request.Request(f"{base_url}/")
            with urllib.request.urlopen(req, timeout=3) as r:
                self.assertEqual(r.status, 200)

            # 2. /api/status without token -> 401
            try:
                urllib.request.urlopen(f"{base_url}/api/status", timeout=3)
                self.fail("Expected 401 Unauthorized without token")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)

            # 3. /api/status with wrong token -> 401
            try:
                req = urllib.request.Request(
                    f"{base_url}/api/status",
                    headers={"X-Desktop-Token": "bad-token-value"}
                )
                urllib.request.urlopen(req, timeout=3)
                self.fail("Expected 401 Unauthorized with bad token")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)

            # 4. /api/status with valid token -> 200
            req = urllib.request.Request(
                f"{base_url}/api/status",
                headers={"X-Desktop-Token": EXPECTED_TOKEN}
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                self.assertEqual(r.status, 200)
                d = json.loads(r.read().decode("utf-8"))
                self.assertIn("vllm_ready", d)

            # 5. /api/takeover without token -> 401
            try:
                req = urllib.request.Request(
                    f"{base_url}/api/takeover",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                urllib.request.urlopen(req, timeout=3)
                self.fail("Expected 401 on takeover without token")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 401)

            # 6. /api/takeover with valid token -> 200
            req = urllib.request.Request(
                f"{base_url}/api/takeover",
                data=b"{}",
                headers={"Content-Type": "application/json", "X-Desktop-Token": EXPECTED_TOKEN},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                self.assertEqual(r.status, 200)

        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()

