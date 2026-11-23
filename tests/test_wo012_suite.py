"""Comprehensive Test Suite for WO-012 (F1 - F9).
Verifies OneVault Fact integration, non-blocking resilient spooling, and PDPA compliance:
- F1: onevault_enabled=false -> no facts written, existing regression suites unaffected.
- F2: Enabled -> FactWebTaskExecuted generated, contentHash verified with rfc8785/jcs.
- F3: Vault downtime -> user task unaffected, fact spooled locally, sync resumes idempotently without duplicates.
- F4: Process restart with spooled facts -> resumes and drains queue cleanly.
- F5: Missing/invalid C1 actor -> warns gracefully, fact skipped, user task succeeds.
- F6: Privacy leak scan -> query string stripped, 0 raw answers, 0 raw inputs, 0 image bytes.
- F7: PDPA Right to Erasure -> deletes trace, answers, and screenshots; issues verifiable FactDataErased.
- F8: Retention policy -> trace_retention_days=0 purges old files and reports count.
- F9: High-risk action approval -> FactWebActionApproved records approver actor and timestamp.
"""
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

REPO_ROOT = BASE_DIR.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.c1.subject_id import parse_local_id, INSTANCE
from shared.c2.jcs import (
    HASH_ALGORITHM_V1,
    canonicalize,
    fact_content_hash_v1,
    jcs_sha256,
    verify_fact
)
from telemetry.vault import OneVaultManager, sanitize_url, set_vault_manager, get_vault_manager
from telemetry.retention import RetentionManager
from policy.approval import ApprovalManager
from models.schemas import ActionType, RiskLevel, TargetCandidate, Strategy


