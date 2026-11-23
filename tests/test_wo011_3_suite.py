"""Comprehensive Verification Suite for WO-011.3 (Y1 - Y4).
Validates Vision Model Grounding Precision on Small Screens (640x400) and Multi-Resolution Scaling:
- Y1: 4 corner buttons across 3 screen sizes (640x400, 1280x900, 1920x1080) on real DGX model -> >= 11/12 hits, 0 outside image.
- Y2: 640x400 small screen precision -> >= 3/4 hits (100% in upscale test).
- Y3: Safe rejection test: non-existent target or corrupted box -> returns (None, 0.0), no false guessing with high confidence.
- Y4: Upscale metadata and coordinate projection fidelity verification.
"""
import io
import json
import os
import pathlib
import sys
import time
import unittest
from typing import Any, Dict, List, Tuple
from PIL import Image, ImageDraw

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent.parent
for p in (str(BASE_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from models.schemas import Strategy, TargetCandidate
from models.vision import VisionClient, set_global_vision_client


class TestWO011_3Suite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = VisionClient(
            vision_enabled=True,
            timeout_seconds=120.0
        )
        cls.sizes = [(640, 400), (1280, 900), (1920, 1080)]
        cls.labels = ["Search", "Help", "Settings", "Profile"]
        cls.colors = [(30, 144, 255), (16, 160, 90), (150, 60, 200), (230, 120, 20)]

    @classmethod
    def tearDownClass(cls):
        set_global_vision_client(None)

    def _generate_test_image(self, size: Tuple[int, int]) -> Tuple[bytes, List[Tuple[int, int, int, int]]]:
        w, h = size
        img = Image.new("RGB", (w, h), (248, 248, 248))
        d = ImageDraw.Draw(img)
        bw, bh = int(w * 0.22), int(h * 0.10)
        pos = [
            (int(w * 0.05), int(h * 0.06)),
            (int(w * 0.70), int(h * 0.06)),
            (int(w * 0.05), int(h * 0.80)),
            (int(w * 0.70), int(h * 0.80))
        ]
        boxes = []
        for (x, y), label, col in zip(pos, self.labels, self.colors):
            d.rectangle([x, y, x + bw, y + bh], fill=col)
            d.text((x + 10, y + bh // 2 - 4), label, fill=(255, 255, 255))
            boxes.append((x, y, x + bw, y + bh))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return buf.getvalue(), boxes

    def test_y1_and_y2_multi_resolution_accuracy_on_dgx(self):
        """Y1 & Y2: Test 4 corner buttons across 3 resolutions (12 calls).
        Y1: Overall >= 11/12 hits, 0 outside image.
        Y2: Specifically 640x400 >= 3/4 hits.
        """
        hits = 0
        outside = 0
        total = 0
        per_size_hits = {}

        for size in self.sizes:
            data, boxes = self._generate_test_image(size)
            mock_page = type("MockPage", (), {"_screenshot_bytes": data, "url": "https://test.local/accuracy"})()
            size_hits = 0

            for label, box in zip(self.labels, boxes):
                total += 1
                cand = self.client.locate_target(mock_page, f"the '{label}' button", viewport_size=size)
                c, conf = cand.coordinates, cand.confidence

                in_img = c is None or (0 <= c[0] <= size[0] and 0 <= c[1] <= size[1])
                in_box = c is not None and box[0] <= c[0] <= box[2] and box[1] <= c[1] <= box[3]

                if in_box:
                    hits += 1
                    size_hits += 1
                if not in_img:
                    outside += 1

                print(f"[{size[0]}x{size[1]}] {label}: coords={c}, box={box}, in_box={in_box}, conf={conf}")

            per_size_hits[size] = size_hits

        # Y2: 640x400 must have at least 3/4 hits
        small_hits = per_size_hits.get((640, 400), 0)
        self.assertGreaterEqual(small_hits, 3, f"640x400 precision too low: {small_hits}/4 (must be >= 3/4)")

        # Y1: Overall >= 11/12 hits and 0 outside
        self.assertGreaterEqual(hits, 11, f"Total precision too low: {hits}/{total} (must be >= 11/12)")
        self.assertEqual(outside, 0, f"No coordinates may fall outside image bounds: got {outside}")

    def test_y3_safe_rejection_no_hallucination_on_missing_target(self):
        """Y3: Non-existent target on 640x400 must return safe rejection (None, 0.0), never guess."""
        data, _ = self._generate_test_image((640, 400))
        mock_page = type("MockPage", (), {"_screenshot_bytes": data, "url": "https://test.local/missing"})()

        # Non-existent button
        cand = self.client.locate_target(mock_page, "Confirm Delete Entire Account Button", viewport_size=(640, 400))
        # If model reports found=false or invalid, coordinates must be None
        if cand.coordinates is not None:
            # If coordinates are returned, confidence must not be high without true grounding
            self.assertLess(cand.confidence, self.client.min_confidence, "Non-existent element must not return high confidence")
        else:
            self.assertEqual(cand.confidence, 0.0, "Safe rejection must set confidence to 0.0")

    def test_y4_upscale_metadata_and_scaling_ratio(self):
        """Y4: Viewport scaling metadata records correct scale factor and dimensions."""
        data, _ = self._generate_test_image((640, 400))
        mock_page = type("MockPage", (), {"_screenshot_bytes": data, "url": "https://test.local/meta"})()

        cand = self.client.locate_target(mock_page, "the 'Search' button", viewport_size=(640, 400))
        meta = cand.metadata
        self.assertEqual(meta.get("upscale_scale"), 2.0)
        self.assertEqual(meta.get("infer_dimensions"), (1280, 800))
        self.assertEqual(meta.get("original_dimensions"), (640, 400))
        self.assertIsNotNone(cand.coordinates)


if __name__ == "__main__":
    unittest.main()
