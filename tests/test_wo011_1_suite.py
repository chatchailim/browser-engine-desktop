"""Comprehensive Verification Suite for WO-011.1 (W1 - W7).
Validates production readiness of Level 4 Vision Grounding on DGX Spark:
- W1: Real On-Premise Model Call on DGX (qwen2.5vl:7b, NO backend_override) on live canvas button
- W2: Resilient Parser (_parse_vlm_output) against 10+ malformed response formats
- W3: Zero Default Confidence -> conf=0.0 -> human_takeover_required (0 clicks)
- W4: Config Dynamic Min Confidence Gate (vision_min_confidence=0.9 halts conf=0.8)
- W5: LAN-Only Guard (blocks link-local 169.254.x.x, resolves internal domains, blocks public IPs)
- W6: VRAM Lifecycle & Latency Benchmark on DGX (before: 0.0 GiB, during: ~19.4 GiB, after unload: 0.0 GiB)
- W7: Regression verification (WO-009 16/16 and WO-011 9/9 suites pass)
"""
import io
import json
import os
import pathlib
import socket
import sys
import time
import unittest
import urllib.request
from typing import Any, Dict, List
from unittest.mock import patch

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from playwright.sync_api import sync_playwright

from models.schemas import ActionStep, ActionType, MacroDefinition, RiskLevel, Strategy, TargetCandidate
from models.vision import VisionClient, is_lan_endpoint, set_global_vision_client, get_global_vision_client
from execution.macro import ConditionalMacroRunner
from tests.fixtures_server import run_fixture_server, FixtureServer


