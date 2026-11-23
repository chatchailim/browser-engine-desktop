"""Comprehensive Verification Suite for WO-011.2 (X1 - X7).
Validates coordinate precision, boundary enforcement, and pre-click re-verification on DGX Spark:
- X1: Real on-premise DGX call on 4 buttons across 3 sizes (12 calls, >= 10/12 in button, 0 outside)
- X2: Out-of-bounds box [900, 800, 1000, 900] on 640x400 -> returns (None, 0.0)
- X3: Negative coordinates [-50, -40, 20, 20] -> returns (None, 0.0)
- X4: Abnormal boxes (> 90% or < 4px) -> returns (None, 0.0)
- X5: Auto-detection and conversion of normalized 0..1000 coordinates (simulated + real DGX case)
- X6: Re-verification mismatch on fixture -> returns no_grounded_target, 0 clicks
- X7: Regression check: WO-009 (16), WO-011 (9), WO-011.1 (6) suites all pass
"""
import io
import json
import os
import pathlib
import sys
import time
import unittest
import urllib.request
from typing import Any, Dict, List, Tuple
from unittest.mock import patch
from PIL import Image, ImageDraw

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from playwright.sync_api import sync_playwright

from models.schemas import ActionStep, ActionType, MacroDefinition, RiskLevel, Strategy, TargetCandidate
from models.vision import VisionClient, set_global_vision_client
from execution.coordinates import CoordinateManager
from execution.macro import ConditionalMacroRunner
from tests.fixtures_server import run_fixture_server, FixtureServer


