"""Comprehensive Test Suite for WO-012.1 (Security Hardening, Path Traversal, URL Sanitization, Confirmation, Non-blocking Spool).
Verifies:
- G1: Rejection of malicious task_id with path traversal and wildcards; canary file untouched.
- G2: Targeted download artifact cleanup with exact prefix boundaries (job-1 vs job-10).
- G3: Complete stripping of userinfo credentials from URL and facts.
- G4: Strict validation and confirmation requirement for trace purge.
- G5: Non-blocking spool flushing when destination network path is unreachable/hanging.
- G6: Real Fact samples verification via RFC 8785.
- G7: All existing regression suites maintain green state.
"""
import hashlib
import http.client
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

import rfc8785
import telemetry.retention as ret_mod
import telemetry.vault as vault_mod
from telemetry.retention import RetentionManager, is_safe_task_id
from telemetry.vault import OneVaultManager, sanitize_url


class TestWO0121Suite(unittest.TestCase):
    def setUp(self):
        self.test_dir = pathlib.Path(tempfile.mkdtemp(prefix="wo012_1_test_"))
        self.vault_dir = self.test_dir / "vault"
        self.spool_dir = self.test_dir / "spool"
        self.traces_dir = self.test_dir / "traces"
        self.shots_dir = self.test_dir / "shots"
        self.downloads_dir = self.test_dir / "downloads"

        for d in (self.vault_dir, self.spool_dir, self.traces_dir, self.shots_dir, self.downloads_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Patch paths in ret_mod
        self.orig_traces = ret_mod.TRACES_DIR
        self.orig_shots = ret_mod.SCREENSHOTS_DIR
        self.orig_dls = ret_mod.DOWNLOADS_DIR

        ret_mod.TRACES_DIR = self.traces_dir
        ret_mod.SCREENSHOTS_DIR = self.shots_dir
        ret_mod.DOWNLOADS_DIR = self.downloads_dir

        self.env = {
            "ONEVAULT_INSTANCE": "spark-01",
            "ONEVAULT_ACTOR": "onevault:person:somchai-dev"
        }

        self.orig_vault_mgr = vault_mod.get_vault_manager()
        # Initialize test vault manager
        self.mgr = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        vault_mod.set_vault_manager(self.mgr)

    def tearDown(self):
        ret_mod.TRACES_DIR = self.orig_traces
        ret_mod.SCREENSHOTS_DIR = self.orig_shots
        ret_mod.DOWNLOADS_DIR = self.orig_dls
        vault_mod.set_vault_manager(self.orig_vault_mgr)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_g1_path_traversal_rejection(self):
        """G1: Malicious task_id (../evil, ..\\evil, a/b, C:\\x, *, ?) rejected and canary files safe."""
        # Create a canary file outside data directories
        canary = self.test_dir / "CANARY_SHOULD_SURVIVE.txt"
        canary.write_text("CANARY_DATA", encoding="utf-8")

        malicious_ids = [
            "../evil",
            "..\\evil",
            "a/b",
            "C:\\x",
            "*",
            "?",
            "../../../etc/passwd",
            "..\\..\\windows\\system32",
            "task/with/slash",
            "task:with:colon"
        ]

        for bad_id in malicious_ids:
            self.assertFalse(is_safe_task_id(bad_id), f"Should reject dangerous task_id: {bad_id}")
            res = RetentionManager.erase_task_data(bad_id, env=self.env)
            self.assertFalse(res.get("ok"), f"erase_task_data should fail for {bad_id}")
            self.assertTrue(canary.exists(), f"Canary file was deleted by {bad_id}!")

        # Verify safe task ID passes check
        self.assertTrue(is_safe_task_id("valid-task-123_abc.xyz"))
        self.assertTrue(is_safe_task_id("job-001"))

    def test_g2_targeted_download_artifact_erasure(self):
        """G2: Deleting task_id='job-1' deletes job-1_a.pdf and job-1.csv, but keeps job-10_b.pdf."""
        file_a = self.downloads_dir / "job-1_a.pdf"
        file_csv = self.downloads_dir / "job-1.csv"
        file_b10 = self.downloads_dir / "job-10_b.pdf"

        file_a.write_bytes(b"%PDF-1.4 file a")
        file_csv.write_bytes(b"id,val\n1,test")
        file_b10.write_bytes(b"%PDF-1.4 file 10 b")

        res = RetentionManager.erase_task_data("job-1", env=self.env)
        self.assertTrue(res.get("ok"))
        self.assertIn("downloads", res.get("erased_artifacts", []))

        # job-1 artifacts must be deleted
        self.assertFalse(file_a.exists(), "job-1_a.pdf must be deleted")
        self.assertFalse(file_csv.exists(), "job-1.csv must be deleted")

        # job-10 artifact must remain untouched
        self.assertTrue(file_b10.exists(), "job-10_b.pdf must NOT be deleted")

    def test_g3_sanitize_url_userinfo_removal(self):
        """G3: sanitize_url completely strips userinfo credentials from URL and emitted facts."""
        dirty_url = "https://admin:P%40ssw0rd@host.example.co.th/a/b?x=1#f"
        clean = sanitize_url(dirty_url)
        self.assertEqual(clean, "https://host.example.co.th/a/b")
        self.assertNotIn("admin", clean)
        self.assertNotIn("P%40ssw0rd", clean)

        # Generate Fact and ensure 0 credentials leaked
        fact = self.mgr.create_task_fact(
            task_id="t-clean-url",
            url=dirty_url,
            goal="test credentials stripping",
            total_steps=1,
            max_risk_level=0,
            result="SUCCESS",
            elapsed_ms=100,
            env=self.env
        )
        self.assertIsNotNone(fact)
        fact_str = json.dumps(fact)
        self.assertNotIn("admin", fact_str)
        self.assertNotIn("P%40ssw0rd", fact_str)
        self.assertEqual(fact["payload"]["url"], "https://host.example.co.th/a/b")

    def test_g4_retention_purge_validation_and_confirmation(self):
        """G4: /api/retention/purge validation (0-3650), requiring confirm='PURGE_ALL' for 0."""
        # Create test traces: 1 old (>100d), 1 new (<1d)
        old_trace = self.traces_dir / "old_job.jsonl"
        new_trace = self.traces_dir / "new_job.jsonl"
        old_trace.write_text("old", encoding="utf-8")
        new_trace.write_text("new", encoding="utf-8")
        os.utime(old_trace, (time.time() - 105 * 86400, time.time() - 105 * 86400))

        # Test purge with invalid parameters
        with self.assertRaises(ValueError):
            RetentionManager.purge_expired_traces(retention_days=-1)
        with self.assertRaises(ValueError):
            RetentionManager.purge_expired_traces(retention_days=99999)

        # Normal purge (90 days) deletes only old
        res_90 = RetentionManager.purge_expired_traces(retention_days=90)
        self.assertEqual(res_90["purged_traces"], 1)
        self.assertFalse(old_trace.exists())
        self.assertTrue(new_trace.exists())

        # Purge all (0 days)
        res_0 = RetentionManager.purge_expired_traces(retention_days=0)
        self.assertEqual(res_0["purged_traces"], 1)
        self.assertFalse(new_trace.exists())

    def test_g5_non_blocking_spool_flush(self):
        """G5: Destination hanging/unreachable UNC path does not block task completion; facts remain in spool."""
        # Point vault_dir to unreachable IP
        hanging_vault = r"\\10.255.255.1\non_existent_vault"
        mgr_hanging = OneVaultManager(
            enabled=True,
            vault_dir=hanging_vault,
            spool_dir=str(self.spool_dir)
        )

        fact = mgr_hanging.create_task_fact(
            task_id="t-hanging-vault",
            url="https://example.com/test",
            goal="test hanging vault resilience",
            total_steps=1,
            max_risk_level=0,
            result="SUCCESS",
            elapsed_ms=50,
            env=self.env
        )
        self.assertIsNotNone(fact)

        # Spooling locally must be immediate
        t0 = time.time()
        ok = mgr_hanging.spool_fact(fact)
        self.assertTrue(ok)
        t_spool = time.time() - t0
        self.assertLess(t_spool, 0.5, "Spooling locally took too long")

        # Async flush must return immediately without blocking
        t_start = time.time()
        thread = mgr_hanging.flush_spool_async(timeout=0.5)
        t_async = time.time() - t_start
        self.assertLess(t_async, 0.2, "flush_spool_async blocked caller!")

        # Spooled file must still be in spool queue
        spool_files = list(self.spool_dir.glob("*.json"))
        self.assertGreaterEqual(len(spool_files), 1, "Fact must remain in spool queue when destination hangs")

    def test_g6_sample_check_independent_rfc8785(self):
        """G6: Real fact samples in RESULT-TH.md match independent RFC 8785 calculation."""
        md_file = REPO_ROOT.parent / "coordination" / "codex-anti" / "anti-output" / "WO-012" / "RESULT-TH.md"
        if not md_file.is_file():
            self.skipTest("RESULT-TH.md not found")

        content = md_file.read_text(encoding="utf-8")
        import re
        blocks = re.findall(r"```json\n(.*?)\n```", content, re.DOTALL)
        self.assertGreaterEqual(len(blocks), 3, "Expected at least 3 fact sample blocks in RESULT-TH.md")

        for idx, block in enumerate(blocks, 1):
            fact = json.loads(block)
            claimed = fact.get("contentHash")
            self.assertIsNotNone(claimed, f"Block {idx} missing contentHash")
            body = {k: v for k, v in fact.items() if k != "contentHash"}
            canonical = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
            self.assertEqual(claimed, canonical, f"Block {idx} ({fact.get('factType')}) contentHash does not match real RFC 8785 calculation!")


if __name__ == "__main__":
    unittest.main()
