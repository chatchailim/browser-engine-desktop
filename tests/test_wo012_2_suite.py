"""Comprehensive Test Suite for WO-012.2 (Non-blocking Spool, Daemon Flushing, Queue Integrity).
Verifies:
- H1: Orchestrator completion with hanging destination (4.0s sleep) returns in < 1.0s.
- H2: Risk action approval with hanging destination (4.0s sleep) returns in < 1.0s.
- H3: PDPA erase with hanging destination (4.0s sleep) returns in < 1.0s with FactDataErased queued in spool.
- H4: Spool queue integrity: zero loss, zero duplication across timeouts; cleanly drains upon vault recovery.
- H5: /api/vault/sync manual retry ceiling timeout prevents indefinite hangs and reports pending count.
"""
import glob
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import unittest
from typing import Any, Dict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent.parent
for p in (str(BASE_DIR), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import telemetry.retention as ret_mod
import telemetry.vault as vault_mod
from telemetry.retention import RetentionManager
from telemetry.vault import OneVaultManager, set_vault_manager, get_vault_manager
from policy.approval import ApprovalManager
from engine.orchestrator import InteractiveAutomationOrchestrator


class TestWO0122Suite(unittest.TestCase):
    def setUp(self):
        self.test_dir = pathlib.Path(tempfile.mkdtemp(prefix="wo012_2_test_"))
        self.vault_dir = self.test_dir / "vault"
        self.spool_dir = self.test_dir / "spool"
        self.traces_dir = self.test_dir / "traces"
        self.shots_dir = self.test_dir / "shots"
        self.downloads_dir = self.test_dir / "downloads"

        for d in (self.vault_dir, self.spool_dir, self.traces_dir, self.shots_dir, self.downloads_dir):
            d.mkdir(parents=True, exist_ok=True)

        self.orig_traces = ret_mod.TRACES_DIR
        self.orig_shots = ret_mod.SCREENSHOTS_DIR
        self.orig_dls = ret_mod.DOWNLOADS_DIR

        ret_mod.TRACES_DIR = self.traces_dir
        ret_mod.SCREENSHOTS_DIR = self.shots_dir
        ret_mod.DOWNLOADS_DIR = self.downloads_dir

        self.orig_env = dict(os.environ)
        os.environ["ONEVAULT_INSTANCE"] = "spark-01"
        os.environ["ONEVAULT_ACTOR"] = "onevault:person:somchai-dev"

        self.valid_env = {
            "ONEVAULT_INSTANCE": "spark-01",
            "ONEVAULT_ACTOR": "onevault:person:somchai-dev"
        }

        self.orig_vault_mgr = get_vault_manager()
        # Ensure clean isolated spool directory for each test
        for p in self.spool_dir.glob("*.json"):
            try:
                p.unlink()
            except OSError:
                pass

        self.mgr = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        set_vault_manager(self.mgr)

    def tearDown(self):
        ret_mod.TRACES_DIR = self.orig_traces
        ret_mod.SCREENSHOTS_DIR = self.orig_shots
        ret_mod.DOWNLOADS_DIR = self.orig_dls
        os.environ.clear()
        os.environ.update(self.orig_env)
        set_vault_manager(self.orig_vault_mgr)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_h1_orchestrator_completion_with_hanging_destination(self):
        """H1: Replacing _do_flush with 4.0s sleep -> task completion returns in < 1.0s."""
        SLEEP = 4.0
        self.mgr._do_flush = lambda: (time.sleep(SLEEP), {"flushed": 0, "failed": 0, "pending": 1})[1]  # type: ignore

        orch = InteractiveAutomationOrchestrator()
        mock_page = type("MockPage", (), {
            "url": "https://example.com/page",
            "title": lambda self: "Test Title",
            "evaluate": lambda self, s, *a: [],
            "snapshot": lambda self, **kw: {}
        })()
        orch.planner.create_plan = lambda **kw: type("MockPlan", (), {"sub_tasks": []})()

        t0 = time.perf_counter()
        res = orch.run_task(page=mock_page, goal="Test Goal", task_id="task-h1")
        elapsed = time.perf_counter() - t0

        self.assertTrue(res.get("ok"), f"run_task should succeed: {res}")
        self.assertLess(elapsed, 1.0, f"Orchestrator completion blocked for {elapsed:.3f}s (must be < 1.0s)")

    def test_h2_approval_path_with_hanging_destination(self):
        """H2: Replacing _do_flush with 4.0s sleep -> approval returns in < 1.0s."""
        SLEEP = 4.0
        self.mgr._do_flush = lambda: (time.sleep(SLEEP), {"flushed": 0, "failed": 0, "pending": 1})[1]  # type: ignore

        am = ApprovalManager()
        req_thread_done = False

        def req_worker():
            nonlocal req_thread_done
            am.request_approval(
                task_id="task-h2",
                step_num=1,
                action_type="click",
                target="Confirm Order",
                risk_level="Risk 2",
                description="Place $100 order",
                timeout=5.0
            )
            req_thread_done = True

        t = threading.Thread(target=req_worker)
        t.start()
        time.sleep(0.1)

        approver = {"system": "onevault", "instance": "spark-01", "id": "onevault:person:somchai-supervisor"}

        t0 = time.perf_counter()
        approved = am.approve("task-h2", 1, approver=approver, env=self.valid_env)
        elapsed = time.perf_counter() - t0

        t.join(timeout=2.0)
        self.assertTrue(approved)
        self.assertTrue(req_thread_done)
        self.assertLess(elapsed, 1.0, f"Approval blocked for {elapsed:.3f}s (must be < 1.0s)")

    def test_h3_pdpa_erase_with_hanging_destination(self):
        """H3: PDPA erase with 4.0s sleep destination returns in < 1.0s and emits FactDataErased to spool."""
        SLEEP = 4.0
        self.mgr._do_flush = lambda: (time.sleep(SLEEP), {"flushed": 0, "failed": 0, "pending": 1})[1]  # type: ignore

        trace_file = self.traces_dir / "task-h3.jsonl"
        trace_file.write_text('{"event": "start"}\n', encoding="utf-8")

        t0 = time.perf_counter()
        res = RetentionManager.erase_task_data("task-h3", env=self.valid_env)
        elapsed = time.perf_counter() - t0

        self.assertTrue(res.get("ok"), f"PDPA erase should succeed: {res}")
        self.assertFalse(trace_file.exists(), "Trace file should have been deleted")
        self.assertLess(elapsed, 1.0, f"PDPA erase blocked for {elapsed:.3f}s (must be < 1.0s)")

        # Verify FactDataErased was written into spool
        spool_files = list(self.spool_dir.glob("*.json"))
        self.assertGreaterEqual(len(spool_files), 1, "FactDataErased must be present in spool")
        spool_fact = json.loads(spool_files[0].read_text(encoding="utf-8"))
        self.assertEqual(spool_fact["factType"], "FactDataErased")
        self.assertEqual(spool_fact["payload"]["taskId"], "task-h3")

    def test_h4_spool_queue_integrity_and_recovery_drain(self):
        """H4: Spooled facts remain intact without loss or duplicate; cleanly drain when vault recovers."""
        # 1. Spool multiple facts
        fact1 = self.mgr.create_task_fact(
            task_id="task-h4-1", url="https://example.com/1",
            goal="Goal 1", total_steps=1, max_risk_level=0, result="completed", elapsed_ms=100,
            env=self.valid_env
        )
        fact2 = self.mgr.create_task_fact(
            task_id="task-h4-2", url="https://example.com/2",
            goal="Goal 2", total_steps=2, max_risk_level=1, result="completed", elapsed_ms=200,
            env=self.valid_env
        )
        self.mgr.spool_fact(fact1)
        self.mgr.spool_fact(fact2)

        # 2. Timeout flush: destination hangs
        self.mgr._do_flush = lambda: (time.sleep(2.0), {"flushed": 0, "failed": 0, "pending": 2})[1]  # type: ignore
        res_timeout = self.mgr.flush_spool(timeout=0.1)
        self.assertEqual(res_timeout["flushed"], 0)
        self.assertEqual(res_timeout["pending"], 2)

        # Confirm queue still has exactly 2 files (no loss)
        spool_files = list(self.spool_dir.glob("*.json"))
        self.assertEqual(len(spool_files), 2, "Facts must not be lost upon timeout")

        # 3. Vault recovers: restore normal _do_flush
        orig_mgr = OneVaultManager(enabled=True, vault_dir=str(self.vault_dir), spool_dir=str(self.spool_dir))
        self.mgr._do_flush = orig_mgr._do_flush

        res_recovered = self.mgr.flush_spool()
        self.assertEqual(res_recovered["flushed"], 2)
        self.assertEqual(res_recovered["pending"], 0)
        self.assertEqual(len(list(self.spool_dir.glob("*.json"))), 0, "Spool must be empty after successful flush")

        # Confirm exactly 2 distinct files in vault
        vault_files = list(self.vault_dir.glob("**/FactWebTaskExecuted/**/*.json"))
        self.assertEqual(len(vault_files), 2)
        fact_ids = {json.loads(f.read_text(encoding="utf-8"))["factId"] for f in vault_files}
        self.assertEqual(len(fact_ids), 2, "No duplicate fact IDs allowed in vault")

        # Idempotent re-flush
        res_reflush = self.mgr.flush_spool()
        self.assertEqual(res_reflush["flushed"], 0)
        self.assertEqual(res_reflush["pending"], 0)

    def test_h5_api_vault_sync_timeout_ceiling(self):
        """H5: /api/vault/sync with hanging destination respects timeout ceiling and reports pending."""
        # Ensure isolated spool contains only the test fact
        for p in self.spool_dir.glob("*.json"):
            p.unlink(missing_ok=True)
        fact = self.mgr.create_task_fact(
            task_id="task-h5", url="https://example.com/5",
            goal="Goal 5", total_steps=1, max_risk_level=0, result="completed", elapsed_ms=50,
            env=self.valid_env
        )
        self.mgr.spool_fact(fact)

        SLEEP = 3.0
        self.mgr._do_flush = lambda: (time.sleep(SLEEP), {"flushed": 0, "failed": 0, "pending": 1})[1]  # type: ignore

        t0 = time.perf_counter()
        res = self.mgr.flush_spool(timeout=0.5)
        elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 1.0, f"flush_spool timeout ceiling violated: took {elapsed:.3f}s")
        self.assertEqual(res["flushed"], 0)
        self.assertEqual(res["failed"], 1)
        self.assertEqual(res["pending"], 1)


if __name__ == "__main__":
    unittest.main()
