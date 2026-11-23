"""Comprehensive Test Suite for WO-011 (V1 - V9).
Executes all verification criteria for browser-engine-desktop 0.3:
- V1: vision_enabled=false regression check (identical to 0.2.1)
- V2: External endpoint rejection (strictly internal LAN only)
- V3: Visual-only button fixture (Risk 0 click succeeds)
- V4: Financial button on vision only blocked (no_grounded_target, 0 clicks)
- V5: Confidence below threshold (human_takeover_required, 0 clicks)
- V6: Trace SHA-256 hash privacy & retention auto-purge
- V7: Timing and VRAM metrics report (P50/P95, VRAM lifecycle, vLLM impact)
- V8: Service drop / crash resilience (safe termination, no coordinate guessing)
- V9: Inverted risk classifier on financial context (deny-by-default, 56+ assertions)
"""
import base64
import json
import os
import pathlib
import sys
import time
import unittest
from typing import Any, Dict, List

# Ensure base directory in sys.path
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from playwright.sync_api import sync_playwright

from models.schemas import ActionStep, ActionType, MacroDefinition, RiskLevel, Strategy, TargetCandidate
from models.vision import VisionClient, is_lan_endpoint, purge_old_screenshots, set_global_vision_client, get_global_vision_client, SCREENSHOTS_DIR
from policy.risk import RiskPolicyEngine
from policy.takeover import HumanTakeoverManager
from execution.macro import ConditionalMacroRunner
from engine.router import InteractionRouter
from engine.orchestrator import InteractiveAutomationOrchestrator
from tests.fixtures_server import run_fixture_server, FixtureServer