class TestWO011_2Suite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_server: FixtureServer = run_fixture_server()
        cls.fixture_port = cls.fixture_server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.fixture_port}"
        cls.dgx_endpoint = "http://192.168.1.108/ai/api/generate"
        cls.model_name = "qwen2.5vl:7b"
        print(f"[*] WO-011.2 Fixture Server started at {cls.base_url}")

    @classmethod
    def tearDownClass(cls):
        set_global_vision_client(None)
        cls.fixture_server.shutdown()

    def test_x1_real_dgx_4_buttons_3_sizes(self):
        """X1: Real DGX call on 4 buttons across 3 sizes (12 calls).
        At least 10 of 12 calls must land inside the actual button box.
        Zero calls may land outside the image bounds.
        """
        client = VisionClient(
            endpoint=self.dgx_endpoint,
            model=self.model_name,
            vision_enabled=True,
            timeout_seconds=120.0,
            backend_override=None
        )

        sizes = [(640, 400), (1280, 900), (1920, 1080)]
        total_calls = 0
        inside_count = 0
        outside_image_count = 0

        for w, h in sizes:
            buttons = [
                ("top-left button", [int(0.08 * w), int(0.10 * h), int(0.35 * w), int(0.25 * h)]),
                ("top-right button", [int(0.65 * w), int(0.10 * h), int(0.92 * w), int(0.25 * h)]),
                ("bottom-left button", [int(0.08 * w), int(0.75 * h), int(0.35 * w), int(0.90 * h)]),
                ("bottom-right button", [int(0.65 * w), int(0.75 * h), int(0.92 * w), int(0.90 * h)])
            ]

            for name, box in buttons:
                total_calls += 1
                img = Image.new("RGB", (w, h), color=(240, 240, 240))
                d = ImageDraw.Draw(img)
                d.rectangle(box, fill=(50, 120, 220), outline=(20, 20, 20), width=2)
                d.text((box[0] + 10, box[1] + 10), name, fill=(255, 255, 255))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                img_bytes = buf.getvalue()

                mock_page = type("MockPage", (), {
                    "url": "http://test-x1",
                    "_screenshot_bytes": img_bytes
                })()

                cand = client.locate_target(mock_page, name, viewport_size=(w, h))
                coords = cand.coordinates
                self.assertIsNotNone(coords, f"[{w}x{h}] {name} must return coordinates")
                px, py = coords

                # Check if outside image
                is_outside_image = (px < 0 or px > w or py < 0 or py > h)
                if is_outside_image:
                    outside_image_count += 1

                # Check if inside button (with small tolerance for edge aliasing)
                bx1, by1, bx2, by2 = box
                is_inside = (bx1 - 5 <= px <= bx2 + 5 and by1 - 5 <= py <= by2 + 5)
                if is_inside:
                    inside_count += 1

                print(f"X1 Test [{w}x{h}] {name}: coords=({px}, {py}), actual_box={box}, inside={is_inside}")

        self.assertEqual(outside_image_count, 0, f"Zero calls may be outside image, got {outside_image_count}")
        self.assertGreaterEqual(inside_count, 10, f"At least 10/12 must land inside button, got {inside_count}/12")

    def test_x2_box_outside_image_returns_none(self):
        """X2: Out-of-bounds box [900, 800, 1000, 900] on 640x400 -> must return (None, 0.0)."""
        client = VisionClient()
        raw_json = '{"found": true, "box": [900, 800, 1000, 900], "confidence": 0.9}'
        res, conf = client._parse_vlm_output(raw_json, 640, 400)
        self.assertIsNone(res, "Out-of-bounds box must return None")
        self.assertEqual(conf, 0.0, "Out-of-bounds box must yield 0.0 confidence")

    def test_x3_negative_coordinates_returns_none(self):
        """X3: Negative coordinates [-50, -40, 20, 20] -> must return (None, 0.0)."""
        client = VisionClient()
        # Case A: JSON box
        res, conf = client._parse_vlm_output('{"found": true, "box": [-50, -40, 20, 20], "confidence": 0.9}', 640, 400)
        self.assertIsNone(res, "Negative coordinates in JSON must return None")
        self.assertEqual(conf, 0.0)

        # Case B: Regex fallback
        res_rgx, conf_rgx = client._parse_vlm_output('Target at [-50, -40, 20, 20] with confidence 0.9', 640, 400)
        self.assertIsNone(res_rgx, "Negative coordinates in text must return None")
        self.assertEqual(conf_rgx, 0.0)

    def test_x4_abnormal_boxes_returns_none(self):
        """X4: Abnormal boxes (> 90% of image or < 4px) -> must return (None, 0.0)."""
        client = VisionClient()
        # Case A: Box width > 90% of image (600 / 640 = 93.75%)
        raw_large = '{"found": true, "box": [10, 50, 610, 150], "confidence": 0.9}'
        res_large, conf_large = client._parse_vlm_output(raw_large, 640, 400)
        self.assertIsNone(res_large, "Box exceeding 90% width must return None")
        self.assertEqual(conf_large, 0.0)

        # Case B: Box height > 90% of image (380 / 400 = 95%)
        raw_tall = '{"found": true, "box": [50, 10, 150, 390], "confidence": 0.9}'
        res_tall, conf_tall = client._parse_vlm_output(raw_tall, 640, 400)
        self.assertIsNone(res_tall, "Box exceeding 90% height must return None")
        self.assertEqual(conf_tall, 0.0)

        # Case C: Box < 4px width (width = 2)
        raw_narrow = '{"found": true, "box": [100, 100, 102, 150], "confidence": 0.9}'
        res_narrow, conf_narrow = client._parse_vlm_output(raw_narrow, 640, 400)
        self.assertIsNone(res_narrow, "Box under 4px width must return None")
        self.assertEqual(conf_narrow, 0.0)

        # Case D: Box < 4px height (height = 2)
        raw_short = '{"found": true, "box": [100, 100, 150, 102], "confidence": 0.9}'
        res_short, conf_short = client._parse_vlm_output(raw_short, 640, 400)
        self.assertIsNone(res_short, "Box under 4px height must return None")
        self.assertEqual(conf_short, 0.0)

    def test_x5_normalized_0_to_1000_conversion(self):
        """X5: Model responds in normalized 0..1000 -> auto detects and converts to pixels.
        Includes simulated case and real response case from DGX.
        """
        client = VisionClient()
        # Simulated Case: unit explicitly declared as normalized_1000
        sim_json = '{"found": true, "box": [100, 150, 400, 275], "unit": "normalized_1000", "confidence": 0.9}'
        res, conf = client._parse_vlm_output(sim_json, 640, 400)
        # Expected: x = (100+400)/2 * 640 / 1000 = 160, y = (150+275)/2 * 400 / 1000 = 85
        self.assertEqual(res, (160, 85))
        self.assertEqual(conf, 0.9)

        # Real DGX Case from R1 experiment on 640x400 top-left button (actual button at [51, 40, 224, 100])
        real_json = '```json\n{"found": true, "box": [91, 77, 403, 186], "unit": "normalized_1000", "confidence": 0.9}\n```'
        res_real, conf_real = client._parse_vlm_output(real_real:=real_json, 640, 400)
        self.assertIsNotNone(res_real)
        px, py = res_real
        # Converted center: x = (91+403)/2 * 0.64 = 158, y = (77+186)/2 * 0.4 = 52.6 -> 53
        self.assertIn(px, range(150, 165))
        self.assertIn(py, range(50, 56))
        self.assertEqual(conf_real, 0.9)

        # Telemetry verification (R5)
        meta = client.last_parse_meta
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("coordinate_unit_detected"), "normalized_1000")
        self.assertEqual(meta.get("conversion_method"), "scaled_from_1000_to_viewport")
        self.assertTrue(meta.get("boundary_check_passed"))

    def test_x6_re_verification_mismatch_aborts_click(self):
        """X6: Pre-click element re-verification (R4).
        When vision returns coordinates pointing to an element that doesn't match target description,
        system aborts with no_grounded_target and 0 clicks executed.
        """
        initial_clicks = self.fixture_server.visual_clicks

        # Target candidate with coordinates pointing to empty area (500, 500)
        cand = TargetCandidate(
            strategy=Strategy.VISION,
            confidence=0.9,
            coordinates=(500, 500),
            name="Non-Existent Submit Button"
        )
        macro = MacroDefinition(
            macro_id="macro-x6",
            steps=[ActionStep(action=ActionType.CLICK, target=cand)]
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"{self.base_url}/visual_button")
            page.wait_for_timeout(300)

            runner = ConditionalMacroRunner(
                adapter=type("MockBrowserAdapter", (), {
                    "click": lambda self, x, y, selector=None: page.mouse.click(x, y) or True
                })()
            )

            res = runner.run_macro(macro, page)
            browser.close()

        # Verify execution was aborted without clicking
        self.assertFalse(res["ok"], "Macro must fail due to element mismatch")
        self.assertEqual(res["error"], "no_grounded_target")
        self.assertEqual(self.fixture_server.visual_clicks, initial_clicks, "Clicks must be strictly 0")

    def test_x7_regression_check_all_suites_pass(self):
        """X7: Regression verification that existing suites pass:
        test_wo009_suite (16), test_wo011_suite (9), test_wo011_1_suite (6)
        """
        import tests.test_wo009_suite as wo009
        import tests.test_wo011_suite as wo011
        import tests.test_wo011_1_suite as wo011_1

        loader = unittest.TestLoader()
        suite = unittest.TestSuite()
        suite.addTests(loader.loadTestsFromModule(wo009))
        suite.addTests(loader.loadTestsFromModule(wo011))
        suite.addTests(loader.loadTestsFromModule(wo011_1))

        runner = unittest.TextTestRunner(verbosity=0)
        result = runner.run(suite)
        self.assertEqual(len(result.errors), 0, f"Errors in regression: {result.errors}")
        self.assertEqual(len(result.failures), 0, f"Failures in regression: {result.failures}")
        self.assertGreaterEqual(result.testsRun, 31, f"Expected at least 31 tests, ran {result.testsRun}")


if __name__ == "__main__":
    unittest.main()
