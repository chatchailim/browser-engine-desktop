"""Fast DOM Interactive Elements Locator and Parser.
Extracts actionable DOM elements with stable selectors, text, bounding boxes, and visibility checks.
"""
from typing import Any, Dict, List, Optional, Tuple
from models.schemas import Strategy, TargetCandidate

DOM_INSPECTOR_JS = """() => {
    const selectorFor = (el) => {
        if (el.id && !/\\d{4,}/.test(el.id)) return '#' + CSS.escape(el.id);
        const tid = el.getAttribute('data-testid') || el.getAttribute('data-test');
        if (tid) return `[data-testid="${tid}"]`;
        const name = el.getAttribute('name');
        if (name) return `${el.tagName.toLowerCase()}[name="${name}"]`;
        const role = el.getAttribute('role');
        const aria = el.getAttribute('aria-label');
        if (role && aria) return `[role="${role}"][aria-label="${aria}"]`;
        return '';
    };

    const elements = [];
    const candidates = document.querySelectorAll('button, a, input, select, textarea, [role=button], [role=link], [role=searchbox], [role=checkbox], [role=combobox], [tabindex="0"]');

    let idx = 1;
    for (const el of candidates) {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;

        const text = (el.innerText || el.getAttribute('aria-label') || el.placeholder || el.value || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
        const sel = selectorFor(el);
        elements.push({
            id: idx++,
            tag: el.tagName.toLowerCase(),
            role: el.getAttribute('role') || el.tagName.toLowerCase(),
            selector: sel,
            text: text,
            bounds: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
            center: [Math.round(rect.left + rect.width / 2), Math.round(rect.top + rect.height / 2)],
            disabled: el.disabled || el.getAttribute('aria-disabled') === 'true',
            focused: document.activeElement === el
        });
    }
    return elements;
}"""

import re


class DOMParser:
    @staticmethod
    def get_interactive_elements(page: Any) -> List[Dict[str, Any]]:
        """Extract interactive DOM elements with bounding boxes and selectors."""
        try:
            return page.evaluate(DOM_INSPECTOR_JS)
        except Exception:
            return []

    @staticmethod
    def get_page_summary(page: Any, max_chars: int = 6000) -> str:
        """Extract a structured, token-efficient text summary of the page context (<= max_chars)."""
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        try:
            url = page.url or ""
        except Exception:
            url = ""

        headings = []
        try:
            headings = page.evaluate("""() => {
                const hs = Array.from(document.querySelectorAll('h1, h2, h3'));
                return hs.map(h => {
                    const t = (h.innerText || '').trim();
                    return t ? `${h.tagName.toLowerCase()}: ${t}` : null;
                }).filter(Boolean).slice(0, 15);
            }""") or []
        except Exception:
            pass

        elements = DOMParser.get_interactive_elements(page)

        summary_parts = [
            f"TITLE: {title}",
            f"URL: {url}"
        ]
        if headings:
            summary_parts.append("HEADINGS:\n" + "\n".join(f"- {h}" for h in headings))

        elem_lines = []
        for el in elements:
            eid = el.get("id")
            role = el.get("role", "element")
            text = el.get("text", "")
            tag = el.get("tag", "")
            elem_lines.append(f"[{eid}] {role}: \"{text}\" ({tag})")

        header_text = "\n".join(summary_parts) + "\nINTERACTIVE ELEMENTS:\n"
        budget = max_chars - len(header_text)
        if budget < 200:
            return header_text[:max_chars]

        cur_text = ""
        for line in elem_lines:
            if len(cur_text) + len(line) + 1 > budget:
                break
            cur_text += line + "\n"

        return header_text + cur_text

    @staticmethod
    def match_candidates(elements: List[Dict[str, Any]], query: str) -> List[TargetCandidate]:
        """Match query against DOM elements and return ranked TargetCandidates."""
        q_clean = query.strip()
        q_lower = q_clean.lower()
        candidates: List[TargetCandidate] = []

        # Check for [index] or numeric index query first
        idx_match = re.search(r"\[(\d+)\]", q_clean)
        target_id = int(idx_match.group(1)) if idx_match else (int(q_clean) if q_clean.isdigit() else None)

        if target_id is not None:
            for el in elements:
                if el.get("id") == target_id:
                    bounds = el.get("bounds")
                    center = el.get("center")
                    cand = TargetCandidate(
                        strategy=Strategy.DOM,
                        confidence=1.0,
                        selector=el.get("selector") or None,
                        role=el.get("role"),
                        name=el.get("text"),
                        bounds=tuple(bounds) if bounds else None,
                        coordinates=tuple(center) if center else None,
                        element_id=el.get("id"),
                        metadata={"focused": el.get("focused"), "index": target_id}
                    )
                    return [cand]

        for el in elements:
            if el.get("disabled"):
                continue
            text = el.get("text", "").lower()
            selector = el.get("selector", "")
            bounds = el.get("bounds")
            center = el.get("center")

            conf = 0.0
            # Direct ID/testid selector or exact text match -> 0.99
            if q_lower == text:
                conf = 0.99
            elif selector and (f"#{q_lower}" == selector.lower() or q_lower in selector.lower()):
                conf = 0.98
            elif q_lower in text:
                conf = 0.92
            elif text in q_lower and len(text) > 3:
                conf = 0.85

            if conf >= 0.70:
                cand = TargetCandidate(
                    strategy=Strategy.DOM,
                    confidence=conf,
                    selector=selector or None,
                    role=el.get("role"),
                    name=el.get("text"),
                    bounds=tuple(bounds) if bounds else None,
                    coordinates=tuple(center) if center else None,
                    element_id=el.get("id"),
                    metadata={"focused": el.get("focused")}
                )
                candidates.append(cand)

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates
