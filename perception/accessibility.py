"""Accessibility Tree Parser for Chrome/Playwright.
Extracts semantic UI nodes (role, name, value, bounds, states) directly from browser accessibility tree.
"""
from typing import Any, Dict, List, Optional, Tuple
from models.schemas import Strategy, TargetCandidate


INTERESTING_ROLES = {
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio",
    "tab", "menuitem", "slider", "switch", "option", "treeitem"
}


def parse_ax_node(node: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    """Recursively parse accessibility nodes and flatten actionable items."""
    role = node.get("role", "").lower()
    name = (node.get("name") or "").strip()
    value = node.get("value")
    description = (node.get("description") or "").strip()
    focused = bool(node.get("focused", False))
    disabled = bool(node.get("disabled", False))

    # Chrome accessibility snapshot may provide bounding box under 'box' or keys
    bounds = None
    if "box" in node:
        b = node["box"]
        bounds = (int(b.get("x", 0)), int(b.get("y", 0)), int(b.get("width", 0)), int(b.get("height", 0)))

    if role in INTERESTING_ROLES or name:
        results.append({
            "role": role,
            "name": name,
            "value": value,
            "description": description,
            "focused": focused,
            "disabled": disabled,
            "bounds": bounds,
            "raw": node
        })

    for child in node.get("children", []):
        parse_ax_node(child, results)


class AccessibilityParser:
    @staticmethod
    def get_snapshot(page: Any) -> List[Dict[str, Any]]:
        """Get flattened accessibility tree snapshot from Playwright page."""
        try:
            raw_tree = page.accessibility.snapshot(interesting_only=True)
            if not raw_tree:
                return []
            results: List[Dict[str, Any]] = []
            parse_ax_node(raw_tree, results)
            return results
        except Exception:
            return []

    @staticmethod
    def match_candidates(ax_nodes: List[Dict[str, Any]], query: str, preferred_role: Optional[str] = None) -> List[TargetCandidate]:
        """Match query against accessibility nodes and return ranked TargetCandidates."""
        q_lower = query.lower().strip()
        candidates: List[TargetCandidate] = []

        for node in ax_nodes:
            if node.get("disabled"):
                continue
            name = node.get("name", "").lower()
            role = node.get("role", "").lower()
            desc = node.get("description", "").lower()

            conf = 0.0
            # Exact name match
            if q_lower == name:
                conf = 0.98
            # Query contains name or name contains query
            elif q_lower in name or name in q_lower:
                conf = 0.90
            elif q_lower in desc:
                conf = 0.85
            # Role matches
            if preferred_role and role == preferred_role.lower():
                conf = min(1.0, conf + 0.05) if conf > 0 else 0.60

            if conf >= 0.60:
                bounds = node.get("bounds")
                coords = (bounds[0] + bounds[2] // 2, bounds[1] + bounds[3] // 2) if bounds else None
                cand = TargetCandidate(
                    strategy=Strategy.ACCESSIBILITY,
                    confidence=conf,
                    role=node.get("role"),
                    name=node.get("name"),
                    bounds=bounds,
                    coordinates=coords,
                    metadata={"value": node.get("value"), "focused": node.get("focused")}
                )
                candidates.append(cand)

        # Sort by highest confidence
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates
