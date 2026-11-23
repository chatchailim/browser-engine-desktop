"""Conditional Macro Runner with Pre/Post-condition Verification.
Executes atomic action sequences safely and halts on unexpected deviations.
"""
import json
import pathlib
import time
from typing import Any, Dict, List, Optional
from models.schemas import ActionStep, ActionType, MacroDefinition, ActionTrace, TargetCandidate, Strategy, RiskLevel
from execution.adapter_base import UIAdapter
from engine.verifier import StateVerifier

CONFIG_FILE = pathlib.Path(__file__).resolve().parent.parent / "config.json"


def _get_min_confidence(runner_override: Optional[float] = None) -> float:
    if runner_override is not None:
        return float(runner_override)
    if CONFIG_FILE.is_file():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if "vision_min_confidence" in cfg:
                return float(cfg["vision_min_confidence"])
        except Exception:
            pass
    return 0.75


class ConditionalMacroRunner:
    def __init__(
        self,
        adapter: UIAdapter,
        verifier: Optional[StateVerifier] = None,
        vision_min_confidence: Optional[float] = None
    ):
        self.adapter = adapter
        self.verifier = verifier or StateVerifier()
        self.vision_min_confidence = vision_min_confidence

    def run_macro(self, macro: MacroDefinition, page: Any) -> Dict[str, Any]:
        """Execute a conditional macro sequence step by step."""
        traces: List[ActionTrace] = []
        start_time = time.monotonic()
        initial_url = page.url

        for idx, step in enumerate(macro.steps, 1):
            step_start = time.monotonic()
            target = step.target

            # 1. Precondition check
            if step.precondition:
                pre_ok = self.verifier.verify_precondition(page, step.precondition, target)
                if not pre_ok:
                    trace = ActionTrace(
                        trace_id=macro.macro_id,
                        step_num=idx,
                        action_type=step.action.value,
                        strategy=target.strategy.value if target else "none",
                        confidence=target.confidence if target else 0.0,
                        target_desc=target.name or "" if target else "",
                        result="FAILED",
                        error="precondition_failed"
                    )
                    traces.append(trace)
                    if macro.stop_on_failure:
                        return {"ok": False, "failed_step": idx, "error": "precondition_failed", "traces": traces}

            # 2. Execution
            act_start = time.monotonic()
            exec_ok = False

            is_click = step.action in (ActionType.CLICK, ActionType.DOUBLE_CLICK, ActionType.RIGHT_CLICK)
            sel = target.selector if target else None
            coords = target.coordinates if (target and target.coordinates and target.coordinates != (0, 0)) else None
            is_vision = bool(target and target.strategy == Strategy.VISION)

            # Requirement A6 & P0-2: Never execute spatial actions without a grounded target
            if is_click and not sel and not coords:
                trace = ActionTrace(
                    trace_id=macro.macro_id,
                    step_num=idx,
                    action_type=step.action.value,
                    strategy=target.strategy.value if target else "none",
                    confidence=target.confidence if target else 0.0,
                    target_desc=target.name or "" if target else "",
                    result="FAILED",
                    error="no_grounded_target"
                )
                traces.append(trace)
                if macro.stop_on_failure:
                    return {"ok": False, "failed_step": idx, "error": "no_grounded_target", "traces": traces}
                continue

            if step.action == ActionType.MOUSE_MOVE and not coords:
                trace = ActionTrace(
                    trace_id=macro.macro_id,
                    step_num=idx,
                    action_type=step.action.value,
                    strategy=target.strategy.value if target else "none",
                    confidence=target.confidence if target else 0.0,
                    target_desc=target.name or "" if target else "",
                    result="FAILED",
                    error="no_grounded_target"
                )
                traces.append(trace)
                if macro.stop_on_failure:
                    return {"ok": False, "failed_step": idx, "error": "no_grounded_target", "traces": traces}
                continue

            # Ground Rule 3 (WO-011): Never execute spatial actions with vision coordinates for Risk >= 2
            if is_vision and step.risk >= RiskLevel.RISK_2_CONFIRM:
                trace = ActionTrace(
                    trace_id=macro.macro_id,
                    step_num=idx,
                    action_type=step.action.value,
                    strategy=Strategy.VISION.value,
                    confidence=target.confidence if target else 0.0,
                    target_desc=target.name or "" if target else "",
                    result="FAILED",
                    error="no_grounded_target",
                    details={"blocked_reason": "vision_coordinates_prohibited_for_risk_2_plus"}
                )
                traces.append(trace)
                if macro.stop_on_failure:
                    return {"ok": False, "failed_step": idx, "error": "no_grounded_target", "traces": traces}
                continue

            # Ground Rule 4 (WO-011 / WO-011.1): Low vision confidence below threshold requires takeover
            min_conf = _get_min_confidence(self.vision_min_confidence)
            if is_vision and target and target.confidence < min_conf:
                trace = ActionTrace(
                    trace_id=macro.macro_id,
                    step_num=idx,
                    action_type=step.action.value,
                    strategy=Strategy.VISION.value,
                    confidence=target.confidence,
                    target_desc=target.name or "",
                    result="TAKEOVER",
                    error="human_takeover_required",
                    details={"reason": "low_vision_confidence", "min_confidence": min_conf}
                )
                traces.append(trace)
                if macro.stop_on_failure:
                    return {"ok": False, "failed_step": idx, "error": "human_takeover_required", "reason": "low_vision_confidence", "traces": traces}
                continue

            recheck_meta = None
            if is_click and is_vision and coords and coords != (0, 0):
                # Requirement R4: Pre-click element re-verification via document.elementFromPoint
                recheck_ok, recheck_reason, recheck_meta = self._verify_target_element_under_point(
                    page, coords, target.name or ""
                )
                if not recheck_ok:
                    if recheck_reason == "canvas_element_cannot_verify":
                        # If page/element is pure canvas and cannot verify, drop confidence below threshold to require takeover
                        target.confidence = min(target.confidence, 0.5)
                        trace = ActionTrace(
                            trace_id=macro.macro_id,
                            step_num=idx,
                            action_type=step.action.value,
                            strategy=Strategy.VISION.value,
                            confidence=target.confidence,
                            target_desc=target.name or "",
                            result="TAKEOVER",
                            error="human_takeover_required",
                            details={"reason": "canvas_element_cannot_verify", "min_confidence": min_conf, **(recheck_meta or {})}
                        )
                        traces.append(trace)
                        if macro.stop_on_failure:
                            return {"ok": False, "failed_step": idx, "error": "human_takeover_required", "reason": "low_vision_confidence", "traces": traces}
                        continue
                    else:
                        # Mismatched element under coordinates -> no_grounded_target (0 clicks)
                        trace = ActionTrace(
                            trace_id=macro.macro_id,
                            step_num=idx,
                            action_type=step.action.value,
                            strategy=Strategy.VISION.value,
                            confidence=target.confidence,
                            target_desc=target.name or "",
                            result="FAILED",
                            error="no_grounded_target",
                            details={"mismatch_reason": recheck_reason, **(recheck_meta or {})}
                        )
                        traces.append(trace)
                        if macro.stop_on_failure:
                            return {"ok": False, "failed_step": idx, "error": "no_grounded_target", "traces": traces}
                        continue

            if step.action == ActionType.MOUSE_MOVE:
                if not coords or coords == (0, 0):
                    trace = ActionTrace(
                        trace_id=macro.macro_id,
                        step_num=idx,
                        action_type=step.action.value,
                        strategy=target.strategy.value if target else "none",
                        confidence=target.confidence if target else 0.0,
                        target_desc=target.name or "" if target else "",
                        result="FAILED",
                        error="no_grounded_target"
                    )
                    traces.append(trace)
                    if macro.stop_on_failure:
                        return {"ok": False, "failed_step": idx, "error": "no_grounded_target", "traces": traces}
                    continue
                exec_ok = self.adapter.mouse_move(coords[0], coords[1])

            elif step.action == ActionType.DRAG_AND_DROP:
                s = step.start_coord
                e = step.end_coord
                if not s or not e or s == (0, 0) or e == (0, 0):
                    trace = ActionTrace(
                        trace_id=macro.macro_id,
                        step_num=idx,
                        action_type=step.action.value,
                        strategy=target.strategy.value if target else "none",
                        confidence=target.confidence if target else 0.0,
                        target_desc=target.name or "" if target else "",
                        result="FAILED",
                        error="no_grounded_target"
                    )
                    traces.append(trace)
                    if macro.stop_on_failure:
                        return {"ok": False, "failed_step": idx, "error": "no_grounded_target", "traces": traces}
                    continue
                exec_ok = self.adapter.drag_and_drop(s[0], s[1], e[0], e[1])

            elif step.action == ActionType.CLICK:
                if not sel and not coords:
                    continue
                target_coords = coords or (0, 0)
                exec_ok = self.adapter.click(target_coords[0], target_coords[1], selector=sel)

            elif step.action == ActionType.DOUBLE_CLICK:
                if not sel and not coords:
                    continue
                target_coords = coords or (0, 0)
                exec_ok = self.adapter.double_click(target_coords[0], target_coords[1], selector=sel)

            elif step.action == ActionType.RIGHT_CLICK:
                if not sel and not coords:
                    continue
                target_coords = coords or (0, 0)
                exec_ok = self.adapter.right_click(target_coords[0], target_coords[1], selector=sel)

            elif step.action == ActionType.TYPE_TEXT:
                sel = target.selector if target else None
                exec_ok = self.adapter.type_text(step.text or "", selector=sel)

            elif step.action == ActionType.PRESS_KEY:
                exec_ok = self.adapter.press_key(step.key or "Enter")

            elif step.action == ActionType.HOTKEY:
                exec_ok = self.adapter.hotkey(step.keys or [])

            elif step.action == ActionType.SCROLL:
                exec_ok = self.adapter.scroll(step.delta_x, step.delta_y)

            elif step.action == ActionType.WAIT_UNTIL:
                # Dynamic wait
                exec_ok = self.verifier.wait_until_stable(page)

            elif step.action == ActionType.VERIFY:
                # Requirement R1 & WO-014.1: Read-only verification without clicks
                sel = target.selector if target else None
                if sel and hasattr(page, "locator"):
                    try:
                        exec_ok = page.locator(sel).first.is_visible(timeout=1000)
                    except Exception:
                        exec_ok = False
                if not exec_ok and target and target.name and hasattr(page, "evaluate"):
                    try:
                        clean_text = target.name.split(":", 1)[1].strip() if ":" in target.name else target.name
                        exec_ok = page.evaluate("(q) => document.body ? document.body.innerText.includes(q) : false", clean_text)
                    except Exception:
                        exec_ok = False
                if not exec_ok and target and target.confidence >= 0.70 and target.metadata.get("found", False):
                    exec_ok = True

            elif step.action == ActionType.EXTRACT:
                exec_ok = True

            act_ms = int((time.monotonic() - act_start) * 1000)

            # 3. Postcondition verification
            verify_start = time.monotonic()
            post_ok = True
            if step.postcondition:
                post_ok = self.verifier.verify_postcondition(page, step.postcondition, target, initial_url=initial_url)
            verify_ms = int((time.monotonic() - verify_start) * 1000)

            step_total_ms = int((time.monotonic() - step_start) * 1000)
            overall_step_ok = exec_ok and post_ok

            trace = ActionTrace(
                trace_id=macro.macro_id,
                step_num=idx,
                action_type=step.action.value,
                strategy=target.strategy.value if target else "none",
                confidence=target.confidence if target else 0.0,
                target_desc=target.name or "" if target else "",
                latency_breakdown_ms={"execution": act_ms, "verification": verify_ms},
                total_ms=step_total_ms,
                result="SUCCESS" if overall_step_ok else "FAILED",
                error=None if overall_step_ok else ("execution_failed" if not exec_ok else "postcondition_failed"),
                details=recheck_meta if recheck_meta else None
            )
            traces.append(trace)

            if not overall_step_ok and macro.stop_on_failure:
                return {
                    "ok": False,
                    "failed_step": idx,
                    "error": trace.error,
                    "elapsed_ms": int((time.monotonic() - start_time) * 1000),
                    "traces": traces
                }

        total_elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return {
            "ok": True,
            "total_steps": len(macro.steps),
            "elapsed_ms": total_elapsed_ms,
            "traces": traces
        }

    def _verify_target_element_under_point(
        self,
        page: Any,
        coords: Tuple[int, int],
        target_desc: str
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """Requirement R4: Pre-click element re-verification.
        Inspects element under coordinates using document.elementFromPoint.
        - If pure canvas and cannot verify -> returns (False, 'canvas_element_cannot_verify', meta)
        - If element matches target keywords -> returns (True, 'matched', meta)
        - If element does not match -> returns (False, 'target_mismatch', meta)
        """
        if not hasattr(page, "evaluate") or not callable(getattr(page, "evaluate")):
            # Non-browser mock page without evaluate
            return True, "skipped_no_evaluate", {}

        try:
            x, y = coords
            js_script = """
            ([x, y]) => {
                const el = document.elementFromPoint(x, y);
                if (!el) {
                    return { found: false, tag: null, text: '', aria: '', role: '', is_canvas: false };
                }
                const tag = (el.tagName || '').toLowerCase();
                const isCanvas = (tag === 'canvas');
                const text = (el.innerText || el.textContent || el.value || '').trim();
                const aria = (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || '').trim();
                const role = (el.getAttribute('role') || tag || '').trim();
                const elId = (el.id || '').toLowerCase();
                const elClass = (el.className || '').toString().toLowerCase();

                // Check if page is pure canvas (whole page consists only of canvas without semantic content)
                const semanticEls = document.querySelectorAll('h1, h2, h3, p, a, button, input, label, table, form');
                const isPureCanvas = (isCanvas && semanticEls.length === 0);

                return {
                    found: true,
                    tag: tag,
                    id: elId,
                    class: elClass,
                    text: text,
                    aria: aria,
                    role: role,
                    is_canvas: isCanvas,
                    is_pure_canvas: isPureCanvas
                };
            }
            """
            el_info = page.evaluate(js_script, [x, y])
            if not el_info or not el_info.get("found"):
                return False, "no_element_at_point", {"re_verification_passed": False, "element_inspected": el_info}

            if el_info.get("is_pure_canvas"):
                # Pure canvas page: DOM cannot inspect anything -> requires human takeover (R4)
                return False, "canvas_element_cannot_verify", {
                    "re_verification_passed": False,
                    "element_inspected": el_info
                }

            # Check match against target_desc
            el_text = (el_info.get("text") or "").lower()
            el_aria = (el_info.get("aria") or "").lower()
            el_role = (el_info.get("role") or "").lower()
            el_id = (el_info.get("id") or "").lower()
            el_class = (el_info.get("class") or "").lower()
            desc = (target_desc or "").lower()

            # Tokenize description
            import re
            desc_tokens = set(re.findall(r"\w+", desc))
            # Remove generic filler words
            meaningful_tokens = {t for t in desc_tokens if t not in ("button", "click", "the", "a", "an", "on", "to", "ปุ่ม", "กด")}
            if not meaningful_tokens:
                meaningful_tokens = desc_tokens

            matched = False
            if el_text and (el_text in desc or desc in el_text):
                matched = True
            elif el_aria and (el_aria in desc or desc in el_aria):
                matched = True
            elif any(token in el_text for token in meaningful_tokens):
                matched = True
            elif any(token in el_aria for token in meaningful_tokens):
                matched = True
            elif el_info.get("tag") in ("button", "a", "input") and any(token in el_role for token in meaningful_tokens):
                matched = True
            elif el_info.get("tag") == "canvas":
                # Canvas button: verify if id, class, or button context matches
                if any(t in el_id for t in ("btn", "button", "action")) or any(t in el_class for t in ("btn", "button", "action")):
                    matched = True
                elif any(token in el_id for token in meaningful_tokens) or any(token in el_class for token in meaningful_tokens):
                    matched = True
                elif "button" in desc or "ปุ่ม" in desc:
                    matched = True

            meta = {
                "re_verification_passed": matched,
                "re_verification_method": "document.elementFromPoint",
                "element_inspected": el_info
            }

            if matched:
                return True, "matched", meta
            return False, "target_mismatch", meta
        except Exception as e:
            return False, f"re_verification_exception: {e}", {"error": str(e)}
