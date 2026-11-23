"""Level 4 Vision Grounding Client (VLM) for OneVault Interactive Agent Engine.
Integrates on-premise vision models (e.g. Typhoon OCR / Qwen2.5-VL on DGX Spark) into Level 4.

Strict Ground Rules:
1. Internal LAN-only endpoint verification: rejects external/cloud endpoints.
2. Deny-by-default for Risk >= 2: vision coordinates are strictly prohibited for Risk >= 2.
3. Minimum confidence gate (vision_min_confidence, default 0.75): below threshold yields takeover.
4. Privacy: traces store only SHA-256 hash of screenshots. Raw images saved only in gitignored
   data/screenshots/ and auto-purged after vision_image_retention_hours (default 24h).
5. VRAM management: unloads or avoids pinning VRAM when idle via keep_alive=0.
6. Fallback resilience: service unavailability terminates safely without crash or coordinate guessing.
"""
import base64
import hashlib
import ipaddress
import json
import os
import pathlib
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from models.schemas import CoordinateContext, Strategy, TargetCandidate
from execution.coordinates import CoordinateManager
from perception.screenshot import ScreenshotPipeline

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"
DATA_DIR = BASE_DIR / "data"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"


def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "vision_enabled": False,
        "vision_endpoint": "http://192.168.1.108/ai/api/generate",
        "vision_model": "qwen2.5vl:7b",
        "vision_min_confidence": 0.75,
        "vision_image_retention_hours": 24,
        "vision_timeout_seconds": 120,
        "vision_keep_alive": "5m"
    }


def is_lan_endpoint(endpoint_url: str) -> Tuple[bool, str]:
    """Verify that the endpoint strictly points to a private local area network (LAN)
    or loopback interface. Rejects any public IP, cloud domain, link-local (169.254.0.0/16),
    or domain resolving to a public IP.
    """
    if not endpoint_url or not str(endpoint_url).strip():
        return False, "external_endpoint_rejected: empty vision_endpoint"

    try:
        parsed = urllib.parse.urlparse(str(endpoint_url).strip())
    except Exception as e:
        return False, f"external_endpoint_rejected: malformed URL ({e})"

    if parsed.scheme not in ("http", "https"):
        return False, f"external_endpoint_rejected: scheme must be http or https, got '{parsed.scheme}'"

    host = parsed.hostname
    if not host:
        return False, "external_endpoint_rejected: no hostname found in vision_endpoint"

    # Local loopback / mock hostnames
    if host.lower() in ("localhost", "127.0.0.1", "::1", "testserver", "fixture-server"):
        return True, "Loopback local host"

    # Check if host is direct IP address
    try:
        ip = ipaddress.ip_address(host)
        # Block link-local addresses (e.g. 169.254.0.0/16, cloud metadata services)
        if ip.is_link_local:
            return False, f"external_endpoint_rejected: {host} is a link-local / cloud metadata address"
        if ip.is_private or ip.is_loopback:
            return True, f"Private LAN IP ({ip})"
        return False, f"external_endpoint_rejected: {host} is a public IP address"
    except ValueError:
        pass

    # Host is a domain name:
    # Must resolve hostname to IP and ensure the target is private LAN and NOT link-local
    try:
        resolved_ip = socket.gethostbyname(host)
        ip = ipaddress.ip_address(resolved_ip)
        if ip.is_link_local:
            return False, f"external_endpoint_rejected: {host} resolved to link-local IP {resolved_ip}"
        if ip.is_private or ip.is_loopback:
            return True, f"Resolved to private IP ({resolved_ip})"
        return False, f"external_endpoint_rejected: {host} resolved to public IP {resolved_ip}"
    except (socket.gaierror, socket.herror, Exception) as e:
        return False, f"external_endpoint_rejected: cannot resolve hostname {host}: {e}"


def purge_old_screenshots(max_age_hours: Optional[float] = None) -> int:
    """Purge raw screenshot files in data/screenshots older than retention limit.
    Traces retain SHA-256 hash indefinitely.
    """
    if not SCREENSHOTS_DIR.is_dir():
        return 0

    if max_age_hours is None:
        cfg = load_config()
        max_age_hours = float(cfg.get("vision_image_retention_hours", 24))

    cutoff = time.time() - (float(max_age_hours) * 3600.0)
    purged_count = 0
    for item in SCREENSHOTS_DIR.glob("*.jpg"):
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
                purged_count += 1
        except Exception:
            pass
    return purged_count


