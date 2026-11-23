"""Multi-Layer Interactive Automation Orchestrator.
Coordinates the Slow Brain (Planner), Interaction Router, Macro Engine,
State Verifier, Human Takeover, Risk Gate, and Structured JSONL Telemetry.
"""
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from models.schemas import (
    ActionStep, ActionType, MacroDefinition, PostCondition, PreCondition,
    RiskLevel, Strategy, TargetCandidate
)
from engine.planner import TaskPlanner
from engine.router import InteractionRouter
from engine.verifier import StateVerifier
from engine.recovery import RecoveryEngine
from execution.browser import BrowserAdapter
from execution.macro import ConditionalMacroRunner
from perception.dom import DOMParser
from policy.takeover import HumanTakeoverManager
from policy.risk import RiskPolicyEngine
from policy.approval import approval_manager
from telemetry.trace import TelemetryCollector
from engine.recipe_cache import make_planner_if_enabled
from engine.planner_tier import make_tiered_planner_if_enabled
from service import task_control


class InteractiveAutomationOrchestrator:
    def __init__(
        self,
        planner: Optional[TaskPlanner] = None,
        takeover_mgr: Optional[HumanTakeoverManager] = None,
        takeover_timeout_s: float = 300.0
    ):
        # ลำดับชั้น: tiered (งานง่าย→เล็ก) > recipe cached > เดิม — ทุกชั้น fail-open กลับโมเดลใหญ่
        self.planner = planner or make_tiered_planner_if_enabled() or make_planner_if_enabled() or TaskPlanner()
        self.takeover_mgr = takeover_mgr or HumanTakeoverManager()
        self.takeover_timeout_s = takeover_timeout_s
        self.recovery = RecoveryEngine()
        self.verifier = StateVerifier()

    def run_task(
        self,
        page: Any,
        goal: str,
        max_subtasks: int = 15,
        on_step_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        task_id: Optional[str] = None,
        is_logged_in_profile: bool = False
    ) -> Dict[str, Any]:
        """Execute goal using Multi-Layer Interactive Automation Engine."""
        trace_id = task_id or f"ITR-{uuid.uuid4().hex[:8]}"
        telemetry = TelemetryCollector(trace_id=trace_id)
        adapter = BrowserAdapter(page=page, show_click_visual=True)
        macro_runner = ConditionalMacroRunner(adapter=adapter, verifier=self.verifier)

        t_start = time.monotonic()
        task_answer: Optional[str] = None
        task_evidence: Optional[Dict[str, str]] = None

        # Step 1: Initial Safety & Bot Wall Check (B4 & B5)
        wall_detected, wall_reason = self.takeover_mgr.detect_security_wall(page)
        if wall_detected:
            self.takeover_mgr.trigger_takeover(wall_reason or "Security challenge detected")
            resumed = self.takeover_mgr.wait_if_paused(timeout_s=self.takeover_timeout_s)
            if not resumed:
                telemetry.write_jsonl_step(
                    step_num=0,
                    observation=f"{page.url}\n{page.title()}",
                    strategy="none",
                    confidence=0.0,
                    action_type="takeover",
                    target_desc="bot_wall",
                    result="TAKEOVER",
                    error="human_takeover_required"
                )
                return {
                    "ok": False,
                    "status": "human_takeover_required",
                    "reason": wall_reason,
                    "trace_id": trace_id
                }

        # Safe Cookie banner handling (Requirement A5: Reject only, never accept all)
        cookie_msg, cookie_takeover = self.recovery.handle_cookie_banner(page)
        if cookie_takeover:
            self.takeover_mgr.trigger_takeover("cookie_consent: only accept cookies available")
            resumed = self.takeover_mgr.wait_if_paused(timeout_s=self.takeover_timeout_s)
            if not resumed:
                return {
                    "ok": False,
                    "status": "human_takeover_required",
                    "reason": "cookie_consent",
                    "trace_id": trace_id
                }

        # Step 2: Context Extraction & Slow Brain Planning (B1 & B2)
        page_summary = DOMParser.get_page_summary(page, max_chars=6000)
        t_plan_start = time.monotonic()
        try:
            plan = self.planner.create_plan(
                goal=goal,
                page_title=page.title(),
                current_url=page.url,
                page_summary=page_summary
            )
            plan_ms = int((time.monotonic() - t_plan_start) * 1000)
            # WO-019 M2/M4 proof: ขั้นวางแผนสำเร็จลง trace ด้วย — เห็นบริบทธุรกิจที่ฉีด (hash) + endpoint ที่ใช้จริง
            telemetry.write_plan_step(goal, getattr(plan, "context", None) or {}, plan_ms)
        except TimeoutError:
            plan_ms = int((time.monotonic() - t_plan_start) * 1000)
            telemetry.write_jsonl_step(
                step_num=0,
                observation=f"{page.url}\n{page.title()}\n{page_summary[:500]}",
                strategy="planner",
                confidence=0.0,
                action_type="plan",
                target_desc="planner",
                latency_breakdown={"planning": plan_ms},
                result="FAILED",
                error="planner_timeout"
            )
            summary = telemetry.get_summary_metrics()
            summary.update({
                "ok": False,
                "status": "planner_timeout",
                "error": "planner_timeout",
                "trace_id": trace_id,
                "planning_ms": plan_ms
            })
            return summary
        except Exception as e:
            plan_ms = int((time.monotonic() - t_plan_start) * 1000)
            err_name = "planner_parse_error" if "parse_error" in str(e) else "planner_failed"
            telemetry.write_jsonl_step(
                step_num=0,
                observation=f"{page.url}\n{page.title()}\n{page_summary[:500]}",
                strategy="planner",
                confidence=0.0,
                action_type="plan",
                target_desc="planner",
                latency_breakdown={"planning": plan_ms},
                result="FAILED",
                error=err_name
            )
            summary = telemetry.get_summary_metrics()
            summary.update({
                "ok": False,
                "status": err_name,
                "error": err_name,
                "trace_id": trace_id,
                "planning_ms": plan_ms
            })
            return summary

        # Step 3: Fast Brain Execution Loop with Replan (B6)
        executed_subtasks = 0
        overall_ok = True
        last_error = None
        replan_count = 0
        max_risk_level = 0
        subtasks_queue = list(plan.sub_tasks[:max_subtasks])

        while subtasks_queue and executed_subtasks < max_subtasks:
            # WO-018 step2: ปุ่มหยุดจากผู้ใช้ (HTTP /api/task-cancel) — ตรวจก่อนทุกขั้น สถานะจบ cancelled
            if task_control.is_cancelled(trace_id):
                return {
                    "ok": False,
                    "status": "cancelled",
                    "error": "cancelled_by_user",
                    "trace_id": trace_id,
                    "metrics": {"total_elapsed_ms": int((time.monotonic() - t_start) * 1000)}
                }
            subtask = subtasks_queue.pop(0)

            action_name = subtask.get("action", "").lower()
            target_desc = subtask.get("target", "")
            val = subtask.get("value")
            expectation = subtask.get("expectation")

            if action_name == "done":
                telemetry.write_jsonl_step(
                    step_num=executed_subtasks + 1,
                    observation=f"{page.url}\n{page.title()}",
                    strategy="none",
                    confidence=1.0,
                    action_type="done",
                    target_desc="task_completed",
                    result="SUCCESS"
                )
                break

            # Mid-execution Bot Wall / Takeover check (B4 & B5)
            wall_detected, wall_reason = self.takeover_mgr.detect_security_wall(page)
            if wall_detected:
                self.takeover_mgr.trigger_takeover(wall_reason or "Security challenge encountered during execution")
                resumed = self.takeover_mgr.wait_if_paused(timeout_s=self.takeover_timeout_s)
                if not resumed:
                    return {
                        "ok": False,
                        "status": "human_takeover_required",
                        "reason": wall_reason,
                        "metrics": telemetry.get_summary_metrics()
                    }
                # Unpaused: re-read page summary and continue
                page_summary = DOMParser.get_page_summary(page, max_chars=6000)

            # Map to action type for risk assessment and routing
            if action_name in ("search", "type_and_enter", "fill"):
                act_enum = ActionType.TYPE_TEXT
            elif action_name == "scroll":
                act_enum = ActionType.SCROLL
            elif action_name == "extract":
                act_enum = ActionType.EXTRACT
            elif action_name == "verify":
                act_enum = ActionType.VERIFY
            elif action_name == "api_call":
                act_enum = ActionType.API_CALL
            else:
                act_enum = ActionType.CLICK

            # Resolve Target via 5-Level Hierarchy (B2 index and DOM/AX resolution)
            t_resolve_start = time.monotonic()
            target_cand = InteractionRouter.resolve_target(page, target_desc, action_type=action_name)
            resolve_ms = int((time.monotonic() - t_resolve_start) * 1000)

            # Requirement A3 & A4: Mandatory Risk Gate Check before every action
            if act_enum == ActionType.VERIFY:
                risk_level = RiskLevel.RISK_0_SAFE
            elif act_enum == ActionType.API_CALL:
                recipe_name = target_cand.name if (target_cand and target_cand.name) else target_desc
                recipe = target_cand.metadata.get("recipe") if target_cand else None
                method = recipe.get("method", "GET") if recipe else "GET"
                path = recipe.get("path", "") if recipe else ""
                call_params = subtask.params if hasattr(subtask, "params") else (val if isinstance(val, dict) else None)
                risk_level = RiskPolicyEngine.classify_api_call(
                    recipe_name=recipe_name,
                    method=method,
                    path=path,
                    params=call_params,
                    recipe=recipe
                )
            else:
                risk_level = RiskPolicyEngine.classify(
                    action_type=act_enum,
                    target_text=f"{target_desc} {val or ''}",
                    url=page.url,
                    goal=goal
                )
            max_risk_level = max(max_risk_level, risk_level.value)

            # Level 4 Vision Security & Policy Gates (WO-011)
            if target_cand and target_cand.strategy == Strategy.VISION:
                # 1. External endpoint rejection (Ground Rule 1)
                if target_cand.metadata.get("error") == "external_endpoint_rejected":
                    err_reason = target_cand.metadata.get("reason", "external_endpoint_rejected")
                    telemetry.write_jsonl_step(
                        step_num=executed_subtasks + 1,
                        observation=f"{page.url}\n{page.title()}",
                        strategy="vision",
                        confidence=0.0,
                        action_type=action_name,
                        target_desc=target_desc,
                        result="FAILED",
                        error="external_endpoint_rejected"
                    )
                    return {
                        "ok": False,
                        "status": "failed",
                        "error": "external_endpoint_rejected",
                        "reason": err_reason,
                        "trace_id": trace_id
                    }

                # 2. Vision Service Failure / Unavailability Fallback (Ground Rule 6 & V8)
                if target_cand.metadata.get("error") in ("vision_service_unavailable", "screenshot_failed"):
                    telemetry.write_jsonl_step(
                        step_num=executed_subtasks + 1,
                        observation=f"{page.url}\n{page.title()}",
                        strategy="vision",
                        confidence=0.0,
                        action_type=action_name,
                        target_desc=target_desc,
                        screenshot_hash=target_cand.metadata.get("screenshot_hash"),
                        result="FAILED",
                        error="no_grounded_target"
                    )
                    return {
                        "ok": False,
                        "status": "no_grounded_target",
                        "error": "no_grounded_target",
                        "reason": target_cand.metadata.get("error"),
                        "trace_id": trace_id
                    }

                # 3. Minimum Vision Confidence Gate (Ground Rule 4)
                cfg_min_conf = 0.75
                try:
                    from models.vision import load_config as load_vision_cfg
                    cfg_min_conf = float(load_vision_cfg().get("vision_min_confidence", 0.75))
                except Exception:
                    pass

                if target_cand.confidence < cfg_min_conf:
                    takeover_reason = f"low_vision_confidence: {target_cand.confidence:.2f} < {cfg_min_conf:.2f}"
                    self.takeover_mgr.trigger_takeover(takeover_reason)
                    telemetry.write_jsonl_step(
                        step_num=executed_subtasks + 1,
                        observation=f"{page.url}\n{page.title()}",
                        strategy="vision",
                        confidence=target_cand.confidence,
                        action_type=action_name,
                        target_desc=target_desc,
                        screenshot_hash=target_cand.metadata.get("screenshot_hash"),
                        result="TAKEOVER",
                        error="human_takeover_required"
                    )
                    resumed = self.takeover_mgr.wait_if_paused(timeout_s=self.takeover_timeout_s)
                    if not resumed:
                        return {
                            "ok": False,
                            "status": "human_takeover_required",
                            "reason": "low_vision_confidence",
                            "trace_id": trace_id
                        }

                # 3. Vision coordinates strictly prohibited for Risk >= 2 (Ground Rule 3)
                if risk_level.value >= RiskLevel.RISK_2_CONFIRM.value:
                    telemetry.write_jsonl_step(
                        step_num=executed_subtasks + 1,
                        observation=f"{page.url}\n{page.title()}",
                        strategy="vision",
                        confidence=target_cand.confidence,
                        action_type=action_name,
                        target_desc=target_desc,
                        screenshot_hash=target_cand.metadata.get("screenshot_hash"),
                        result="FAILED",
                        error="no_grounded_target"
                    )
                    return {
                        "ok": False,
                        "status": "no_grounded_target",
                        "error": "no_grounded_target",
                        "reason": "vision_coordinates_prohibited_for_risk_2_plus",
                        "trace_id": trace_id
                    }

            # Requirement A3 & A7: Risk Approval Gate
            require_approval = False
            if is_logged_in_profile and risk_level.value != 0:
                require_approval = True
            elif risk_level.value in (RiskLevel.RISK_2_CONFIRM.value, RiskLevel.RISK_3_APPROVAL.value):
                require_approval = True

            if require_approval:
                step_idx = executed_subtasks + 1
                desc = f"Action '{action_name}' on '{target_desc}' (Risk {risk_level.value})"
                if on_step_callback:
                    on_step_callback({
                        "subtask": step_idx,
                        "action": action_name,
                        "target": target_desc,
                        "status": "approval_required",
                        "risk_level": risk_level.value,
                        "description": desc
                    })
                telemetry.write_jsonl_step(
                    step_num=step_idx,
                    observation=f"{page.url}\n{page.title()}",
                    strategy=target_cand.strategy.value if target_cand else "dom",
                    confidence=target_cand.confidence if target_cand else 1.0,
                    action_type=action_name,
                    target_desc=target_desc,
                    value=val,
                    is_sensitive=(risk_level.value >= 1),
                    result="APPROVAL_REQUIRED",
                    error="approval_required"
                )
                approved = approval_manager.request_approval(
                    task_id=trace_id,
                    step_num=step_idx,
                    action_type=action_name,
                    target=target_desc,
                    risk_level=risk_level.value,
                    description=desc,
                    timeout=300.0
                )
                if not approved:
                    summary = telemetry.get_summary_metrics()
                    summary["status"] = "approval_required"
                    summary["error"] = "approval_required"
                    summary["reason"] = f"Action '{action_name}' requires approval ({risk_level.value}) but was not approved within 5 minutes (cancelled)."
                    summary["pending_step"] = step_idx
                    return summary

            # Requirement B3: Action EXTRACT (read-only, NO click, returns answer & evidence)
            if action_name == "extract":
                t_extract_start = time.monotonic()
                extracted_text = ""
                sel = target_cand.selector if target_cand else None
                if sel:
                    try:
                        extracted_text = page.locator(sel).first.inner_text(timeout=2000)
                    except Exception:
                        try:
                            extracted_text = page.evaluate("(sel) => { const el = document.querySelector(sel); return el ? (el.innerText || el.value || '') : ''; }", sel)
                        except Exception:
                            pass

                ev_text = ""
                if not extracted_text:
                    try:
                        # Gather key content including infoboxes and product details
                        parts = []
                        if page.query_selector("table.infobox"):
                            parts.append(page.inner_text("table.infobox"))
                        if page.query_selector(".price_color, .product_main"):
                            parts.append(page.inner_text("article.product_pod, .page_inner"))
                        parts.append(page.inner_text("body")[:6000])
                        p_body = "\n".join(p for p in parts if p)
                        ext_res = self.planner.extract_from_page(goal=goal, page_text=p_body)
                        extracted_text = ext_res.get("answer", "")
                        ev_text = ext_res.get("evidence", extracted_text)
                    except Exception:
                        pass

                answer_val = extracted_text.strip() if extracted_text else target_desc
                task_answer = answer_val
                task_evidence = {
                    "selector": sel or "body",
                    "text": (ev_text or answer_val)[:300]
                }
                exec_ms = int((time.monotonic() - t_extract_start) * 1000)

                telemetry.write_jsonl_step(
                    step_num=executed_subtasks + 1,
                    observation=f"{page.url}\n{page.title()}",
                    strategy=target_cand.strategy.value if target_cand else "dom",
                    confidence=target_cand.confidence if target_cand else 1.0,
                    action_type="extract",
                    target_desc=target_desc,
                    value=answer_val,
                    is_sensitive=False,
                    latency_breakdown={"planning": resolve_ms, "execution": exec_ms},
                    result="SUCCESS"
                )
                if on_step_callback:
                    on_step_callback({
                        "subtask": executed_subtasks + 1,
                        "action": "extract",
                        "target": target_desc,
                        "answer": answer_val,
                        "evidence": task_evidence,
                        "ok": True,
                        "elapsed_ms": exec_ms
                    })
                executed_subtasks += 1
                continue

            # Requirement B3 & WO-014.1 (R1 & R2): Action VERIFY (read-only verification, NO click)
            if action_name == "verify":
                t_verify_start = time.monotonic()
                is_verified = bool(target_cand and target_cand.confidence >= 0.70 and target_cand.metadata.get("found", True))
                if not is_verified and target_cand and target_cand.selector:
                    try:
                        is_verified = page.locator(target_cand.selector).first.is_visible(timeout=500)
                    except Exception:
                        pass
                if not is_verified:
                    try:
                        clean_text = target_desc.split(":", 1)[1].strip() if ":" in target_desc else target_desc
                        is_verified = page.evaluate("(q) => document.body ? document.body.innerText.includes(q) : false", clean_text)
                    except Exception:
                        pass
                exec_ms = int((time.monotonic() - t_verify_start) * 1000)
                telemetry.write_jsonl_step(
                    step_num=executed_subtasks + 1,
                    observation=f"{page.url}\n{page.title()}",
                    strategy=target_cand.strategy.value if target_cand else "dom",
                    confidence=target_cand.confidence if target_cand else 0.0,
                    action_type="verify",
                    target_desc=target_desc,
                    value=str(is_verified),
                    is_sensitive=False,
                    latency_breakdown={"planning": resolve_ms, "execution": exec_ms, "verification": exec_ms},
                    result="SUCCESS" if is_verified else "FAILED",
                    error=None if is_verified else "target_not_found"
                )
                if on_step_callback:
                    on_step_callback({
                        "subtask": executed_subtasks + 1,
                        "action": "verify",
                        "target": target_desc,
                        "verified": is_verified,
                        "ok": is_verified,
                        "elapsed_ms": exec_ms
                    })
                executed_subtasks += 1
                continue

            # Requirement WO-015: Action API_CALL (Level 1 Direct API execution)
            if action_name == "api_call":
                t_api_start = time.monotonic()
                from execution.direct_api import get_direct_api_client
                api_client = get_direct_api_client()

                recipe_name = target_cand.name if (target_cand and target_cand.name) else target_desc
                call_params = {}
                if hasattr(subtask, "params") and isinstance(subtask.params, dict):
                    call_params = subtask.params
                elif val and isinstance(val, dict):
                    call_params = val
                elif target_cand and target_cand.metadata.get("parameters"):
                    call_params = target_cand.metadata["parameters"]

                api_result = api_client.execute_call(
                    recipe_name=recipe_name,
                    parameters=call_params,
                    task_id=trace_id,
                    step_num=executed_subtasks + 1
                )
                exec_ms = api_result.get("elapsed_ms", int((time.monotonic() - t_api_start) * 1000))
                is_api_ok = api_result.get("ok", False)

                # Requirement GR8: Spool FactApiCallExecuted in OneVault
                try:
                    from telemetry.vault import get_vault_manager
                    vault = get_vault_manager()
                    if vault.enabled and api_result.get("status") != "direct_api_disabled":
                        fact = vault.create_api_call_fact(
                            task_id=trace_id,
                            step_num=executed_subtasks + 1,
                            recipe_name=recipe_name,
                            host=api_result.get("host", ""),
                            path=api_result.get("path", ""),
                            method=api_result.get("method", "GET"),
                            status_code=api_result.get("status_code", 0),
                            elapsed_ms=exec_ms,
                            risk_level=risk_level.value,
                            request_body_hash=api_result.get("request_body_hash", ""),
                            response_body_hash=api_result.get("response_body_hash", ""),
                            insecure_http=api_result.get("insecure_http", False)
                        )
                        if fact:
                            vault.spool_fact(fact)
                            vault.flush_spool_async()
                except Exception as e:
                    sys.stderr.write(f"[*] [OneVault] Warning: FactApiCallExecuted creation skipped ({e})\n")

                telemetry.write_jsonl_step(
                    step_num=executed_subtasks + 1,
                    observation=f"api_call:{recipe_name}",
                    strategy="direct_api",
                    confidence=1.0 if is_api_ok else 0.0,
                    action_type="api_call",
                    target_desc=recipe_name,
                    value=f"status_code={api_result.get('status_code')}",
                    is_sensitive=(risk_level.value >= 1),
                    latency_breakdown={"planning": resolve_ms, "execution": exec_ms},
                    result="SUCCESS" if is_api_ok else "FAILED",
                    error=api_result.get("error") if not is_api_ok else None
                )

                if on_step_callback:
                    on_step_callback({
                        "subtask": executed_subtasks + 1,
                        "action": "api_call",
                        "recipe_name": recipe_name,
                        "status_code": api_result.get("status_code"),
                        "ok": is_api_ok,
                        "elapsed_ms": exec_ms
                    })

                if is_api_ok and api_result.get("response_data"):
                    resp_val = str(api_result["response_data"])
                    task_answer = resp_val
                    task_evidence = {"selector": "direct_api", "text": resp_val[:300]}

                executed_subtasks += 1
                if not is_api_ok:
                    last_error = api_result.get("error", "api_call_failed")
                    overall_ok = False
                    break
                continue

            # Build Conditional Macro steps for physical actions
            macro_steps: List[ActionStep] = []

            if action_name in ("search", "type_and_enter"):
                macro_steps.append(ActionStep(
                    action=ActionType.CLICK,
                    target=target_cand,
                    risk=risk_level,
                    precondition=PreCondition(visible=True)
                ))
                macro_steps.append(ActionStep(
                    action=ActionType.TYPE_TEXT,
                    target=target_cand,
                    text=val or "",
                    risk=risk_level
                ))
                macro_steps.append(ActionStep(
                    action=ActionType.PRESS_KEY,
                    key="Enter",
                    risk=risk_level,
                    postcondition=PostCondition(navigation=True) if "load" in (expectation or "") else None
                ))

            elif action_name == "click":
                macro_steps.append(ActionStep(
                    action=ActionType.CLICK,
                    target=target_cand,
                    risk=risk_level,
                    precondition=PreCondition(visible=True),
                    postcondition=PostCondition(navigation=True) if "navig" in (expectation or "") else None
                ))

            elif action_name == "fill":
                macro_steps.append(ActionStep(
                    action=ActionType.CLICK,
                    target=target_cand,
                    risk=risk_level
                ))
                macro_steps.append(ActionStep(
                    action=ActionType.TYPE_TEXT,
                    target=target_cand,
                    text=val or "",
                    risk=risk_level
                ))

            elif action_name == "scroll":
                macro_steps.append(ActionStep(
                    action=ActionType.SCROLL,
                    delta_y=600,
                    risk=risk_level
                ))

            else:
                macro_steps.append(ActionStep(
                    action=ActionType.CLICK,
                    target=target_cand,
                    risk=risk_level
                ))

            # Execute Macro via Fast Engine
            macro_def = MacroDefinition(
                macro_id=f"{trace_id}-s{executed_subtasks+1}",
                steps=macro_steps,
                stop_on_failure=True,
                description=f"{action_name} on {target_desc}"
            )

            macro_res = macro_runner.run_macro(macro_def, page)
            for tr in macro_res.get("traces", []):
                telemetry.record_trace(tr)

            # Requirement B7: Write step to data/traces/<task_id>.jsonl
            telemetry.write_jsonl_step(
                step_num=executed_subtasks + 1,
                observation=f"{page.url}\n{page.title()}",
                strategy=target_cand.strategy.value if target_cand else "dom",
                confidence=target_cand.confidence if target_cand else 1.0,
                action_type=action_name,
                target_desc=target_desc,
                value=val,
                is_sensitive=(risk_level.value >= 1),
                latency_breakdown={"planning": resolve_ms, "execution": macro_res.get("elapsed_ms", 0)},
                result="SUCCESS" if macro_res.get("ok", False) else "FAILED",
                error=macro_res.get("error"),
                screenshot_hash=target_cand.metadata.get("screenshot_hash") if target_cand else None
            )

            if on_step_callback:
                on_step_callback({
                    "subtask": executed_subtasks + 1,
                    "action": action_name,
                    "target": target_desc,
                    "strategy": target_cand.strategy.value if target_cand else "unknown",
                    "confidence": target_cand.confidence if target_cand else 1.0,
                    "is_vision": (target_cand.strategy == Strategy.VISION) if target_cand else False,
                    "ok": macro_res.get("ok", False),
                    "elapsed_ms": macro_res.get("elapsed_ms", 0)
                })

            executed_subtasks += 1

            if not macro_res.get("ok", False):
                step_err = macro_res.get("error", "macro_failed")
                # Requirement B6: Replan on step failure (up to 2 times per task)
                if replan_count < 2:
                    replan_count += 1
                    try:
                        new_page_summary = DOMParser.get_page_summary(page, max_chars=6000)
                        new_plan = self.planner.replan(
                            goal=goal,
                            failed_step=subtask,
                            error=step_err,
                            page_summary=new_page_summary
                        )
                        if new_plan.sub_tasks:
                            subtasks_queue = list(new_plan.sub_tasks)
                            continue
                    except Exception:
                        pass

                last_error = step_err
                overall_ok = False
                break

        total_elapsed_ms = int((time.monotonic() - t_start) * 1000)
        if not task_answer and overall_ok:
            try:
                parts = []
                if page.query_selector("table.infobox"):
                    parts.append(page.inner_text("table.infobox"))
                if page.query_selector(".price_color, .product_main"):
                    parts.append(page.inner_text("article.product_pod, .page_inner"))
                parts.append(page.inner_text("body")[:6000])
                p_body = "\n".join(p for p in parts if p)
                ext_res = self.planner.extract_from_page(goal=goal, page_text=p_body)
                if ext_res and ext_res.get("answer"):
                    task_answer = ext_res.get("answer")
                    task_evidence = {"selector": "body", "text": (ext_res.get("evidence") or "")[:300]}
            except Exception:
                pass

        summary = telemetry.get_summary_metrics()
        summary["ok"] = overall_ok
        summary["total_elapsed_ms"] = total_elapsed_ms
        summary["planning_ms"] = plan_ms
        summary["status"] = "done" if overall_ok else (last_error or "failed")
        summary["error"] = last_error
        summary["answer"] = task_answer
        summary["evidence"] = task_evidence
        # Recipe-cache provenance + planner tier provenance (ผ่านมากับแผนที่ใช้จริง ไม่แต่งตอนสรุป)
        try:
            ctx = getattr(plan, "context", None) or {}
            _keep = {k: v for k, v in ctx.items()
                     if str(k).startswith("recipe_") or str(k).startswith("tier_")}
            # WO-019 M4: endpoint ที่ตอบจริงรอบนั้น — ตรวจย้อนสมองตัวไหนรับงานได้จาก summary/trace
            if ctx.get("endpoint_used"):
                _keep["endpoint_used"] = ctx["endpoint_used"]
            summary.update(_keep)
        except Exception:
            pass

        # Requirement 32 & Ground Rule 21: OneVault FactWebTaskExecuted (WO-012)
        try:
            from telemetry.vault import get_vault_manager
            vault = get_vault_manager()
            if vault.enabled:
                if task_answer:
                    telemetry.set_answer(task_answer)
                task_url = ""
                try:
                    if hasattr(page, "url"):
                        task_url = page.url
                except Exception:
                    pass

                fact = vault.create_task_fact(
                    task_id=trace_id,
                    url=task_url,
                    goal=goal,
                    total_steps=executed_subtasks,
                    max_risk_level=max_risk_level,
                    result="SUCCESS" if overall_ok else "FAILED",
                    elapsed_ms=total_elapsed_ms,
                    observation_hashes=telemetry.get_observation_hashes(),
                    answer_hash=telemetry.get_answer_hash()
                )
                if fact:
                    vault.spool_fact(fact)
                    vault.flush_spool_async()
        except Exception as e:
            sys.stderr.write(f"[*] [OneVault] Warning: Task fact creation skipped ({e})\n")

        return summary