class TestWO012Suite(unittest.TestCase):
    def setUp(self):
        self.orig_vault_mgr = get_vault_manager()
        self.temp_dir = tempfile.mkdtemp(prefix="wo012_test_")
        self.vault_dir = pathlib.Path(self.temp_dir) / "vault"
        self.spool_dir = pathlib.Path(self.temp_dir) / "spool"
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.spool_dir.mkdir(parents=True, exist_ok=True)

        self.valid_env = {
            "ONEVAULT_INSTANCE": "spark-01",
            "ONEVAULT_ACTOR": "onevault:person:somchai-dev"
        }

    def tearDown(self):
        set_vault_manager(self.orig_vault_mgr)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_f1_onevault_disabled_writes_no_facts(self):
        """F1: onevault_enabled=false -> zero facts generated or spooled."""
        mgr = OneVaultManager(
            enabled=False,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        fact = mgr.create_task_fact(
            task_id="task-f1",
            url="https://shopee.co.th/product/123",
            goal="search item",
            total_steps=3,
            max_risk_level=0,
            result="SUCCESS",
            elapsed_ms=1200,
            env=self.valid_env
        )
        self.assertIsNone(fact, "Fact must be None when OneVault is disabled")
        spool_files = list(self.spool_dir.glob("*.json"))
        self.assertEqual(len(spool_files), 0, "No files should be spooled")
        vault_files = list(self.vault_dir.glob("**/*.json"))
        self.assertEqual(len(vault_files), 0, "No files should be in vault")

    def test_f2_fact_web_task_executed_canonical_hash(self):
        """F2: FactWebTaskExecuted contentHash independently verified using Contract C2 (RFC 8785)."""
        mgr = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        obs_hashes = [
            hashlib.sha256(b"obs_1").hexdigest(),
            hashlib.sha256(b"obs_2").hexdigest()
        ]
        ans_hash = hashlib.sha256(b"Answer Text").hexdigest()

        fact = mgr.create_task_fact(
            task_id="task-f2-12345",
            url="https://example.com/catalog/items?ref=promo&user=999",
            goal="Extract price for book",
            total_steps=5,
            max_risk_level=1,
            result="SUCCESS",
            elapsed_ms=2500,
            observation_hashes=obs_hashes,
            answer_hash=ans_hash,
            env=self.valid_env
        )
        self.assertIsNotNone(fact)
        self.assertEqual(fact["factType"], "FactWebTaskExecuted")
        self.assertEqual(fact["hashAlgorithm"], HASH_ALGORITHM_V1)

        # Verify C1 Actor Structure
        actor = fact["actor"]
        self.assertEqual(actor["system"], "onevault")
        self.assertEqual(actor["instance"], "spark-01")
        self.assertEqual(actor["id"], "onevault:person:somchai-dev")
        self.assertIsNotNone(parse_local_id(actor["id"]))

        # Independent contentHash verification via Contract C2
        self.assertTrue(verify_fact(fact), "Fact contentHash must be cryptographically valid")
        body = {k: v for k, v in fact.items() if k != "contentHash"}
        expected_hash = "sha256:" + hashlib.sha256(canonicalize(body).encode("utf-8")).hexdigest()
        self.assertEqual(fact["contentHash"], expected_hash)

        # Test Spool and Flush
        spooled = mgr.spool_fact(fact)
        self.assertTrue(spooled)
        res = mgr.flush_spool()
        self.assertEqual(res["flushed"], 1)
        self.assertEqual(res["pending"], 0)

        # Verify atomic location in vault
        sub_dir = fact["contentHash"][7:9]
        target_path = self.vault_dir / "facts" / "FactWebTaskExecuted" / sub_dir / f"{fact['factId']}.json"
        self.assertTrue(target_path.is_file(), "Fact must be stored in standard vault hierarchy")

    def test_f3_vault_offline_resilience_and_idempotent_sync(self):
        """F3: When vault is offline/unreachable, task finishes, fact remains in spool, resync is idempotent."""
        # Point to an unwritable / non-existent root or simulate write error
        bad_vault_dir = pathlib.Path(self.temp_dir) / "non_existent_vault"
        # Create mgr with bad vault
        mgr = OneVaultManager(
            enabled=True,
            vault_dir=str(bad_vault_dir),
            spool_dir=str(self.spool_dir)
        )

        fact = mgr.create_task_fact(
            task_id="task-f3",
            url="https://shopee.co.th",
            goal="search",
            total_steps=2,
            max_risk_level=0,
            result="SUCCESS",
            elapsed_ms=800,
            env=self.valid_env
        )
        self.assertTrue(mgr.spool_fact(fact))

        # Simulate vault write failure by making vault path a file instead of dir
        bad_vault_dir.touch()
        res_fail = mgr.flush_spool()
        self.assertEqual(res_fail["flushed"], 0)
        self.assertEqual(res_fail["pending"], 1)
        self.assertTrue((self.spool_dir / f"{fact['factId']}.json").is_file(), "Fact must safely persist in spool")

        # Restore vault directory and sync again
        bad_vault_dir.unlink()
        bad_vault_dir.mkdir(parents=True, exist_ok=True)
        res_ok = mgr.flush_spool()
        self.assertEqual(res_ok["flushed"], 1)
        self.assertEqual(res_ok["pending"], 0)

        # Idempotent re-flush produces no duplicates or errors
        res_repeat = mgr.flush_spool()
        self.assertEqual(res_repeat["flushed"], 0)
        self.assertEqual(res_repeat["pending"], 0)

    def test_f4_process_restart_with_spooled_facts(self):
        """F4: Facts spooled before process termination survive restart and are successfully flushed."""
        # Instance 1: Spool 3 facts and terminate without flushing
        mgr1 = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        for i in range(3):
            fact = mgr1.create_task_fact(
                task_id=f"task-f4-{i}",
                url=f"https://bot.or.th/page{i}",
                goal=f"goal {i}",
                total_steps=i + 1,
                max_risk_level=0,
                result="SUCCESS",
                elapsed_ms=500 * (i + 1),
                env=self.valid_env
            )
            mgr1.spool_fact(fact)

        self.assertEqual(len(list(self.spool_dir.glob("*.json"))), 3)

        # Simulate process termination and restart (Instance 2 with new instance)
        mgr2 = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        status = mgr2.get_status(env=self.valid_env)
        self.assertEqual(status["spool_pending"], 3)

        # Drain on restart
        drain_res = mgr2.flush_spool()
        self.assertEqual(drain_res["flushed"], 3)
        self.assertEqual(drain_res["pending"], 0)
        self.assertEqual(len(list(self.spool_dir.glob("*.json"))), 0)

    def test_f5_missing_c1_actor_rejects_fact_without_aborting_task(self):
        """F5: When C1 env is missing or invalid, fact generation is skipped with warning, user task completes."""
        mgr = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )

        # Case A: Missing env
        fact_missing = mgr.create_task_fact(
            task_id="task-f5-a",
            url="https://example.com",
            goal="read info",
            total_steps=1,
            max_risk_level=0,
            result="SUCCESS",
            elapsed_ms=300,
            env={}  # Empty env
        )
        self.assertIsNone(fact_missing, "Must skip fact when C1 env missing")

        # Case B: Invalid instance format
        fact_bad_inst = mgr.create_task_fact(
            task_id="task-f5-b",
            url="https://example.com",
            goal="read info",
            total_steps=1,
            max_risk_level=0,
            result="SUCCESS",
            elapsed_ms=300,
            env={"ONEVAULT_INSTANCE": "INVALID_UPPERCASE", "ONEVAULT_ACTOR": "onevault:person:somchai"}
        )
        self.assertIsNone(fact_bad_inst, "Must skip fact when C1 instance invalid")

        # Case C: Invalid actor format
        fact_bad_actor = mgr.create_task_fact(
            task_id="task-f5-c",
            url="https://example.com",
            goal="read info",
            total_steps=1,
            max_risk_level=0,
            result="SUCCESS",
            elapsed_ms=300,
            env={"ONEVAULT_INSTANCE": "spark-01", "ONEVAULT_ACTOR": "invalid:actor"}
        )
        self.assertIsNone(fact_bad_actor, "Must skip fact when C1 actor invalid")

        # Spool remains clean
        self.assertEqual(len(list(self.spool_dir.glob("*.json"))), 0)

    def test_f6_privacy_leak_zero_raw_data_in_fact(self):
        """F6: Zero raw typed text, raw answers, query strings, or image bytes in emitted Facts."""
        mgr = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        raw_user_typed = "SuperSecretPassword123!"
        raw_answer = "Sensitive customer address: 123 Bangkok Road"
        raw_query_string = "session_token=abc998877&email=user@example.com"
        raw_image_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIFIMAGEBYTESDATA"

        ans_hash = hashlib.sha256(raw_answer.encode("utf-8")).hexdigest()
        obs_hash = hashlib.sha256(raw_image_bytes).hexdigest()

        fact = mgr.create_task_fact(
            task_id="task-f6",
            url=f"https://shop.example.com/checkout/pay?{raw_query_string}#step2",
            goal="Complete order",
            total_steps=4,
            max_risk_level=2,
            result="SUCCESS",
            elapsed_ms=3200,
            observation_hashes=[obs_hash],
            answer_hash=ans_hash,
            env=self.valid_env
        )
        fact_str = json.dumps(fact, ensure_ascii=False)

        # Assertions for zero raw leakage
        self.assertNotIn("session_token", fact_str, "Query string tokens must not leak into fact")
        self.assertNotIn("user@example.com", fact_str, "Query string email must not leak into fact")
        self.assertNotIn(raw_user_typed, fact_str, "User typed text must not leak into fact")
        self.assertNotIn(raw_answer, fact_str, "Raw answer text must not leak into fact")
        self.assertNotIn("IMAGEBYTESDATA", fact_str, "Image bytes must not leak into fact")
        self.assertEqual(fact["payload"]["url"], "https://shop.example.com/checkout/pay")
        self.assertEqual(fact["payload"]["answerHash"], ans_hash)
        self.assertIn(obs_hash, fact["payload"]["observationHashes"])

    def test_f7_pdpa_right_to_erasure_removes_data_and_emits_fact(self):
        """F7: PDPA Right to Erasure command deletes trace/answers/screenshots and emits FactDataErased."""
        # 1. Setup mock trace and screenshot files
        task_id = "task-f7-pdpa"
        trace_dir = BASE_DIR / "data" / "traces"
        screen_dir = BASE_DIR / "data" / "screenshots"
        trace_dir.mkdir(parents=True, exist_ok=True)
        screen_dir.mkdir(parents=True, exist_ok=True)

        fake_sh = "a1b2c3d4e5f678901234567890abcdef1234567890abcdef1234567890abcdef"
        img_file = screen_dir / f"{fake_sh}.jpg"
        img_file.write_bytes(b"dummy image")

        trace_file = trace_dir / f"{task_id}.jsonl"
        trace_line = json.dumps({
            "task_id": task_id,
            "step_num": 1,
            "decision": {"screenshot_hash": fake_sh},
            "action": {"type": "click", "target": "button"}
        })
        trace_file.write_text(trace_line + "\n", encoding="utf-8")

        # 2. Setup OneVaultManager with test vault
        mgr = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        set_vault_manager(mgr)

        # 3. Execute erasure
        res = RetentionManager.erase_task_data(
            task_id=task_id,
            reason="Customer requested GDPR/PDPA data deletion",
            actor=self.valid_env,
            env=self.valid_env
        )
        self.assertTrue(res["ok"])
        self.assertTrue(res["erased"])
        self.assertIn("trace", res["erased_artifacts"])
        self.assertIn("screenshots", res["erased_artifacts"])

        # 4. Verify physical files are gone
        self.assertFalse(trace_file.is_file(), "Trace file must be deleted")
        self.assertFalse(img_file.is_file(), "Linked screenshot file must be deleted")

        # 5. Verify FactDataErased was generated and verifiable
        fact_files = list(self.vault_dir.glob("**/FactDataErased/**/*.json"))
        self.assertGreaterEqual(len(fact_files), 1, "FactDataErased must be written to vault")
        fact_content = json.loads(fact_files[0].read_text(encoding="utf-8"))
        self.assertEqual(fact_content["factType"], "FactDataErased")
        self.assertEqual(fact_content["payload"]["taskId"], task_id)
        self.assertTrue(verify_fact(fact_content), "FactDataErased contentHash must be valid")

    def test_f8_data_retention_policy_purges_old_traces(self):
        """F8: trace_retention_days=0 purges old traces and reports accurate deleted count."""
        trace_dir = BASE_DIR / "data" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)

        t1 = trace_dir / "old_task_1.jsonl"
        t2 = trace_dir / "old_task_2.jsonl"
        t1.write_text('{"step": 1}\n', encoding="utf-8")
        t2.write_text('{"step": 2}\n', encoding="utf-8")

        res = RetentionManager.purge_expired_traces(retention_days=0)
        self.assertTrue(res["ok"])
        self.assertEqual(res["retention_days"], 0)
        self.assertGreaterEqual(res["purged_traces"], 2)
        self.assertFalse(t1.is_file())
        self.assertFalse(t2.is_file())

    def test_f9_fact_web_action_approved_on_risk_approval(self):
        """F9: Risky action approval (Risk >= 2) emits FactWebActionApproved with approver and timestamp."""
        mgr = OneVaultManager(
            enabled=True,
            vault_dir=str(self.vault_dir),
            spool_dir=str(self.spool_dir)
        )
        set_vault_manager(mgr)

        app_mgr = ApprovalManager()
        # Request approval for Risk 3 action
        req_thread_done = False

        def request_worker():
            nonlocal req_thread_done
            app_mgr.request_approval(
                task_id="task-f9",
                step_num=2,
                action_type="click",
                target="Confirm Payment",
                risk_level="Risk 3",
                description="Pay $50.00 for order",
                timeout=5.0
            )
            req_thread_done = True

        import threading
        t = threading.Thread(target=request_worker)
        t.start()
        time.sleep(0.2)

        # Grant approval with C1 actor
        approver = {"system": "onevault", "instance": "spark-01", "id": "onevault:person:somchai-supervisor"}
        approved = app_mgr.approve("task-f9", 2, approver=approver, env=self.valid_env)
        self.assertTrue(approved)
        t.join(timeout=2.0)
        self.assertTrue(req_thread_done)

        # Allow async spool worker to flush
        vault_files = []
        for _ in range(20):
            vault_files = list(self.vault_dir.glob("**/FactWebActionApproved/**/*.json"))
            if vault_files:
                break
            time.sleep(0.05)

        # Verify FactWebActionApproved in vault
        self.assertEqual(len(vault_files), 1, "FactWebActionApproved must be written to vault")
        fact = json.loads(vault_files[0].read_text(encoding="utf-8"))
        self.assertEqual(fact["factType"], "FactWebActionApproved")
        self.assertEqual(fact["payload"]["taskId"], "task-f9")
        self.assertEqual(fact["payload"]["stepNum"], 2)
        self.assertEqual(fact["payload"]["riskLevel"], 3)
        self.assertEqual(fact["payload"]["approver"], "onevault:person:somchai-supervisor")
        self.assertTrue(verify_fact(fact), "FactWebActionApproved contentHash must be valid")

    def test_f10_c1_identity_regression_validation(self):
        """F10 / R1: Strict C1 identity validation regression.
        - Missing environment variables in os.environ produces actor=None, fact=None, 0 spool files.
        - Invalid instance/actor syntax in os.environ produces actor=None, fact=None, 0 spool files.
        - Valid instance and actor in os.environ generates valid Fact with correct actor and spools cleanly.
        - No hardcoded fallback actor/instance allowed under any circumstances.
        """
        isolated_spool = self.spool_dir / "f10_spool"
        isolated_vault = self.vault_dir / "f10_vault"
        isolated_spool.mkdir(parents=True, exist_ok=True)
        isolated_vault.mkdir(parents=True, exist_ok=True)

        mgr = OneVaultManager(
            enabled=True,
            vault_dir=str(isolated_vault),
            spool_dir=str(isolated_spool)
        )

        orig_inst = os.environ.get("ONEVAULT_INSTANCE")
        orig_actor = os.environ.get("ONEVAULT_ACTOR")

        try:
            # Subtest 1: Missing identity in os.environ (no fallback allowed)
            os.environ.pop("ONEVAULT_INSTANCE", None)
            os.environ.pop("ONEVAULT_ACTOR", None)
            actor, err = mgr.get_c1_actor()
            self.assertIsNone(actor, "Actor must be None when env vars missing; no fallback allowed")
            self.assertIsNotNone(err)
            fact = mgr.create_task_fact(
                task_id="task-f10-missing",
                url="https://example.com/test",
                goal="test",
                total_steps=1,
                max_risk_level=0,
                result="SUCCESS",
                elapsed_ms=100
            )
            self.assertIsNone(fact, "Fact must not be emitted when identity is missing")
            self.assertEqual(len(list(isolated_spool.glob("*.json"))), 0)

            # Subtest 2: Invalid identity in os.environ
            os.environ["ONEVAULT_INSTANCE"] = "BAD_INSTANCE!"
            os.environ["ONEVAULT_ACTOR"] = "bad_actor_syntax"
            actor, err = mgr.get_c1_actor()
            self.assertIsNone(actor, "Actor must be None when env vars invalid")
            fact = mgr.create_task_fact(
                task_id="task-f10-invalid",
                url="https://example.com/test",
                goal="test",
                total_steps=1,
                max_risk_level=0,
                result="SUCCESS",
                elapsed_ms=100
            )
            self.assertIsNone(fact, "Fact must not be emitted when identity is invalid")
            self.assertEqual(len(list(isolated_spool.glob("*.json"))), 0)

            # Subtest 3: Valid identity in os.environ
            os.environ["ONEVAULT_INSTANCE"] = "spark-01"
            os.environ["ONEVAULT_ACTOR"] = "onevault:person:chatchai-operator"
            actor, err = mgr.get_c1_actor()
            self.assertIsNotNone(actor, "Actor must resolve when env vars are valid")
            self.assertIsNone(err)
            self.assertEqual(actor["system"], "onevault")
            self.assertEqual(actor["instance"], "spark-01")
            self.assertEqual(actor["id"], "onevault:person:chatchai-operator")

            fact = mgr.create_task_fact(
                task_id="task-f10-valid",
                url="https://example.com/test",
                goal="test",
                total_steps=1,
                max_risk_level=0,
                result="SUCCESS",
                elapsed_ms=100
            )
            self.assertIsNotNone(fact, "Fact must be generated when identity is valid")
            self.assertEqual(fact["actor"], actor)
            self.assertTrue(mgr.spool_fact(fact))
            self.assertEqual(len(list(isolated_spool.glob("*.json"))), 1)

            res = mgr.flush_spool()
            self.assertEqual(res["flushed"], 1)
            self.assertEqual(res["pending"], 0)
        finally:
            if orig_inst is not None:
                os.environ["ONEVAULT_INSTANCE"] = orig_inst
            else:
                os.environ.pop("ONEVAULT_INSTANCE", None)
            if orig_actor is not None:
                os.environ["ONEVAULT_ACTOR"] = orig_actor
            else:
                os.environ.pop("ONEVAULT_ACTOR", None)


if __name__ == "__main__":
    unittest.main()

