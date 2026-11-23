"""High-speed Screenshot Pipeline with ROI (Region of Interest) Cropping.
Produces optimized WebP / JPEG images for low-latency VLM perception.
"""
import base64
import io
from typing import Any, Optional, Tuple
from PIL import Image


class ScreenshotPipeline:
    @staticmethod
    def capture_viewport(page: Any, quality: int = 80, max_width: int = 1280) -> Tuple[bytes, int, int]:
        """Capture viewport screenshot as compressed JPEG and downscale if wider than max_width."""
        raw_bytes = page.screenshot(type="jpeg", quality=quality)
        img = Image.open(io.BytesIO(raw_bytes))
        w, h = img.size

        if w > max_width:
            scale = max_width / float(w)
            new_h = int(h * scale)
            img = img.resize((max_width, new_h), Image.Resampling.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            return buf.getvalue(), max_width, new_h

        return raw_bytes, w, h

    @staticmethod
    def crop_roi(image_bytes: bytes, x: int, y: int, width: int, height: int) -> Tuple[bytes, Tuple[int, int]]:
        """Crop a specific Region of Interest from the screenshot."""
        img = Image.open(io.BytesIO(image_bytes))
        img_w, img_h = img.size

        # Clamp boundaries
        x1 = max(0, min(img_w - 1, x))
        y1 = max(0, min(img_h - 1, y))
        x2 = max(x1 + 1, min(img_w, x + width))
        y2 = max(y1 + 1, min(img_h, y + height))

        cropped = img.crop((x1, y1, x2, y2))
        buf = io.BytesIO()
        cropped.save(buf, format="JPEG", quality=85)
        return buf.getvalue(), (x1, y1)

    @staticmethod
    def to_data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
        """Convert image bytes to base64 data URL."""
        b64 = base64.b64encode(image_bytes).decode("ascii")
        return f"data:{mime};base64,{b64}"