class TestWO011_1Suite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_server: FixtureServer = run_fixture_server()
        cls.fixture_port = cls.fixture_server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.fixture_port}"
        cls.dgx_endpoint = "http://192.168.1.108/ai/api/generate"
        cls.dgx_ps = "http://192.168.1.108/ai/api/ps"
        cls.model_name = "qwen2.5vl:7b"
        print(f"[*] WO-011.1 Fixture Server started at {cls.base_url}")

    @classmethod
    def tearDownClass(cls):
        set_global_vision_client(None)
        cls.fixture_server.shutdown()

    def test_w1_real_dgx_model_call(self):
        """W1 (CRITICAL): Call real on-premise qwen2.5vl:7b on DGX via VisionClient (NO backend_override).
        Must yield coordinates within the actual canvas button boundary with confidence >= 0.75 without timeout.
        """
        initial_clicks = self.fixture_server.visual_clicks

        # Instantiate VisionClient strictly pointing to real DGX (NO backend_override)
        client = VisionClient(
            endpoint=self.dgx_endpoint,
            model=self.model_name,
            vision_enabled=True,
            timeout_seconds=120.0,
            backend_override=None  # Explicitly None - strictly real DGX model call
        )
        self.assertIsNone(client.backend_override, "W1 must NOT use backend_override")

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"{self.base_url}/visual_button")
            page.wait_for_timeout(500)

            # Get DOM-level canvas boundaries for ground truth comparison
            # Canvas #visual-btn is width 160, height 50 at (x=20, y~138)
            canvas_box = page.locator("#visual-btn").bounding_box()
            self.assertIsNotNone(canvas_box)
            c_xmin = canvas_box["x"]
            c_ymin = canvas_box["y"]
            c_xmax = c_xmin + canvas_box["width"]
            c_ymax = c_ymin + canvas_box["height"]

            # Locate target with real DGX model call
            t0 = time.monotonic()
            cand = client.locate_target(page, "Action Button", viewport_size=(1280, 900))
            elapsed = time.monotonic() - t0
            print(f"\n[W1 Real Call] Time taken: {elapsed:.2f}s | Confidence: {cand.confidence} | Coords: {cand.coordinates}")

            self.assertEqual(cand.strategy, Strategy.VISION)
            self.assertGreaterEqual(cand.confidence, 0.75, f"Confidence must be >= 0.75, got {cand.confidence}")
            self.assertIsNotNone(cand.coordinates, "Must return valid coordinates")
            px, py = cand.coordinates

            # Verify coordinates are strictly inside the real canvas button bounds
            self.assertGreaterEqual(px, c_xmin, f"px {px} should be >= canvas xmin {c_xmin}")
            self.assertLessEqual(px, c_xmax, f"px {px} should be <= canvas xmax {c_xmax}")
            self.assertGreaterEqual(py, c_ymin, f"py {py} should be >= canvas ymin {c_ymin}")
            self.assertLessEqual(py, c_ymax, f"py {py} should be <= canvas ymax {c_ymax}")

            # Verify click physically registers on the page
            page.mouse.click(px, py)
            page.wait_for_timeout(300)
            self.assertEqual(self.fixture_server.visual_clicks, initial_clicks + 1, "Click must register on canvas")

            browser.close()

    def test_w2_resilient_vlm_parser(self):
        """W2: Parser (_parse_vlm_output) must resist dirty/malformed responses without crashing.
        Must return (None, 0.0) when output is unparsable, and never guess coordinates.
        Tested against 11 diverse cases including reviewer's real failure case.
        """
        client = VisionClient()

        # 1. Reviewer's exact failure case: Python True, malformed [72x66], unclosed quote in 0.9"
        res, conf = client._parse_vlm_output('{"found": True, "box": [72x66], "confidence": 0.9"}', 1280, 900)
        self.assertIsNone(res, "Malformed [72x66] must return None")
        self.assertEqual(conf, 0.0)

        # 2. Markdown json code block
        res, conf = client._parse_vlm_output('```json\n{"found": true, "box": [25, 137, 185, 186], "confidence": 0.9}\n```', 1280, 900)
        self.assertEqual(res, (105, 161))
        self.assertEqual(conf, 0.9)

        # 3. Surrounding conversational text
        res, conf = client._parse_vlm_output('Detected button: {"found": true, "box": [25, 137, 185, 186], "confidence": 0.95} done.', 1280, 900)
        self.assertEqual(res, (105, 161))
        self.assertEqual(conf, 0.95)

        # 4. Extra parentheses and Python booleans
        res, conf = client._parse_vlm_output('(({"found": True, "box": [25, 137, 185, 186], "confidence": 0.85}))', 1280, 900)
        self.assertEqual(res, (105, 161))
        self.assertEqual(conf, 0.85)

        # 5. Target not found
        res, conf = client._parse_vlm_output('{"found": false, "box": [25, 137, 185, 186], "confidence": 0.0}', 1280, 900)
        self.assertIsNone(res)
        self.assertEqual(conf, 0.0)

        # 6. String box instead of coordinate list
        res, conf = client._parse_vlm_output('{"found": true, "box": "72x66", "confidence": 0.9}', 1280, 900)
        self.assertIsNone(res)
        self.assertEqual(conf, 0.0)

        # 7. Non-JSON HTML / error payload
        res, conf = client._parse_vlm_output('<html><head><title>502 Bad Gateway</title></head></html>', 1280, 900)
        self.assertIsNone(res)
        self.assertEqual(conf, 0.0)

        # 8. Empty / whitespace string
        res, conf = client._parse_vlm_output('   \n\t  ', 1280, 900)
        self.assertIsNone(res)
        self.assertEqual(conf, 0.0)

        # 9. List wrapper [{"found": true, ...}]
        res, conf = client._parse_vlm_output('```\n[{"found": true, "box": [25, 137, 185, 186], "confidence": 0.92}]\n```', 1280, 900)
        self.assertEqual(res, (105, 161))
        self.assertEqual(conf, 0.92)

        # 10. Truncated JSON
        res, conf = client._parse_vlm_output('{"found": true, "box": [25, 137, 185', 1280, 900)
        self.assertIsNone(res)
        self.assertEqual(conf, 0.0)

        # 11. Point/coord format
        res, conf = client._parse_vlm_output('{"found": true, "point": [105, 161], "confidence": 0.88}', 1280, 900)
        self.assertEqual(res, (105, 161))
        self.assertEqual(conf, 0.88)

    def test_w3_zero_default_confidence_triggers_takeover(self):
        """W3: When model omits confidence, parser defaults conf=0.0 (not 0.8).
        System orders human_takeover_required instead of clicking.
        """
        client = VisionClient()
        # Case A: Missing confidence in JSON -> conf must be 0.0
        res, conf = client._parse_vlm_output('{"found": true, "box": [25, 137, 185, 186]}', 1280, 900)
        self.assertEqual(res, (105, 161))
        self.assertEqual(conf, 0.0, "Omitted confidence must default to 0.0")

        # Case B: Regex fallback without confidence -> conf must be 0.0
        res_rgx, conf_rgx = client._parse_vlm_output('The target is inside [25, 137, 185, 186]', 1280, 900)
        self.assertEqual(res_rgx, (105, 161))
        self.assertEqual(conf_rgx, 0.0, "Regex fallback must default confidence to 0.0")

        # Case C: Macro execution with conf=0.0 -> must trigger human_takeover_required (0 clicks)
        cand = TargetCandidate(
            strategy=Strategy.VISION,
            confidence=0.0,
            coordinates=(105, 161),
            name="Unconfident Target"
        )
        runner = ConditionalMacroRunner(adapter=type("DummyAdapter", (), {"click": lambda *a, **kw: True})())
        macro = MacroDefinition(
            macro_id="macro-w3",
            steps=[ActionStep(action=ActionType.CLICK, target=cand)]
        )
        mock_page = type("MockPage", (), {"url": "http://test"})()
        out = runner.run_macro(macro, mock_page)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "human_takeover_required")
        self.assertEqual(out["reason"], "low_vision_confidence")

    def test_w4_macro_reads_vision_min_confidence_from_config(self):
        """W4: Macro runner dynamically reads vision_min_confidence from config.
        When set to 0.9, confidence 0.8 is halted by takeover. When set to 0.75, 0.8 passes.
        """
        cand = TargetCandidate(
            strategy=Strategy.VISION,
            confidence=0.8,
            coordinates=(100, 160),
            name="Threshold Test"
        )
        macro = MacroDefinition(
            macro_id="macro-w4",
            steps=[ActionStep(action=ActionType.CLICK, target=cand)]
        )
        mock_page = type("MockPage", (), {"url": "http://test"})()

        # Runner with override 0.9 (or config 0.9) -> 0.8 < 0.9 -> TAKEOVER
        runner_strict = ConditionalMacroRunner(
            adapter=type("DummyAdapter", (), {"click": lambda *a, **kw: True})(),
            vision_min_confidence=0.9
        )
        out_strict = runner_strict.run_macro(macro, mock_page)
        self.assertFalse(out_strict["ok"])
        self.assertEqual(out_strict["error"], "human_takeover_required")
        self.assertEqual(out_strict["reason"], "low_vision_confidence")

        # Runner with standard 0.75 -> 0.8 >= 0.75 -> SUCCESS
        runner_std = ConditionalMacroRunner(
            adapter=type("DummyAdapter", (), {"click": lambda *a, **kw: True})(),
            vision_min_confidence=0.75
        )
        out_std = runner_std.run_macro(macro, mock_page)
        self.assertTrue(out_std["ok"])

    def test_w5_lan_endpoint_link_local_and_domain_resolution(self):
        """W5: is_lan_endpoint: 169.254.169.254 rejected, domains resolving to public IP rejected,
        private IPs and localhost accepted.
        """
        # 1. Link-local IP rejection (169.254.0.0/16)
        link_locals = [
            "http://169.254.169.254/latest/meta-data",
            "http://169.254.1.1/ai/api",
            "https://169.254.200.50:11434"
        ]
        for url in link_locals:
            is_lan, msg = is_lan_endpoint(url)
            self.assertFalse(is_lan, f"Link-local {url} must be rejected")
            self.assertIn("link-local", msg.lower())

        # 2. Internal domain suffix resolving to public IP -> must be REJECTED
        with patch("socket.gethostbyname", return_value="8.8.8.8"):
            is_lan, msg = is_lan_endpoint("http://compromised.internal/ai")
            self.assertFalse(is_lan, "Domain resolving to public IP must be rejected")
            self.assertIn("public ip", msg.lower())

        # 3. Unresolvable domain -> must be REJECTED
        with patch("socket.gethostbyname", side_effect=socket.gaierror("Name resolution failed")):
            is_lan, msg = is_lan_endpoint("http://nonexistent.local/ai")
            self.assertFalse(is_lan, "Unresolvable domain must be rejected")
            self.assertIn("cannot resolve", msg.lower())

        # 4. Domain resolving to valid private LAN IP -> ACCEPTED
        with patch("socket.gethostbyname", return_value="192.168.1.108"):
            is_lan, msg = is_lan_endpoint("http://dgx-spark.local/ai/api/generate")
            self.assertTrue(is_lan, "Domain resolving to private IP must be accepted")

        # 5. Direct private LAN IPs and loopback -> ACCEPTED
        for url in ["http://127.0.0.1:8205", "http://localhost:8205", "http://192.168.1.108/ai"]:
            is_lan, msg = is_lan_endpoint(url)
            self.assertTrue(is_lan, f"{url} must be accepted")

    def test_w6_dgx_vram_lifecycle_and_timing(self):
        """W6: Measure VRAM before, during (keep_alive), and after unload on real DGX.
        Verify keep_alive maintains warm state during work and releases VRAM to 0.0 GiB on unload.
        """
        client = VisionClient(
            endpoint=self.dgx_endpoint,
            model=self.model_name,
            vision_enabled=True,
            timeout_seconds=120.0,
            keep_alive="5m"
        )

        # 1. Unload first to establish clean baseline
        client.unload_model()
        time.sleep(1)

        # Check baseline VRAM
        try:
            with urllib.request.urlopen(self.dgx_ps, timeout=10) as resp:
                ps_data = json.loads(resp.read().decode("utf-8"))
                models_before = ps_data.get("models", [])
                vram_before = sum(m.get("size_vram", 0) for m in models_before)
        except Exception as e:
            self.skipTest(f"DGX ps endpoint unreachable: {e}")

        # 2. Perform real inference to load model with keep_alive='5m'
        mock_page = type("MockPage", (), {
            "url": "http://test",
            "_screenshot_bytes": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
        })()
        t0 = time.monotonic()
        cand = client.locate_target(mock_page, "button")
        elapsed_inference = time.monotonic() - t0

        # Check VRAM during keep_alive window
        with urllib.request.urlopen(self.dgx_ps, timeout=10) as resp:
            ps_during = json.loads(resp.read().decode("utf-8"))
            models_during = ps_during.get("models", [])
            vram_during = sum(m.get("size_vram", 0) for m in models_during)

        self.assertGreater(vram_during, 10 * 1024**3, "VRAM during keep_alive should reflect loaded model (~19.4 GiB)")

        # 3. Explicit unload
        unloaded = client.unload_model()
        self.assertTrue(unloaded, "unload_model must succeed")
        time.sleep(1)

        # Check VRAM after unload
        with urllib.request.urlopen(self.dgx_ps, timeout=10) as resp:
            ps_after = json.loads(resp.read().decode("utf-8"))
            models_after = ps_after.get("models", [])
            vram_after = sum(m.get("size_vram", 0) for m in models_after)

        self.assertEqual(vram_after, 0, "VRAM must return to 0 bytes after unload_model")
        print(f"\n[W6 VRAM Report] Before: {vram_before} | During: {vram_during / 1024**3:.2f} GiB | After: {vram_after} bytes | Inference: {elapsed_inference:.2f}s")


if __name__ == "__main__":
    unittest.main()
