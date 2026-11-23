"""Browser Engine Desktop — Multi-Layer Interactive Automation Edition.
Powered by OneVault Interactive Agent Engine:
- Slow Brain (Planner AI on DGX Qwen3.8-27B)
- 5-Level Interaction Router (Direct API -> DOM -> Accessibility Tree -> Vision -> Windows UIA)
- Conditional Macro Engine with Pre/Post-condition Verification
- Real-Time Human Takeover Mode for CAPTCHAs & Security Challenges
"""
import json
import os
import pathlib
import sys
import time
import shutil
import tempfile
from typing import Any, Callable, Dict, Optional

from playwright.sync_api import sync_playwright

BASE_DIR = pathlib.Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from models.client import DGXVllmClient, load_config
from engine.orchestrator import InteractiveAutomationOrchestrator
from policy.takeover import HumanTakeoverManager

CFG = load_config()

# Global Takeover Manager shared across server sessions
takeover_manager = HumanTakeoverManager()


def run_interactive_task(
    url: str,
    goal: str,
    max_steps: int = 15,
    on_step: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    headless: Optional[bool] = None,
    profile: Optional[str] = None,
    task_id: Optional[str] = None
) -> Dict[str, Any]:
    """Run an autonomous task using the OneVault Interactive Agent Engine.

    Requirement A7: By default, uses an ephemeral profile cleaned up on completion.
    Named profiles (data/profiles/<name>) are only used when explicitly requested.
    """
    is_ephemeral = False
    if profile:
        safe_name = "".join(c for c in profile if c.isalnum() or c in ("-", "_")).strip()
        if not safe_name:
            safe_name = "default_named"
        user_data_dir = BASE_DIR / "data" / "profiles" / safe_name
        user_data_dir.mkdir(parents=True, exist_ok=True)
        is_logged_in_profile = True
    else:
        user_data_dir = pathlib.Path(tempfile.mkdtemp(prefix="browser_ephem_"))
        is_ephemeral = True
        is_logged_in_profile = False

    browser_channel = CFG.get("browser_channel", "chrome")
    is_headless = headless if headless is not None else CFG.get("headless", False)

    orchestrator = InteractiveAutomationOrchestrator(
        takeover_mgr=takeover_manager
    )

    tid = task_id or f"task_{int(time.time())}"
    trace: Dict[str, Any] = {
        "id": tid,
        "url": url,
        "goal": goal,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "running",
        "steps": [],
        "telemetry": {}
    }

    def notify(event_type: str, data: Any):
        if on_step:
            try:
                on_step(event_type, data)
            except Exception:
                pass

    notify("started", trace)

    try:
        with sync_playwright() as p:
            # Requirement A2: Standard clean browser launch without hiding automation flags
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                channel=browser_channel,
                headless=is_headless,
                viewport={"width": 1280, "height": 900},
                args=["--no-sandbox"]
            )

            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            try:
                print(f"[*] Navigating to {url} ...")
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1500)
            except Exception as e:
                print(f"[!] Navigation notice: {e}")

            def on_subtask_step(step_data: Dict[str, Any]):
                trace["steps"].append(step_data)
                notify("step", step_data)

            # Run multi-layer orchestrated automation with risk gate
            res = orchestrator.run_task(
                page=page,
                goal=goal,
                max_subtasks=max_steps,
                on_step_callback=on_subtask_step,
                task_id=tid,
                is_logged_in_profile=is_logged_in_profile
            )

            # RecipeCache: งานจบไม่สำเร็จ (ทุกเส้นทาง) → ลืมสูตรที่เพิ่งใช้ กันแผนพังถูกใช้ซ้ำ
            try:
                from engine.recipe_cache import RecipeCachedPlanner
                if (res.get("ok") is False and isinstance(orchestrator.planner, RecipeCachedPlanner)
                        and orchestrator.planner.last_planned_key
                        and orchestrator.planner.cache.invalidate_by_key(orchestrator.planner.last_planned_key)):
                    print("[*] [RecipeCache] task failed — recipe invalidated")
            except Exception:
                pass

            trace["status"] = res.get("status", "done")
            trace["answer"] = res.get("answer")
            trace["evidence"] = res.get("evidence")
            trace["telemetry"] = res
            trace["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")

            notify("finished", trace)
            ctx.close()
    finally:
        # Requirement A7: Ephemeral profile deleted when task finishes
        if is_ephemeral and user_data_dir.exists():
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            except Exception:
                pass

    return trace

