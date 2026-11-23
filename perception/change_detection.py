"""Change Detection and Screen Hash Cache.
Computes perceptual hashes and diff bounding boxes to skip redundant VLM inference.
"""
import hashlib
import io
from typing import Optional, Tuple
from PIL import Image, ImageChops


class ChangeDetector:
    @staticmethod
    def compute_screen_hash(image_bytes: bytes) -> str:
        """Compute fast perceptual hash over a downscaled 64x64 grayscale image."""
        img = Image.open(io.BytesIO(image_bytes)).convert("L").resize((64, 64))
        return hashlib.md5(img.tobytes()).hexdigest()

    @staticmethod
    def detect_changed_region(img_bytes_a: bytes, img_bytes_b: bytes, threshold: int = 15) -> Optional[Tuple[int, int, int, int]]:
        """Identify bounding box (x, y, width, height) of visual differences between two frames."""
        try:
            img_a = Image.open(io.BytesIO(img_bytes_a)).convert("RGB")
            img_b = Image.open(io.BytesIO(img_bytes_b)).convert("RGB")

            if img_a.size != img_b.size:
                img_b = img_b.resize(img_a.size)

            diff = ImageChops.difference(img_a, img_b)
            # Thresholding
            diff = diff.point(lambda p: 255 if p > threshold else 0).convert("L")
            bbox = diff.getbbox()  # (left, upper, right, lower)
            if not bbox:
                return None  # No significant change

            x1, y1, x2, y2 = bbox
            return (x1, y1, x2 - x1, y2 - y1)
        except Exception:
            return None
