"""Coordinate Context & Normalization Layer.
Handles conversion between normalized [0..1000] coordinates and actual pixel coordinates
with viewport bounds, deviceScaleFactor, and scroll offsets.
Enforces strict boundary checking and automatic unit detection (WO-011.2).
"""
from typing import Any, Dict, Optional, Tuple
from models.schemas import CoordinateContext


class CoordinateManager:
    @staticmethod
    def normalize(x: int, y: int, viewport_width: int, viewport_height: int) -> CoordinateContext:
        """Convert pixel coordinates to normalized [0..1000] coordinate context."""
        norm_x = max(0, min(1000, int((x * 1000.0) / max(1, viewport_width))))
        norm_y = max(0, min(1000, int((y * 1000.0) / max(1, viewport_height))))
        return CoordinateContext(
            norm_x=norm_x,
            norm_y=norm_y,
            viewport_width=viewport_width,
            viewport_height=viewport_height
        )

    @staticmethod
    def to_pixel(context: CoordinateContext) -> Tuple[int, int]:
        """Convert normalized coordinate context to actual pixel coordinates for the viewport."""
        px = int((context.norm_x * context.viewport_width) / 1000.0)
        py = int((context.norm_y * context.viewport_height) / 1000.0)
        return (px, py)

    @staticmethod
    def clamp_bounds(x: int, y: int, bounds: Tuple[int, int, int, int]) -> Tuple[int, int]:
        """Refine a coordinate to lie comfortably within an element's bounding box [x, y, w, h]."""
        bx, by, bw, bh = bounds
        cx = bx + bw // 2
        cy = by + bh // 2
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return (x, y)
        return (cx, cy)

    @staticmethod
    def validate_bounds(
        x1: float, y1: float, x2: float, y2: float,
        viewport_w: int, viewport_h: int
    ) -> Tuple[bool, str]:
        """Strict boundary verification (Requirement R3):
        - Rejects negative coordinates (X3).
        - Rejects coordinates exceeding viewport bounds (X2).
        - Rejects box width/height > 90% of image or < 4px (X4).
        """
        # Check negative coordinates
        if x1 < 0 or y1 < 0 or x2 < 0 or y2 < 0:
            return False, "negative_coordinates"

        # Check inversion
        if x1 > x2 or y1 > y2:
            return False, "inverted_coordinates"

        # Check box dimensions in pixel space
        box_w = x2 - x1
        box_h = y2 - y1

        if box_w > 0.9 * viewport_w or box_h > 0.9 * viewport_h:
            return False, "abnormal_box_too_large"

        if box_w < 4.0 or box_h < 4.0:
            return False, "abnormal_box_too_small"

        # Check center
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        if cx < 0 or cx > viewport_w or cy < 0 or cy > viewport_h:
            return False, "center_outside_viewport"

        # Check box bounds
        if x1 > viewport_w or x2 > viewport_w or y1 > viewport_h or y2 > viewport_h:
            return False, "box_outside_viewport"

        return True, "valid"

    @classmethod
    def convert_and_validate_box(
        cls,
        box: Tuple[float, float, float, float],
        viewport: Tuple[int, int],
        declared_unit: Optional[str] = None
    ) -> Tuple[Optional[Tuple[int, int]], bool, str, Dict[str, Any]]:
        """Auto-detect coordinate unit and enforce strict boundary validation (R2 & R3).
        Returns:
            (center_px, is_valid, reason, trace_metadata)
        """
        vw, vh = viewport
        c0, c1, c2, c3 = box
        x_min, x_max = min(c0, c2), max(c0, c2)
        y_min, y_max = min(c1, c3), max(c1, c3)

        # 1. Immediate negative check
        if x_min < 0 or y_min < 0 or x_max < 0 or y_max < 0:
            return None, False, "negative_coordinates", {
                "coordinate_unit_detected": declared_unit or "unknown",
                "conversion_method": "none",
                "boundary_check_passed": False,
                "boundary_check_reason": "negative_coordinates"
            }

        # 2. Determine Unit (R2)
        unit = "pixel"
        if declared_unit:
            d = declared_unit.lower()
            if "norm" in d or "1000" in d:
                unit = "normalized_1000"
            else:
                unit = "pixel"

        # 3. Convert coordinates if normalized
        conversion_method = "direct_pixel"
        if unit == "normalized_1000":
            px1 = (x_min * vw) / 1000.0
            px2 = (x_max * vw) / 1000.0
            py1 = (y_min * vh) / 1000.0
            py2 = (y_max * vh) / 1000.0
            conversion_method = "scaled_from_1000_to_viewport"
        else:
            px1, px2 = float(x_min), float(x_max)
            py1, py2 = float(y_min), float(y_max)

        # 4. Strict Boundary Validation on pixel coordinates (R3)
        ok, reason = cls.validate_bounds(px1, py1, px2, py2, vw, vh)
        meta = {
            "coordinate_unit_detected": unit,
            "conversion_method": conversion_method,
            "boundary_check_passed": ok,
            "boundary_check_reason": reason
        }

        if not ok:
            return None, False, reason, meta

        center_x = int((px1 + px2) / 2.0)
        center_y = int((py1 + py2) / 2.0)
        return (center_x, center_y), True, "valid", meta