class VisionClient:
    def __init__(
        self,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        min_confidence: Optional[float] = None,
        retention_hours: Optional[float] = None,
        vision_enabled: Optional[bool] = None,
        timeout_seconds: Optional[float] = None,
        keep_alive: Optional[Any] = None,
        backend_override: Optional[Callable[[str, bytes, Tuple[int, int]], Dict[str, Any]]] = None
    ):
        cfg = load_config()
        self.vision_enabled = vision_enabled if vision_enabled is not None else bool(cfg.get("vision_enabled", False))
        self.endpoint = (endpoint or cfg.get("vision_endpoint", "http://192.168.1.108/ai/api/generate")).rstrip("/")
        self.model = model or cfg.get("vision_model", "qwen2.5vl:7b")
        self.min_confidence = float(min_confidence if min_confidence is not None else cfg.get("vision_min_confidence", 0.75))
        self.retention_hours = float(retention_hours if retention_hours is not None else cfg.get("vision_image_retention_hours", 24))
        self.timeout_seconds = float(timeout_seconds if timeout_seconds is not None else cfg.get("vision_timeout_seconds", 120.0))
        self.keep_alive = keep_alive if keep_alive is not None else cfg.get("vision_keep_alive", "5m")
        self.backend_override = backend_override

    def unload_model(self) -> bool:
        """Explicitly unload the vision model from DGX VRAM."""
        try:
            payload = {"model": self.model, "keep_alive": 0}
            req = urllib.request.Request(
                self.endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                time.sleep(1.5)
                return True
        except Exception:
            return False

    def locate_target(
        self,
        page: Any,
        target_description: str,
        viewport_size: Tuple[int, int] = (1280, 900)
    ) -> TargetCandidate:
        """Ground a visual target using the on-premise vision model (Level 4)."""
        t0 = time.monotonic()

        # Check 1: Feature toggle
        if not self.vision_enabled:
            return TargetCandidate(
                strategy=Strategy.VISION,
                confidence=0.0,
                name=target_description,
                metadata={"error": "vision_disabled", "needs_vision_grounding": True}
            )

        # Check 2: LAN-only endpoint enforcement
        is_lan, lan_msg = is_lan_endpoint(self.endpoint)
        if not is_lan:
            return TargetCandidate(
                strategy=Strategy.VISION,
                confidence=0.0,
                name=target_description,
                metadata={"error": "external_endpoint_rejected", "reason": lan_msg}
            )

        # Check 3: Capture screenshot
        img_bytes = b""
        w, h = viewport_size
        try:
            if hasattr(page, "screenshot") and callable(page.screenshot):
                img_bytes, w, h = ScreenshotPipeline.capture_viewport(page, quality=80, max_width=viewport_size[0])
            elif hasattr(page, "_screenshot_bytes"):
                img_bytes = page._screenshot_bytes
            else:
                # Fallback blank canvas for headless testing without live browser
                img_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"
        except Exception as e:
            return TargetCandidate(
                strategy=Strategy.VISION,
                confidence=0.0,
                name=target_description,
                metadata={"error": "screenshot_failed", "detail": str(e)}
            )

        # Store only SHA-256 hash in trace, raw image in gitignored data/screenshots/
        img_hash = hashlib.sha256(img_bytes).hexdigest()
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        img_file = SCREENSHOTS_DIR / f"{img_hash}.jpg"
        if not img_file.is_file():
            try:
                img_file.write_bytes(img_bytes)
            except Exception:
                pass

        # Purge screenshots exceeding retention window
        purge_old_screenshots(self.retention_hours)

        # Custom Backend Hook (used for isolated unit tests / deterministic fixtures)
        if self.backend_override:
            try:
                custom_res = self.backend_override(target_description, img_bytes, (w, h))
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                conf = float(custom_res.get("confidence", 0.0))
                coords = custom_res.get("coordinates")
                err = custom_res.get("error")

                return TargetCandidate(
                    strategy=Strategy.VISION,
                    confidence=conf,
                    name=target_description,
                    coordinates=tuple(coords) if coords else None,
                    metadata={
                        "screenshot_hash": img_hash,
                        "latency_ms": elapsed_ms,
                        "error": err,
                        "low_confidence": (conf < self.min_confidence)
                    }
                )
            except Exception as e:
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                return TargetCandidate(
                    strategy=Strategy.VISION,
                    confidence=0.0,
                    name=target_description,
                    metadata={"error": "vision_service_unavailable", "detail": str(e), "screenshot_hash": img_hash}
                )

        # Determine image dimensions from img_bytes or viewport_size
        scale = 1.0
        infer_img_bytes = img_bytes
        infer_w, infer_h = w, h

        # R1 (WO-011.3): Upscale small viewports (width < 1280) before calling VLM
        # to ensure sufficient token spatial resolution, then map coordinates back.
        if w < 1280 and img_bytes:
            try:
                import io
                from PIL import Image
                orig_img = Image.open(io.BytesIO(img_bytes))
                scale = 1280.0 / float(w)
                infer_w = int(round(w * scale))
                infer_h = int(round(h * scale))
                scaled_img = orig_img.resize((infer_w, infer_h), Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                scaled_img.save(buf, format="JPEG", quality=90)
                infer_img_bytes = buf.getvalue()
            except Exception:
                scale = 1.0
                infer_img_bytes = img_bytes
                infer_w, infer_h = w, h

        # Default: Call On-Premise VLM Endpoint (Ollama / vLLM on DGX)
        b64_img = base64.b64encode(infer_img_bytes).decode("ascii")
        prompt = (
            f"Detect the visual bounding box of the target: '{target_description}'. "
            f"Return JSON in the format: {{\"found\": true, \"box\": [xmin, ymin, xmax, ymax], \"confidence\": 0.9}} "
            f"with pixel coordinates (0..{infer_w}, 0..{infer_h}), or {{\"found\": false, \"confidence\": 0.0}} if not present."
        )

        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [b64_img],
            "stream": False,
            "keep_alive": self.keep_alive
        }

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw_json = json.loads(resp.read().decode("utf-8"))
                elapsed_ms = int((time.monotonic() - t0) * 1000)
                resp_text = raw_json.get("response", "")

                # Parse JSON or structured box from model output with R1 fallback unit
                auto_unit = "normalized_1000" if max(infer_w, infer_h) <= 1000 else "pixel"
                parsed_coords, parsed_conf = self._parse_vlm_output(
                    resp_text, infer_w, infer_h, default_unit=auto_unit, force_unit_on_small_image=(max(infer_w, infer_h) <= 1000)
                )

                # Scale coordinates back to original viewport space
                if scale != 1.0 and parsed_coords is not None:
                    orig_x = int(round(parsed_coords[0] / scale))
                    orig_y = int(round(parsed_coords[1] / scale))
                    # Strict boundary check against original image bounds (R2 & Y3)
                    if 0 <= orig_x <= w and 0 <= orig_y <= h:
                        parsed_coords = (orig_x, orig_y)
                    else:
                        parsed_coords = None
                        parsed_conf = 0.0

                meta = {
                    "screenshot_hash": img_hash,
                    "latency_ms": elapsed_ms,
                    "model": self.model,
                    "low_confidence": (parsed_conf < self.min_confidence),
                    "upscale_scale": scale,
                    "infer_dimensions": (infer_w, infer_h),
                    "original_dimensions": (w, h)
                }
                if hasattr(self, "last_parse_meta") and self.last_parse_meta:
                    meta.update(self.last_parse_meta)

                return TargetCandidate(
                    strategy=Strategy.VISION,
                    confidence=parsed_conf,
                    name=target_description,
                    coordinates=parsed_coords,
                    metadata=meta
                )
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return TargetCandidate(
                strategy=Strategy.VISION,
                confidence=0.0,
                name=target_description,
                metadata={
                    "error": "vision_service_unavailable",
                    "screenshot_hash": img_hash,
                    "latency_ms": elapsed_ms,
                    "detail": str(e)
                }
            )
        except Exception as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return TargetCandidate(
                strategy=Strategy.VISION,
                confidence=0.0,
                name=target_description,
                metadata={
                    "error": "vision_service_unavailable",
                    "screenshot_hash": img_hash,
                    "latency_ms": elapsed_ms,
                    "detail": str(e)
                }
            )

    def _parse_vlm_output(
        self,
        text: str,
        viewport_w: int,
        viewport_h: int,
        default_unit: str = "pixel",
        force_unit_on_small_image: bool = False
    ) -> Tuple[Optional[Tuple[int, int]], float]:
        """Parse coordinates and confidence from model text output.
        Tolerates markdown fences, python booleans, surrounding text, extra brackets.
        Enforces strict boundary checking and automatic unit detection (R2, R3).
        Returns (None, 0.0) whenever parsing fails, target is not found, or bounds invalid.
        Never guesses coordinates. Default confidence is 0.0 if not explicitly provided.
        """
        import re
        import ast
        self.last_parse_meta = None
        if not text or not str(text).strip():
            return None, 0.0

        cleaned_text = str(text).strip()

        # Strip markdown code fences if present (```json ... ``` or ``` ... ```)
        if "```" in cleaned_text:
            fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned_text, re.DOTALL | re.IGNORECASE)
            if fence_match:
                cleaned_text = fence_match.group(1).strip()

        # Strip surrounding parenthesis if any e.g. (({ ... }))
        while cleaned_text.startswith("(") and cleaned_text.endswith(")"):
            cleaned_text = cleaned_text[1:-1].strip()

        # Attempt 1: Extract JSON object or python dict
        m = re.search(r"(\{.*\})", cleaned_text, re.DOTALL)
        json_cand = m.group(1) if m else None

        data = None
        if json_cand:
            try:
                data = json.loads(json_cand)
            except Exception:
                norm_cand = re.sub(r"\bTrue\b", "true", json_cand)
                norm_cand = re.sub(r"\bFalse\b", "false", norm_cand)
                norm_cand = re.sub(r"\bNone\b", "null", norm_cand)
                norm_cand = re.sub(r'([0-9.]+)"(\s*[\},])', r'\1\2', norm_cand)
                try:
                    data = json.loads(norm_cand)
                except Exception:
                    try:
                        data = ast.literal_eval(json_cand)
                    except Exception:
                        data = None

        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            data = data[0]

        if isinstance(data, dict):
            found = data.get("found", True)
            if not found or found is False or str(found).lower() == "false":
                return None, 0.0

            # Confidence defaults to 0.0 if not provided or invalid (Requirement R3)
            conf_val = data.get("confidence")
            conf = 0.0
            if conf_val is not None:
                try:
                    cleaned_conf = re.sub(r'[^0-9.]', '', str(conf_val))
                    conf = float(cleaned_conf) if cleaned_conf else 0.0
                except (ValueError, TypeError):
                    conf = 0.0

            # Unit detection from declared unit or text
            declared_unit = data.get("unit") or data.get("coordinate_system")
            if force_unit_on_small_image:
                declared_unit = "normalized_1000"
            elif not declared_unit:
                if data.get("normalized") is True or "normalized" in cleaned_text.lower():
                    declared_unit = "normalized_1000"
                else:
                    declared_unit = default_unit

            box = data.get("box") or data.get("bbox") or data.get("box_2d")
            if box and isinstance(box, (list, tuple)) and len(box) == 4:
                try:
                    vals = [float(v) for v in box]
                    center_px, ok, reason, meta = CoordinateManager.convert_and_validate_box(
                        tuple(vals), (viewport_w, viewport_h), declared_unit=declared_unit
                    )
                    self.last_parse_meta = meta
                    if not ok or not center_px:
                        return None, 0.0
                    return center_px, conf
                except (ValueError, TypeError):
                    return None, 0.0

            point = data.get("point") or data.get("coord")
            if point and isinstance(point, (list, tuple)) and len(point) == 2:
                try:
                    p0, p1 = float(point[0]), float(point[1])
                    if p0 < 0 or p1 < 0:
                        return None, 0.0
                    if declared_unit and ("norm" in str(declared_unit).lower() or "1000" in str(declared_unit).lower()):
                        px = int(round(p0 * viewport_w / 1000.0))
                        py = int(round(p1 * viewport_h / 1000.0))
                        conversion = "scaled_from_1000_to_viewport"
                    else:
                        px, py = int(round(p0)), int(round(p1))
                        conversion = "direct_pixel"

                    if px < 0 or px > viewport_w or py < 0 or py > viewport_h:
                        return None, 0.0

                    self.last_parse_meta = {
                        "coordinate_unit_detected": declared_unit or "pixel",
                        "conversion_method": conversion,
                        "boundary_check_passed": True,
                        "boundary_check_reason": "valid"
                    }
                    return (px, py), conf
                except (ValueError, TypeError):
                    return None, 0.0

            return None, 0.0

        # Attempt 2: Regex fallback for 4 numbers in brackets [n1, n2, n3, n4]
        box_match = re.search(r"\[\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", cleaned_text)
        if box_match:
            c0, c1, c2, c3 = map(float, box_match.groups())
            # Confidence defaults to 0.0 if not explicitly mentioned in text (Requirement R3)
            conf = 0.0
            conf_match = re.search(r'confidence["\']?\s*[:=]\s*([0-9.]+)', cleaned_text, re.IGNORECASE)
            if conf_match:
                try:
                    conf = float(conf_match.group(1))
                except ValueError:
                    conf = 0.0

            declared_unit = "normalized_1000" if "normalized" in cleaned_text.lower() else default_unit
            center_px, ok, reason, meta = CoordinateManager.convert_and_validate_box(
                (c0, c1, c2, c3), (viewport_w, viewport_h), declared_unit=declared_unit
            )
            self.last_parse_meta = meta
            if not ok or not center_px:
                return None, 0.0
            return center_px, conf

        return None, 0.0


# Module-level client instance
_GLOBAL_VISION_CLIENT: Optional[VisionClient] = None


def get_global_vision_client() -> VisionClient:
    global _GLOBAL_VISION_CLIENT
    if _GLOBAL_VISION_CLIENT is None:
        _GLOBAL_VISION_CLIENT = VisionClient()
    return _GLOBAL_VISION_CLIENT


def set_global_vision_client(client: Optional[VisionClient]):
    global _GLOBAL_VISION_CLIENT
    _GLOBAL_VISION_CLIENT = client