class TestWO011Suite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_server: FixtureServer = run_fixture_server()
        cls.fixture_port = cls.fixture_server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.fixture_port}"
        print(f"[*] WO-011 Fixture Server started at {cls.base_url}")

    @classmethod
    def tearDownClass(cls):
        set_global_vision_client(None)
        cls.fixture_server.shutdown()

    def test_v1_vision_disabled_regression(self):
        """V1: vision_enabled=false -> behavior identical to 0.2.1, no crash, fallback to no_grounded_target."""
        # 1. Config check: default is vision_enabled=false
        from models.vision import load_config
        cfg = load_config()
        self.assertFalse(cfg.get("vision_enabled", False))

        # 2. Router check when vision disabled
        client_disabled = VisionClient(vision_enabled=False)
        set_global_vision_client(client_disabled)

        mock_page = type("MockPage", (), {
            "url": f"{self.base_url}/visual_button",
            "evaluate": lambda self, s, *a: [],
            "snapshot": lambda self, **kw: {}
        })()

        cand = InteractionRouter.resolve_target(mock_page, "Action Button")
        self.assertEqual(cand.strategy, Strategy.VISION)
        self.assertEqual(cand.confidence, 0.75)
        self.assertTrue(cand.metadata.get("needs_vision_grounding"))
        self.assertIsNone(cand.coordinates)

        # 3. Macro execution fails safely with no_grounded_target (0 clicks)
        runner = ConditionalMacroRunner(adapter=type("DummyAdapter", (), {})())
        macro = MacroDefinition(
            macro_id="macro-v1",
            steps=[ActionStep(action=ActionType.CLICK, target=cand)]
        )
        res = runner.run_macro(macro, mock_page)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "no_grounded_target")

    def test_v2_lan_endpoint_rejection(self):
        """V2: External endpoints outside private LAN are strictly rejected and logged in trace."""
        # External URLs that must be rejected
        external_urls = [
            "http://api.openai.com/v1/chat/completions",
            "https://cloud.anthropic.com/v1/messages",
            "http://8.8.8.8:8000/vlm",
            "http://1.1.1.1/ai/api/generate",
            "http://vlm.external-cloud.com:11434/api/generate",
            "ftp://192.168.1.108/ai"  # invalid scheme
        ]
        for url in external_urls:
            is_lan, msg = is_lan_endpoint(url)
            self.assertFalse(is_lan, f"URL should be rejected: {url}")
            self.assertIn("external_endpoint_rejected", msg)

        # Private LAN URLs that must be accepted
        lan_urls = [
            "http://127.0.0.1:8205/api/status",
            "http://localhost/ai/api/generate",
            "http://192.168.1.108/ai/api/generate",
            "http://10.0.4.20:11434/api/generate",
            "http://172.20.0.2:8000/vlm",
            "http://dgx-spark.local/ai/api/generate"
        ]
        from unittest.mock import patch
        with patch("socket.gethostbyname", side_effect=lambda h: "192.168.1.108" if "dgx-spark" in h else "127.0.0.1"):
            for url in lan_urls:
                is_lan, msg = is_lan_endpoint(url)
                self.assertTrue(is_lan, f"URL should be accepted: {url} ({msg})")

        # Test VisionClient rejection behavior
        ext_client = VisionClient(
            endpoint="http://api.openai.com/v1/generate",
            vision_enabled=True
        )
        mock_page = type("MockPage", (), {"url": "http://test", "screenshot": lambda **k: b"dummy"})()
        cand = ext_client.locate_target(mock_page, "button")
        self.assertEqual(cand.confidence, 0.0)
        self.assertEqual(cand.metadata.get("error"), "external_endpoint_rejected")

    def test_v3_visual_only_button_click_succeeds(self):
        """V3: Visual-only canvas button -> Level 4 vision grounds coordinates and Risk 0 click succeeds."""
        initial_clicks = self.fixture_server.visual_clicks

        # Backend mock returning accurate canvas coordinates for Risk 0 button
        def visual_backend(target_desc, img_bytes, viewport_size):
            return {
                "coordinates": (100, 163),  # Canvas (#visual-btn) center at (100, 163)
                "confidence": 0.92
            }

        client = VisionClient(
            vision_enabled=True,
            backend_override=visual_backend
        )
        set_global_vision_client(client)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.goto(f"{self.base_url}/visual_button")
            page.wait_for_timeout(500)

            # Step 1: Verify DOM and AX parsers find no matching interactive elements
            cand_dom = InteractionRouter.resolve_target(page, "Action Button")
            # Should fall back to Level 4 Vision
            self.assertEqual(cand_dom.strategy, Strategy.VISION)
            self.assertEqual(cand_dom.coordinates, (100, 163))
            self.assertEqual(cand_dom.confidence, 0.92)

            # Step 2: Run via Orchestrator
            orch = InteractiveAutomationOrchestrator()
            # Goal is Risk 0
            plan_mock = type("MockPlan", (), {
                "sub_tasks": [{"action": "click", "target": "Action Button"}]
            })()
            orch.planner.create_plan = lambda **kw: plan_mock

            res = orch.run_task(page=page, goal="คลิกปุ่ม Action Button เพื่อดูข้อมูล")
            page.wait_for_timeout(500)

            # Step 3: Verify click was physically registered on fixture server
            self.assertEqual(self.fixture_server.visual_clicks, initial_clicks + 1)
            self.assertTrue(res.get("ok"))
            browser.close()

    def test_v4_financial_button_on_vision_only_blocked(self):
        """V4: Financial button detectable only from vision must NOT click and return no_grounded_target (0 clicks)."""
        initial_clicks = self.fixture_server.financial_clicks

        # Vision locates the payment button coordinates
        def payment_backend(target_desc, img_bytes, viewport_size):
            return {
                "coordinates": (110, 165),  # Canvas coords of Confirm Pay
                "confidence": 0.95
            }

        client = VisionClient(
            vision_enabled=True,
            backend_override=payment_backend
        )
        set_global_vision_client(client)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.goto(f"{self.base_url}/visual_financial")
            page.wait_for_timeout(500)

            # Target is financial: Confirm Pay
            orch = InteractiveAutomationOrchestrator()
            plan_mock = type("MockPlan", (), {
                "sub_tasks": [{"action": "click", "target": "Confirm Pay"}]
            })()
            orch.planner.create_plan = lambda **kw: plan_mock

            res = orch.run_task(page=page, goal="ชำระเงินค่าสินค้า 250 บาท")
            page.wait_for_timeout(500)

            # Ground Rule 3 verification:
            # 1. Macro must return no_grounded_target
            self.assertEqual(res.get("status"), "no_grounded_target")
            self.assertEqual(res.get("error"), "no_grounded_target")

            # 2. Fixture clicks MUST BE 0 (no click performed)
            self.assertEqual(self.fixture_server.financial_clicks, initial_clicks)
            browser.close()

    def test_v5_low_confidence_triggers_takeover(self):
        """V5: Vision confidence < 0.75 -> human_takeover_required (low_vision_confidence) and 0 clicks."""
        initial_clicks = self.fixture_server.visual_clicks

        # Return low confidence 0.62 (< 0.75)
        def low_conf_backend(target_desc, img_bytes, viewport_size):
            return {
                "coordinates": (100, 163),
                "confidence": 0.62
            }

        client = VisionClient(
            vision_enabled=True,
            backend_override=low_conf_backend
        )
        set_global_vision_client(client)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.goto(f"{self.base_url}/visual_button")

            orch = InteractiveAutomationOrchestrator(takeover_timeout_s=0.5)
            plan_mock = type("MockPlan", (), {
                "sub_tasks": [{"action": "click", "target": "Action Button"}]
            })()
            orch.planner.create_plan = lambda **kw: plan_mock

            res = orch.run_task(page=page, goal="คลิกดูข้อมูล")

            # Ground Rule 4 verification:
            self.assertEqual(res.get("status"), "human_takeover_required")
            self.assertEqual(res.get("reason"), "low_vision_confidence")

            # Zero clicks executed
            self.assertEqual(self.fixture_server.visual_clicks, initial_clicks)
            browser.close()

    def test_v6_image_hash_privacy_and_retention_purge(self):
        """V6: Traces store SHA-256 only (zero image bytes in jsonl), and raw images older than retention are purged."""
        # 1. Check trace jsonl files: search for base64 / binary image signatures
        traces_dir = BASE_DIR / "data" / "traces"
        if traces_dir.is_dir():
            for jsonl_file in traces_dir.glob("*.jsonl"):
                content = jsonl_file.read_text(encoding="utf-8")
                self.assertNotIn("data:image", content, f"Trace {jsonl_file.name} contains data:image data URL")
                self.assertNotIn(";base64,", content, f"Trace {jsonl_file.name} contains raw base64 data")

        # 2. Retention test
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        old_file = SCREENSHOTS_DIR / "test_old_screenshot.jpg"
        fresh_file = SCREENSHOTS_DIR / "test_fresh_screenshot.jpg"

        old_file.write_bytes(b"dummy_old_image")
        fresh_file.write_bytes(b"dummy_fresh_image")

        # Backdate old_file mtime by 26 hours
        old_mtime = time.time() - (26 * 3600)
        os.utime(old_file, (old_mtime, old_mtime))

        purged = purge_old_screenshots(max_age_hours=24)
        self.assertGreaterEqual(purged, 1)
        self.assertFalse(old_file.exists(), "Old screenshot should have been purged")
        self.assertTrue(fresh_file.exists(), "Fresh screenshot must be retained")

        # Cleanup test fresh file
        fresh_file.unlink(missing_ok=True)

    def test_v7_latency_and_vram_report(self):
        """V7: Report P50/P95 of vision steps, VRAM lifecycle before/during/after, and vLLM impact."""
        benchmark_file = BASE_DIR / "v7_benchmark_results.json"
        if not benchmark_file.is_file():
            alt = BASE_DIR.parent.parent.parent / "coordination" / "codex-anti" / "anti-output" / "WO-011" / "v7_benchmark_results.json"
            if alt.is_file():
                benchmark_file = alt
        self.assertTrue(benchmark_file.is_file(), "v7_benchmark_results.json must exist")
        data = json.loads(benchmark_file.read_text(encoding="utf-8"))

        vram = data.get("vram_gib", {})
        self.assertEqual(vram.get("before"), 0.0, "VRAM before vision must be 0 GiB")
        self.assertGreaterEqual(vram.get("during"), 10.0, "VRAM during vision should reflect model load (~12 GiB)")
        self.assertEqual(vram.get("after"), 0.0, "VRAM after vision must return to 0 GiB via keep_alive=0")

        vllm_impact = data.get("vllm_qwen27b_latency_ms", {})
        self.assertIn("diff_ms", vllm_impact)
        # Verify latency difference doesn't cause significant starvation
        self.assertLess(vllm_impact.get("diff_ms"), 500.0)

        vision_lat = data.get("vision_latency_ms", {})
        self.assertGreater(vision_lat.get("p50"), 0)
        self.assertGreater(vision_lat.get("p95"), 0)

    def test_v8_service_drop_crash_resilience(self):
        """V8: When vision endpoint drops midway, system halts safely without crashing or guessing coordinates."""
        # Client pointing to a closed/unreachable port on localhost
        dead_client = VisionClient(
            endpoint="http://127.0.0.1:59998/ai/api/generate",
            vision_enabled=True,
            timeout_seconds=0.5
        )
        set_global_vision_client(dead_client)

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            page.goto(f"{self.base_url}/visual_button")

            orch = InteractiveAutomationOrchestrator(takeover_timeout_s=0.5)
            plan_mock = type("MockPlan", (), {
                "sub_tasks": [{"action": "click", "target": "Action Button"}]
            })()
            orch.planner.create_plan = lambda **kw: plan_mock

            # Must terminate safely with no_grounded_target
            res = orch.run_task(page=page, goal="คลิกดูข้อมูล")
            self.assertFalse(res.get("ok"))
            self.assertEqual(res.get("status"), "no_grounded_target")
            self.assertEqual(res.get("error"), "no_grounded_target")
            browser.close()

    def test_v9_inverted_risk_classifier_matrix(self):
        """V9: Inverted risk classifier on financial context (56+ cases).
        On checkout/payment pages: unlisted terms, empty buttons, and financial verbs default to Risk >= 2.
        Outside financial context: safe terms stay Risk 0 to prevent over-blocking.
        """
        checkout_urls = [
            "https://store.local/checkout",
            "https://store.local/payment",
            "https://store.local/cart",
            "https://store.local/order",
            "https://store.local/billing",
            "https://store.local/ชำระเงิน"
        ]

        # 1. Unlisted / Financial actions on checkout pages -> MUST BE >= Risk 2 (or Risk 3)
        financial_and_unlisted_terms = [
            "จ่ายเลย", "ซื้อ", "Buy", "Order now", "รูดบัตร", "เพิ่มบัตรใหม่", "โอนเลย",
            "ชำระเงิน", "ซื้อเลย", "จ่ายเงิน", "Pay Now", "Complete Purchase",
            "ยืนยันการสั่งซื้อ", "สั่งซื้อ", "โอนเงิน", "ถอนเงิน", "เติมเงิน", "ผูกบัตร",
            "", "   ", "ปุ่มไอคอน", "confirm_selection", "apply_coupon", "select_shipping"
        ]

        tested_checkout = 0
        for u in checkout_urls[:3]:  # sample 3 checkout urls
            for term in financial_and_unlisted_terms:
                lvl = RiskPolicyEngine.classify(ActionType.CLICK, term, url=u)
                self.assertGreaterEqual(
                    lvl.value,
                    RiskLevel.RISK_2_CONFIRM.value,
                    f"Expected at least Risk 2 for '{term}' on {u}, got {lvl}"
                )
                tested_checkout += 1

        # 2. Pure read-only actions on checkout pages -> Risk 0
        safe_checkout_terms = [
            "ดูรายละเอียด", "อ่านเพิ่มเติม", "view details", "inspect", "read"
        ]
        for term in safe_checkout_terms:
            lvl = RiskPolicyEngine.classify(ActionType.CLICK, term, url="https://store.local/checkout")
            self.assertEqual(lvl, RiskLevel.RISK_0_SAFE, f"Expected Risk 0 for safe term '{term}' on checkout, got {lvl}")
            tested_checkout += 1

        # 3. Outside checkout context -> normal exploration stays Risk 0
        safe_non_checkout_terms = [
            "search", "next page", "ดูรายละเอียด", "อ่านเพิ่มเติม", "inspect",
            "browse", "read", "view details", "filter", "home", "refresh", "view"
        ]
        tested_non_checkout = 0
        for term in safe_non_checkout_terms:
            lvl = RiskPolicyEngine.classify(ActionType.CLICK, term, url="https://store.local/home")
            self.assertEqual(lvl, RiskLevel.RISK_0_SAFE, f"Expected Risk 0 for '{term}' on home, got {lvl}")
            tested_non_checkout += 1

        total_tested = tested_checkout + tested_non_checkout
        self.assertGreaterEqual(total_tested, 56, f"Expected >= 56 items tested, got {total_tested}")
        print(f"\n[PASS] V9 Inverted Risk Classifier verified across {total_tested} test cases!")


if __name__ == "__main__":
    unittest.main()
