"""5-Level Interaction Router with Confidence Scoring.
Ranks and selects the fastest and most reliable targeting strategy:
Direct API -> DOM -> Accessibility Tree -> Vision Coordinate -> Windows UIA.
"""
from typing import Any, Dict, List, Optional, Tuple
from models.schemas import Strategy, TargetCandidate
from perception.dom import DOMParser
from perception.accessibility import AccessibilityParser
from execution.coordinates import CoordinateManager


class InteractionRouter:
    @staticmethod
    def resolve_verify_target(
        page: Any,
        target_description: str,
        timeout_seconds: float = 3.0
    ) -> TargetCandidate:
        """Resolve verify/read-only target from DOM & Accessibility with DOM polling.
        Falls back to Vision ONLY if vision_enabled=true, never when disabled (R1).
        """
        import time
        deadline = time.monotonic() + max(timeout_seconds, 0.5)

        DOM_VERIFY_JS = """(query) => {
            if (!query) return null;
            let tag = null;
            let searchText = query.trim();

            const colonIdx = query.indexOf(':');
            if (colonIdx > 0) {
                const potentialTag = query.slice(0, colonIdx).trim().toLowerCase();
                if (/^[a-z][a-z0-9]*$/.test(potentialTag)) {
                    tag = potentialTag;
                    searchText = query.slice(colonIdx + 1).trim();
                }
            }

            function isVis(elem) {
                if (!elem || elem.nodeType !== 1) return false;
                if (typeof elem.checkVisibility === 'function') {
                    if (!elem.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) {
                        return false;
                    }
                }
                const r = elem.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return false;
                let cur = elem;
                while (cur && cur !== document.documentElement) {
                    const s = window.getComputedStyle(cur);
                    if (!s || s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') {
                        return false;
                    }
                    cur = cur.parentElement;
                }
                return true;
            }

            // 1. Try query directly as CSS selector
            try {
                const el = document.querySelector(query);
                if (el && isVis(el)) {
                    const r = el.getBoundingClientRect();
                    return {
                        found: true,
                        selector: query,
                        text: (el.innerText || el.textContent || '').trim(),
                        bounds: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                        tag: el.tagName.toLowerCase()
                    };
                }
            } catch(e) {}

            // 2. Search specific tag or candidate text elements
            const selector = tag || 'h1, h2, h3, h4, h5, h6, p, span, div, strong, b, li, td, th, label, [role=heading], [role=status], [role=alert]';
            let candidates = [];
            try {
                candidates = document.querySelectorAll(selector);
            } catch(e) {
                candidates = document.querySelectorAll('*');
            }
            const targetLower = searchText.toLowerCase();

            for (const el of candidates) {
                if (!isVis(el)) continue;
                const text = (el.innerText || el.textContent || '').trim();
                if (!text) continue;
                const textLower = text.toLowerCase();
                if (textLower === targetLower || textLower.includes(targetLower) || targetLower.includes(textLower)) {
                    const r = el.getBoundingClientRect();
                    let sel = '';
                    if (el.id) sel = '#' + CSS.escape(el.id);
                    else if (tag) sel = tag;
                    return {
                        found: true,
                        selector: sel || tag || el.tagName.toLowerCase(),
                        text: text,
                        bounds: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                        tag: el.tagName.toLowerCase()
                    };
                }
            }

            // 3. Fallback: check entire document body innerText
            if (document.body && document.body.innerText && document.body.innerText.toLowerCase().includes(targetLower)) {
                return {
                    found: true,
                    selector: 'body',
                    text: searchText,
                    bounds: [0, 0, window.innerWidth, window.innerHeight],
                    tag: 'body'
                };
            }

            return null;
        }"""

        while True:
            # Level 1: DOM search
            try:
                dom_res = page.evaluate(DOM_VERIFY_JS, target_description)
                if dom_res and dom_res.get("found"):
                    bounds = tuple(dom_res["bounds"])
                    coords = (bounds[0] + bounds[2] // 2, bounds[1] + bounds[3] // 2)
                    return TargetCandidate(
                        strategy=Strategy.DOM,
                        confidence=0.98,
                        selector=dom_res.get("selector"),
                        name=dom_res.get("text") or target_description,
                        bounds=bounds,
                        coordinates=coords,
                        metadata={"found": True, "tag": dom_res.get("tag")}
                    )
            except Exception:
                pass

            # Level 2: Accessibility Tree
            try:
                clean_text = target_description.split(":", 1)[1].strip() if ":" in target_description else target_description
                clean_lower = clean_text.lower().strip()
                ax_nodes = AccessibilityParser.get_snapshot(page)
                ax_candidates = AccessibilityParser.match_candidates(ax_nodes, clean_text)
                for cand in ax_candidates:
                    c_name = (cand.name or "").lower().strip()
                    if c_name and (clean_lower in c_name or c_name in clean_lower) and cand.confidence >= 0.70:
                        cand.metadata = cand.metadata or {}
                        cand.metadata["found"] = True
                        return cand
            except Exception:
                pass

            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)

        # Level 3: Vision fallback ONLY if vision is actually enabled
        from models.vision import get_global_vision_client
        vision_client = get_global_vision_client()
        if vision_client and vision_client.vision_enabled:
            return vision_client.locate_target(page, target_description)

        # Level 4: Safe non-found result (DO NOT fallback to Strategy.VISION when vision is disabled)
        return TargetCandidate(
            strategy=Strategy.DOM,
            confidence=0.0,
            name=target_description,
            selector=None,
            metadata={"found": False, "error": "target_not_found"}
        )

    @staticmethod
    def resolve_target(
        page: Any,
        target_description: str,
        preferred_role: Optional[str] = None,
        direct_api_payload: Optional[Dict[str, Any]] = None,
        action_type: Optional[str] = None
    ) -> TargetCandidate:
        """Resolve the optimal target candidate across the 5-level hierarchy."""
        if action_type == "verify":
            return InteractionRouter.resolve_verify_target(page, target_description)

        # Level 1: Direct API (WO-015)
        recipe_name = None
        if direct_api_payload and direct_api_payload.get("recipe_name"):
            recipe_name = direct_api_payload["recipe_name"]
        elif action_type == "api_call":
            recipe_name = target_description.split(":", 1)[1].strip() if ":" in target_description else target_description.strip()
        elif target_description.startswith("api:") or target_description.startswith("recipe:"):
            recipe_name = target_description.split(":", 1)[1].strip()

        if recipe_name:
            from execution.direct_api import get_direct_api_client
            client = get_direct_api_client()
            recipe = client.get_recipe(recipe_name)
            if recipe:
                return TargetCandidate(
                    strategy=Strategy.DIRECT_API,
                    confidence=1.0,
                    name=recipe_name,
                    metadata={
                        "recipe_name": recipe_name,
                        "recipe": recipe,
                        "parameters": direct_api_payload.get("parameters", {}) if direct_api_payload else {}
                    }
                )
            else:
                return TargetCandidate(
                    strategy=Strategy.DIRECT_API,
                    confidence=0.0,
                    name=recipe_name,
                    metadata={"error": "unknown_recipe", "recipe_name": recipe_name}
                )

        if direct_api_payload and direct_api_payload.get("url"):
            return TargetCandidate(
                strategy=Strategy.DIRECT_API,
                confidence=1.0,
                metadata=direct_api_payload
            )

        # Gather parallel observations from DOM and Accessibility
        dom_elements = DOMParser.get_interactive_elements(page)
        dom_candidates = DOMParser.match_candidates(dom_elements, target_description)

        ax_nodes = AccessibilityParser.get_snapshot(page)
        ax_candidates = AccessibilityParser.match_candidates(ax_nodes, target_description, preferred_role)

        all_candidates: List[TargetCandidate] = []

        # Level 2: DOM candidates
        for c in dom_candidates:
            all_candidates.append(c)

        # Level 3: Accessibility candidates
        for c in ax_candidates:
            all_candidates.append(c)

        # Direct CSS selector check if description looks like a selector
        if any(target_description.startswith(p) for p in ('#', '.', '[', 'css=', '//', 'table', 'div', 'span', 'p', 'h1', 'h2', 'h3', 'a', 'button', 'input', 'select')):
            clean_sel = target_description[4:] if target_description.startswith("css=") else target_description
            try:
                rect = page.evaluate("""(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
                }""", clean_sel)
                if rect:
                    all_candidates.append(TargetCandidate(
                        strategy=Strategy.DOM,
                        confidence=0.99,
                        selector=clean_sel,
                        name=clean_sel,
                        bounds=tuple(rect),
                        coordinates=(rect[0] + rect[2] // 2, rect[1] + rect[3] // 2)
                    ))
            except Exception:
                pass

        # Select highest confidence candidate
        if all_candidates:
            all_candidates.sort(key=lambda c: c.confidence, reverse=True)
            best = all_candidates[0]

            # If confidence >= 0.90, proceed with DOM/AX
            if best.confidence >= 0.85:
                # Coordinate refinement: snap to bounding box center
                if best.bounds and not best.coordinates:
                    best.coordinates = (best.bounds[0] + best.bounds[2] // 2, best.bounds[1] + best.bounds[3] // 2)
                return best

        # Level 4: Vision Fallback
        # If vision_enabled is True, invoke VisionClient to locate the target visually
        from models.vision import get_global_vision_client
        vision_client = get_global_vision_client()
        if vision_client and vision_client.vision_enabled:
            return vision_client.locate_target(page, target_description)

        # If vision is not enabled (default), return placeholder preserving 0.2.1 behavior
        return TargetCandidate(
            strategy=Strategy.VISION,
            confidence=0.75,
            name=target_description,
            metadata={"needs_vision_grounding": True}
        )

    @staticmethod
    def refine_coordinates_with_bounds(coord: Tuple[int, int], candidate: TargetCandidate) -> Tuple[int, int]:
        """Refine approximate vision coordinate with crisp DOM/AX bounding box."""
        if candidate and candidate.bounds:
            return CoordinateManager.clamp_bounds(coord[0], coord[1], candidate.bounds)
        return coord
