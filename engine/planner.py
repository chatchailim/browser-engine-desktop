"""Slow Brain Planner AI.
Analyzes user intent once, decomposes complex goals into sub-tasks and atomic macro steps,
and only intervenes when the Fast Brain reports an unexpected deviation.
"""
import json
import re
import time
from typing import Any, Dict, List, Optional
from models.client import DGXVllmClient
from models.schemas import PlanTask

try:
    from service.task_memory import build_context_block  # WO-019 M2: บริบทธุรกิจราย-Node (จำข้อเท็จจริง ไม่จำคำตอบ)
except Exception:
    def build_context_block(max_chars=1200):
        return ""

PLANNER_SYSTEM_PROMPT = """You are a High-Level Task Planner for an autonomous web agent.
Analyze the user's overall GOAL and the current page context.
Decompose the goal into an efficient, deterministic sequence of SUB-TASKS (1 to 15 sub-tasks maximum).

Each sub-task must specify:
- "action": one of ["search", "click", "fill", "extract", "verify", "scroll", "done"]
- "target": target description or index from page summary (e.g. "[2]", "search input", "save button", or element selector)
- "value": string value if filling/typing/searching (or null)
- "expectation": what to verify or expect (e.g. "results page loaded", "population count", null)

If the goal is to read, find, or extract information from the page, use "extract" action on the relevant element or index without clicking.
If verifying state without modifying, use "verify" action.

Return strictly in valid JSON format:
{
  "goal": "<brief goal summary>",
  "sub_tasks": [
    {"action": "<action>", "target": "<target>", "value": "<value or null>", "expectation": "<expectation>"}
  ]
}
"""


class TaskPlanner:
    def __init__(self, client: Optional[DGXVllmClient] = None):
        self.client = client or DGXVllmClient()

    def create_plan(
        self,
        goal: str,
        page_title: str = "",
        current_url: str = "",
        page_summary: str = "",
        timeout: Optional[float] = None
    ) -> PlanTask:
        """Query Qwen3.8-27B to generate high-level plan.

        Raises TimeoutError('planner_timeout') on timeout.
        Raises ValueError('planner_parse_error') on invalid JSON response.
        """
        user_content = f"GOAL: {goal}\nPAGE TITLE: {page_title}\nPAGE URL: {current_url}\n"
        biz = build_context_block()  # WO-019 M2: บริบทธุรกิจราย-Node (ว่าง = ไม่ผนวก — เดิม 100%)
        if biz:
            user_content += f"\n\nBUSINESS CONTEXT:\n{biz}"
        if page_summary:
            user_content += f"\n\nPAGE CONTEXT & ELEMENTS:\n{page_summary[:5000]}"

        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]

        try:
            # max_tokens ตาม client (โมเดลเล็ก verbose — 4b ตอบยาว → ตัดกลาง JSON = planner_parse_error — WO-019)
            max_tok = getattr(self.client, "default_max_tokens", 500)
            resp = self.client.chat_completion(messages, max_tokens=max_tok, temperature=0.0, timeout=timeout)
        except TimeoutError as te:
            raise TimeoutError("planner_timeout") from te
        except Exception as e:
            if "planner_timeout" in str(e).lower():
                raise TimeoutError("planner_timeout") from e
            raise RuntimeError(f"planner_failed: {e}") from e

        raw = self.client.extract_text_or_json(resp)
        if not raw:
            raise ValueError("planner_empty_response")

        try:
            clean = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
            m = re.search(r"(\{.*\})", clean, re.DOTALL)
            json_str = m.group(1) if m else clean
            data = json.loads(json_str)
            sub_tasks = data.get("sub_tasks", [])
            if not isinstance(sub_tasks, list):
                raise ValueError("planner_invalid_subtasks")
            _ctx = {"planning_ms": resp.get("client_latency_ms", 0)}
            # WO-019 M2/M4 proof: จับสมอง/บริบทที่ใช้จริงรอบนั้น — ใส่เฉพาะเมื่อมีจริง (ไม่มี = รูปเดิม 100%)
            if resp.get("endpoint_used"):
                _ctx["endpoint_used"] = resp["endpoint_used"]
            if biz:
                _ctx["business_context"] = biz[:300]
            return PlanTask(
                task_id=f"plan-{int(resp.get('created', time.time()))}",
                goal=data.get("goal", goal),
                sub_tasks=sub_tasks,
                context=_ctx
            )
        except Exception as e:
            raise ValueError(f"planner_parse_error: {e}") from e

    def replan(
        self,
        goal: str,
        failed_step: Dict[str, Any],
        error: str,
        page_summary: str = "",
        timeout: Optional[float] = None
    ) -> PlanTask:
        """Query Qwen3.8-27B to replan following a step failure."""
        replan_prompt = f"""GOAL: {goal}
PREVIOUS STEP FAILED:
Action: {failed_step.get('action')}
Target: {failed_step.get('target')}
Error: {error}

CURRENT PAGE CONTEXT & ELEMENTS:
{page_summary[:5000]}

Analyze why the previous step failed and generate a revised plan with alternative subtasks to achieve the GOAL."""

        messages = [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": replan_prompt}
        ]

        try:
            resp = self.client.chat_completion(messages, max_tokens=500, temperature=0.0, timeout=timeout)
        except TimeoutError as te:
            raise TimeoutError("planner_timeout") from te
        except Exception as e:
            if "planner_timeout" in str(e).lower():
                raise TimeoutError("planner_timeout") from e
            raise RuntimeError(f"planner_failed: {e}") from e

        raw = self.client.extract_text_or_json(resp)
        try:
            clean = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
            m = re.search(r"(\{.*\})", clean, re.DOTALL)
            json_str = m.group(1) if m else clean
            data = json.loads(json_str)
            return PlanTask(
                task_id=f"replan-{int(resp.get('created', time.time()))}",
                goal=data.get("goal", goal),
                sub_tasks=data.get("sub_tasks", []),
                context={"replan": True, "error": error}
            )
        except Exception as e:
            raise ValueError(f"planner_parse_error: {e}") from e

    def extract_from_page(self, goal: str, page_text: str) -> Dict[str, str]:
        """Extract concise answer and evidence quote directly from page text."""
        prompt = (
            f"PAGE TEXT:\n{page_text[:4000]}\n\n"
            f"Extract the concise answer to the user's GOAL: '{goal}'\n"
            "Return strictly valid JSON with no preamble:\n"
            '{"answer": "<concise answer>", "evidence": "<exact quote under 300 chars>"}'
        )
        try:
            resp = self.client.chat_completion([{"role": "user", "content": prompt}], max_tokens=150, temperature=0.0)
            raw = self.client.extract_text_or_json(resp)
            clean = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
            m = re.search(r"(\{.*\})", clean, re.DOTALL)
            return json.loads(m.group(1) if m else clean)
        except Exception:
            return {"answer": goal, "evidence": page_text[:200]}
