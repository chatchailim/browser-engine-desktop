"""WO-017.4 Negative Test Suite and Fault Injection Assertions.
Covers requirements R1 through R5 per WO-017.4:
- R1: Complete cursor sample verification (intermediate movement, intermediate None, (0,0), None, single-axis zero, multi-monitor negative)
- R2: Strict evaluator enforcement on all acceptance criteria:
  * Round B rejected clicks, server clicks, clean stop
  * Round D pre-approval requests, gate pending counters, GET/POST risk levels, idempotency key
  * Round C takeover/resume proof
  * Fact payload presence, taskId mapping to run, RFC 8785 contentHash, manifest mapping
- R3 / R4: Real production persistence and multi-run isolation:
  * Calling production run() in sandbox fixture
  * Two consecutive runs: Run 1 files remain 100% hash-identical after Run 2
  * Duplicate run_id rejection before mutation
- R5: Exception safety & baseline restoration:
  * Injected setup exception after config write: config, env, and vault manager restored
  * Injected mid-run exception: config, env, and vault manager restored
  * Sentinel preservation under exceptions
"""
import copy
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import rfc8785

from tests import rehearsal_runner
from tests.rehearsal_runner import (
    evaluate_round_e_status,
    evaluate_overall_rehearsal,
    safe_cleanup_run,
    restore_config,
    rehearsal_execution_context,
    persist_run_artifacts,
    is_valid_cursor_pos,
    BASE_DIR,
    CONFIG_FILE,
    ORIGINAL_CONFIG_BYTES,
)
from telemetry.vault import OneVaultManager, set_vault_manager, get_vault_manager


class Server:
    def shutdown(self):
        pass


def make_canonical_fact(fact_id: str, fact_type: str, task_id: str) -> dict:
    if fact_type == "FactApiCallExecuted":
        payload = {
            "taskId": task_id,
            "stepNum": 1,
            "recipeName": "finance_transfer",
            "host": "127.0.0.1:18822",
            "path": "/api/v1/finance/transfer",
            "method": "POST",
            "statusCode": 200,
            "elapsedMs": 120,
            "riskLevel": 3,
            "requestBodyHash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "responseBodyHash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "insecureHttp": False
        }
    elif fact_type == "FactWebActionApproved":
        payload = {
            "taskId": task_id,
            "stepNum": 1,
            "actionType": "click",
            "target": "#approve-btn",
            "riskLevel": 3,
            "approvedAt": "2026-09-25T04:00:00Z",
            "approver": "onevault:agent:rehearsal-01"
        }
    elif fact_type == "FactDesktopWindowRead":
        payload = {
            "taskId": task_id,
            "processName": "notepad.exe",
            "windowTitleHash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "elementCount": 10,
            "elapsedMs": 50,
            "structureHash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        }
    elif fact_type == "FactWebTaskExecuted":
        payload = {
            "taskId": task_id,
            "url": "https://books.toscrape.com",
            "goal": "Rehearsal task execution",
            "totalSteps": 1,
            "maxRiskLevel": 0,
            "elapsedMs": 250,
            "result": "success",
            "observationHashes": ["sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
            "answerHash": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        }
    else:
        payload = {
            "taskId": task_id,
            "summary": "canonical_fact_for_test",
            "sampleCount": 42
        }
    fact_json = {
        "factId": fact_id,
        "factType": fact_type,
        "actor": "onevault:agent:rehearsal-01",
        "subjectId": f"subject:{task_id}",
        "occurredAt": "2026-09-25T04:00:00Z",
        "payload": payload,
        "hashAlgorithm": "sha256"
    }
    body = {k: v for k, v in fact_json.items() if k != "contentHash"}
    h = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
    fact_json["contentHash"] = h
    return {
        "file": f"facts/{fact_type}_{fact_id}.json",
        "factId": fact_id,
        "factType": fact_type,
        "contentHash": h,
        "calculatedHash": h,
        "hash_valid": True,
        "fact_json": fact_json
    }


class TestWO017NegativeSuite(unittest.TestCase):
    def setUp(self):
        methods = (
            "click", "double_click", "right_click", "mouse_move",
            "drag_and_drop", "type_text", "press_key", "hotkey",
            "scroll", "wait_until"
        )
        self.valid_method_details = {
            m: {
                "result": False,
                "blocked": True,
                "cursor_before": (1920, 1080),
                "cursor_after": (1920, 1080),
                "step_displacement": 0
            }
            for m in methods
        }
        self.valid_audit = {
            "cursor_initial": (1920, 1080),
            "cursor_final": (1920, 1080),
            "cursor_displacement": 0,
            "cursor_valid": True,
            "window_found": True,
            "tree_parsed": True,
            "synthetic_content_found": True,
            "cleanup_verified": True,
            "config_restored_cleanly": True,
            "secret_in_tree": False,
            "secret_in_trace": False,
            "secret_in_fact": False,
            "secret_in_log": False,
            "input_methods_tested": 10,
            "input_methods_blocked": True,
            "method_details": copy.deepcopy(self.valid_method_details),
            "fact_desktop_spooled": True,
        }

    def _make_valid_results(self):
        run_id = "test_run_123"
        tasks = {
            "task_id_a": f"rehearsal_a_{run_id}",
            "task_id_b": f"rehearsal_b_{run_id}",
            "task_id_b_rej": f"rehearsal_b_rej_{run_id}",
            "task_id_c": f"rehearsal_c_{run_id}",
            "task_id_d_get": f"rehearsal_d_get_{run_id}",
            "task_id_d_rej": f"rehearsal_d_rej_{run_id}",
            "task_id_d_app": f"rehearsal_d_app_{run_id}",
            "task_id_e": f"rehearsal_e_{run_id}",
        }
        facts = [
            make_canonical_fact("fact-web-111", "FactWebTaskExecuted", tasks["task_id_a"]),
            make_canonical_fact("fact-app-222", "FactWebActionApproved", tasks["task_id_b"]),
            make_canonical_fact("fact-api-333", "FactApiCallExecuted", tasks["task_id_d_app"]),
            make_canonical_fact("fact-desk-444", "FactDesktopWindowRead", tasks["task_id_e"]),
        ]
        manifest = {
            "run_id": run_id,
            "timestamp": "2026-09-25T04:00:00Z",
            "output_dir": f"/sandbox/runs/{run_id}",
            "rounds": {
                "round_a": {"task_id": tasks["task_id_a"]},
                "round_b_approved": {"task_id": tasks["task_id_b"]},
                "round_b_rejected": {"task_id": tasks["task_id_b_rej"]},
                "round_c": {"task_id": tasks["task_id_c"]},
                "round_d": {
                    "task_id_get": tasks["task_id_d_get"],
                    "task_id_rej": tasks["task_id_d_rej"],
                    "task_id_app": tasks["task_id_d_app"],
                },
                "round_e": {"task_id": tasks["task_id_e"]},
            },
            "facts": {
                f["factId"]: {
                    "fact_type": f["factType"],
                    "content_hash": f["contentHash"],
                    "valid": True
                }
                for f in facts
            }
        }
        return {
            "run_id": run_id,
            "manifest": manifest,
            "round_a": {"status": "done", "answer": "£45.17", "step_count": 1},
            "round_b_approved": {
                "status": "done",
                "audit": {"pre_approval_clicks": 0, "post_approval_clicks": 1, "server_clicks": 1, "approval_held": True}
            },
            "round_b_rejected": {
                "status": "done",
                "audit": {"clean_stop": True, "post_reject_clicks": 0, "server_clicks": 0}
            },
            "round_c": {
                "status": "done",
                "answer": "42,910",
                "audit": {"takeover_triggered": True, "challenge_solved": True, "resumed": True}
            },
            "round_d": {
                "status": "completed",
                "audit": {
                    "get_risk_level": "RISK_0_SAFE",
                    "get_status": "done",
                    "get_server_count": 1,
                    "post_financial_risk_level": "RISK_3_APPROVAL",
                    "rejection_status": "rejected_cleanly",
                    "rejection_gate_reached": True,
                    "rejection_pending_server_count": 0,
                    "rejection_pre_clicks": 0,
                    "rejection_post_clicks": 0,
                    "approval_status": "approved_and_executed",
                    "approval_gate_reached": True,
                    "approval_pending_server_count": 0,
                    "approval_pre_clicks": 0,
                    "approval_post_clicks": 1,
                    "rejection_pending_sampled_order": 1,
                    "rejection_decision_order": 2,
                    "approval_pending_sampled_order": 1,
                    "approval_decision_order": 2,
                    "idempotency_key_sent": True,
                    "fact_api_spooled": True
                }
            },
            "round_e": {"status": "completed", "audit": copy.deepcopy(self.valid_audit)},
            "facts": facts
        }

    # =========================================================================
    # R1: Strict Cursor Measurement and Method Details Checks
    # =========================================================================

    def test_round_e_valid_completed(self):
        """Baseline check: perfect audit produces completed / True."""
        status, passed = evaluate_round_e_status(self.valid_audit)
        self.assertEqual(status, "completed")
        self.assertTrue(passed)

    def test_round_e_movement_then_still_fails(self):
        """Reviewer case: cursor moved then still (displacement > 0) must NOT pass."""
        audit = copy.deepcopy(self.valid_audit)
        audit["cursor_initial"] = (100, 100)
        audit["cursor_final"] = (200, 200)
        audit["cursor_displacement"] = 200
        status, passed = evaluate_round_e_status(audit)
        self.assertNotEqual(status, "completed")
        self.assertFalse(passed)
        self.assertEqual(status, "inconclusive_or_failed")

    def test_round_e_intermediate_movement_returned_to_start_fails(self):
        """Codex probe 1: cursor moved during a method and returned to initial position must NOT pass."""
        audit = copy.deepcopy(self.valid_audit)
        audit["method_details"]["click"] = {
            "blocked": True,
            "result": False,
            "cursor_before": (100, 100),
            "cursor_after": (200, 200),
            "step_displacement": 200
        }
        status, passed = evaluate_round_e_status(audit)
        self.assertNotEqual(status, "completed")
        self.assertFalse(passed)
        self.assertEqual(status, "inconclusive_or_failed")

    def test_round_e_intermediate_measurement_none_fails(self):
        """Codex probe 2: intermediate cursor measurement was None must NOT pass."""
        audit = copy.deepcopy(self.valid_audit)
        audit["method_details"]["click"] = {
            "blocked": True,
            "result": False,
            "cursor_before": None,
            "cursor_after": None,
            "step_displacement": 0
        }
        status, passed = evaluate_round_e_status(audit)
        self.assertNotEqual(status, "completed")
        self.assertFalse(passed)
        self.assertEqual(status, "inconclusive_or_failed")

    def test_round_e_zero_zero_coordinate_fails(self):
        """(0,0) coordinate indicates broken measurement and must return inconclusive_or_failed."""
        audit = copy.deepcopy(self.valid_audit)
        audit["cursor_initial"] = (0, 0)
        status, passed = evaluate_round_e_status(audit)
        self.assertEqual(status, "inconclusive_or_failed")
        self.assertFalse(passed)

    def test_is_valid_cursor_pos_single_axis_zero(self):
        """Single-axis zero e.g. (0, 500) or (500, 0) is valid on primary monitor."""
        self.assertTrue(is_valid_cursor_pos((0, 500)))
        self.assertTrue(is_valid_cursor_pos((500, 0)))

    def test_is_valid_cursor_pos_multi_monitor_negative(self):
        """Negative coordinates on multi-monitor displays e.g. (-1920, 1080) are valid."""
        self.assertTrue(is_valid_cursor_pos((-1920, 1080)))
        self.assertTrue(is_valid_cursor_pos((-100, -200)))

    def test_is_valid_cursor_pos_origin_zero_zero_invalid(self):
        """(0, 0) origin represents failure/uninitialized measurement."""
        self.assertFalse(is_valid_cursor_pos((0, 0)))

    def test_is_valid_cursor_pos_none_invalid(self):
        """None coordinate is invalid."""
        self.assertFalse(is_valid_cursor_pos(None))

    def test_round_e_secret_found_in_tree_fails(self):
        """Secret leakage in UIA tree must fail evaluation."""
        audit = copy.deepcopy(self.valid_audit)
        audit["secret_in_tree"] = True
        status, passed = evaluate_round_e_status(audit)
        self.assertFalse(passed)

    def test_round_e_input_method_not_blocked_fails(self):
        """If any method returns True, evaluation must fail."""
        audit = copy.deepcopy(self.valid_audit)
        audit["method_details"]["click"]["result"] = True
        audit["method_details"]["click"]["blocked"] = False
        status, passed = evaluate_round_e_status(audit)
        self.assertFalse(passed)

    def test_round_e_method_details_count_and_keys(self):
        """All 10 required methods must be tracked in method_details."""
        required = {
            "click", "double_click", "right_click", "mouse_move",
            "drag_and_drop", "type_text", "press_key", "hotkey",
            "scroll", "wait_until"
        }
        self.assertEqual(set(self.valid_audit["method_details"].keys()), required)

    # =========================================================================
    # R2: Strict Overall Evaluator & Probes Assertions
    # =========================================================================

    def test_overall_rehearsal_baseline_passes(self):
        """Baseline valid results must return ('PASS', True)."""
        results = self._make_valid_results()
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "PASS")
        self.assertTrue(passed)

    def test_overall_rehearsal_facts_missing_identity_and_payload_fails(self):
        """Codex probe: Facts missing identity/payload/hash must fail."""
        results = self._make_valid_results()
        results["facts"] = [
            {"factType": t, "hash_valid": True}
            for t in ["FactWebTaskExecuted", "FactWebActionApproved", "FactApiCallExecuted", "FactDesktopWindowRead"]
        ]
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_overall_rehearsal_different_run_id_fails(self):
        """Codex probe: Mismatched run_id must fail."""
        results = self._make_valid_results()
        results["run_id"] = "unrelated_run"
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_overall_rehearsal_missing_manifest_fails(self):
        """Codex probe: Missing manifest must fail."""
        results = self._make_valid_results()
        results.pop("manifest", None)
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_overall_rehearsal_get_wrong_risk_fails(self):
        """Codex probe: GET with wrong risk level must fail."""
        results = self._make_valid_results()
        results["round_d"]["audit"]["get_risk_level"] = "RISK_3"
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_overall_rehearsal_round_b_rejection_clicks_fails(self):
        """Codex probe: Round B rejected with clicks or no clean stop must fail."""
        results = self._make_valid_results()
        results["round_b_rejected"]["audit"].update(post_reject_clicks=1, server_clicks=1, clean_stop=False)
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_overall_rehearsal_d_preapproval_request_fails(self):
        """Codex probe: Round D with requests sent before approval must fail."""
        results = self._make_valid_results()
        results["round_d"]["audit"]["approval_pre_clicks"] = 1
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_overall_rehearsal_missing_fact_type_fails(self):
        """Missing any required fact type must fail."""
        results = self._make_valid_results()
        results["facts"] = [f for f in results["facts"] if f["factType"] != "FactDesktopWindowRead"]
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_overall_rehearsal_fact_task_id_mismatch_fails(self):
        """Fact payload taskId not belonging to this run must fail."""
        results = self._make_valid_results()
        bad_fact = copy.deepcopy(results["facts"][0])
        bad_fact["fact_json"]["payload"]["taskId"] = "foreign_task_id"
        body = {k: v for k, v in bad_fact["fact_json"].items() if k != "contentHash"}
        h = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
        bad_fact["fact_json"]["contentHash"] = h
        bad_fact["contentHash"] = h
        results["facts"][0] = bad_fact
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    # =========================================================================
    # WO-017.5: Hardened Contract Mapping & D Pending Evidence Tests
    # =========================================================================

    def test_evaluator_api_fact_bound_to_round_a_fails(self):
        """Reviewer Probe 1: API fact bound to round A with valid hash must FAIL."""
        results = self._make_valid_results()
        f = results["facts"][2]
        new = make_canonical_fact(f["factId"], f["factType"], results["manifest"]["rounds"]["round_a"]["task_id"])
        results["facts"][2] = new
        results["manifest"]["facts"][new["factId"]]["content_hash"] = new["contentHash"]
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_missing_round_c_manifest_fails(self):
        """Reviewer Probe 2: Missing round C from manifest rounds must FAIL."""
        results = self._make_valid_results()
        results["manifest"]["rounds"].pop("round_c")
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_relabel_both_run_ids_without_rebinding_tasks_fails(self):
        """Reviewer Probe 3: Relabeling raw + manifest run_id without rebinding tasks must FAIL."""
        results = self._make_valid_results()
        results["run_id"] = "different_run"
        results["manifest"]["run_id"] = "different_run"
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_manifest_wrong_fact_type_fails(self):
        """Reviewer Probe 4: Manifest fact_type mismatch with envelope factType must FAIL."""
        results = self._make_valid_results()
        results["manifest"]["facts"][results["facts"][0]["factId"]]["fact_type"] = "WrongType"
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_missing_d_gate_and_pending_samples_fails(self):
        """Reviewer Probe 5: Missing D gate flags must FAIL."""
        results = self._make_valid_results()
        results["round_d"]["audit"].pop("approval_gate_reached", None)
        results["round_d"]["audit"].pop("rejection_gate_reached", None)
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_nonzero_d_pending_sample_fails(self):
        """Reviewer Probe 6: Nonzero D pending server counters must FAIL."""
        results = self._make_valid_results()
        results["round_d"]["audit"]["approval_pending_server_count"] = 7
        results["round_d"]["audit"]["rejection_pending_server_count"] = 9
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_bool_d_pending_counter_fails(self):
        """WO-017.5 R21: Boolean pending counter (e.g. True or False) must FAIL."""
        results = self._make_valid_results()
        results["round_d"]["audit"]["approval_pending_server_count"] = False
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_none_or_missing_d_pending_counter_fails(self):
        """WO-017.5 R21: None or missing pending counter must FAIL."""
        results = self._make_valid_results()
        results["round_d"]["audit"]["rejection_pending_server_count"] = None
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

        results2 = self._make_valid_results()
        results2["round_d"]["audit"].pop("rejection_pending_server_count", None)
        status2, passed2 = evaluate_overall_rehearsal(results2)
        self.assertEqual(status2, "FAIL")
        self.assertFalse(passed2)

    # =========================================================================
    # WO-017.6: Exact Identity, Execution Evidence, Ordering & Schema Matrix
    # =========================================================================

    def test_evaluator_missing_all_d_ordering_fails(self):
        """WO-017.6: Missing all or any Round D ordering keys must FAIL."""
        results = self._make_valid_results()
        for k in list(results["round_d"]["audit"].keys()):
            if k.endswith("_order"):
                results["round_d"]["audit"].pop(k)
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_none_or_malformed_d_ordering_fails(self):
        """WO-017.6: None, bool, float, or invalid ordering in Round D must FAIL without exception."""
        # Case 1: None
        results1 = self._make_valid_results()
        results1["round_d"]["audit"]["approval_pending_sampled_order"] = None
        status1, passed1 = evaluate_overall_rehearsal(results1)
        self.assertEqual(status1, "FAIL")
        self.assertFalse(passed1)

        # Case 2: Boolean
        results2 = self._make_valid_results()
        results2["round_d"]["audit"]["rejection_decision_order"] = True
        status2, passed2 = evaluate_overall_rehearsal(results2)
        self.assertEqual(status2, "FAIL")
        self.assertFalse(passed2)

        # Case 3: Out of order (sample >= decision)
        results3 = self._make_valid_results()
        results3["round_d"]["audit"]["approval_pending_sampled_order"] = 5
        results3["round_d"]["audit"]["approval_decision_order"] = 2
        status3, passed3 = evaluate_overall_rehearsal(results3)
        self.assertEqual(status3, "FAIL")
        self.assertFalse(passed3)

    def test_evaluator_created_fact_substituted_for_executed_fails(self):
        """WO-017.6: FactWebTaskCreated cannot substitute for FactWebTaskExecuted."""
        results = self._make_valid_results()
        old = results["facts"][0]
        new = make_canonical_fact(old["factId"], "FactWebTaskCreated", results["manifest"]["rounds"]["round_a"]["task_id"])
        results["facts"][0] = new
        results["manifest"]["facts"][new["factId"]].update(fact_type="FactWebTaskCreated", content_hash=new["contentHash"])
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_api_execution_fact_for_rejected_task_only_fails(self):
        """WO-017.6: API fact for rejected task only cannot count as execution evidence for approved POST."""
        results = self._make_valid_results()
        old = results["facts"][2]
        new = make_canonical_fact(old["factId"], "FactApiCallExecuted", results["manifest"]["rounds"]["round_d"]["task_id_rej"])
        results["facts"][2] = new
        results["manifest"]["facts"][new["factId"]].update(fact_type="FactApiCallExecuted", content_hash=new["contentHash"])
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_round_b_null_fails_without_exception(self):
        """WO-017.6: None/null round_b_approved (or any required round) must FAIL without exception."""
        for r_key in ("round_a", "round_b_approved", "round_b_rejected", "round_c", "round_d", "round_e"):
            results = self._make_valid_results()
            results[r_key] = None
            try:
                status, passed = evaluate_overall_rehearsal(results)
                self.assertEqual(status, "FAIL")
                self.assertFalse(passed)
            except Exception as e:
                self.fail(f"evaluate_overall_rehearsal raised unexpected {type(e).__name__} for {r_key}=None: {e}")

    def test_evaluator_run_id_substring_fails(self):
        """WO-017.6: run_id as a substring without rebinding tasks must FAIL exact identity check."""
        results = self._make_valid_results()
        results["run_id"] = "test_run"
        results["manifest"]["run_id"] = "test_run"
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_malformed_schema_matrix(self):
        """WO-017.6 R24: Comprehensive table-driven malformed schema matrix must fail-closed with zero exceptions."""
        malformed_cases = [
            ("results_is_none", lambda: None),
            ("results_is_list", lambda: [1, 2, 3]),
            ("results_is_string", lambda: "invalid"),
            ("results_is_empty", lambda: {}),
            ("manifest_is_none", lambda: {**self._make_valid_results(), "manifest": None}),
            ("manifest_is_list", lambda: {**self._make_valid_results(), "manifest": []}),
            ("manifest_rounds_is_none", lambda: {**self._make_valid_results(), "manifest": {"run_id": "test_run_123", "rounds": None}}),
            ("manifest_facts_is_none", lambda: {**self._make_valid_results(), "manifest": {"run_id": "test_run_123", "rounds": {}, "facts": None}}),
            ("facts_is_none", lambda: {**self._make_valid_results(), "facts": None}),
            ("facts_is_empty", lambda: {**self._make_valid_results(), "facts": []}),
            ("facts_contains_none", lambda: {**self._make_valid_results(), "facts": [None, None]}),
            ("facts_contains_string", lambda: {**self._make_valid_results(), "facts": ["fact-1", "fact-2"]}),
            ("round_d_audit_is_none", lambda: {**self._make_valid_results(), "round_d": {"status": "completed", "audit": None}}),
            ("round_e_audit_is_none", lambda: {**self._make_valid_results(), "round_e": {"status": "completed", "audit": None}}),
            ("run_id_is_int", lambda: {**self._make_valid_results(), "run_id": 12345}),
        ]

        for case_name, mutate_fn in malformed_cases:
            data = mutate_fn()
            try:
                status, passed = evaluate_overall_rehearsal(data)
                self.assertEqual(status, "FAIL", f"Case {case_name} expected FAIL but got {status}")
                self.assertFalse(passed, f"Case {case_name} expected passed=False")
            except Exception as e:
                self.fail(f"evaluate_overall_rehearsal raised unhandled {type(e).__name__} on case '{case_name}': {e}")

    # =========================================================================
    # WO-017.7: Production Semantic Payload & Non-Coercive Schema Matrix Tests
    # =========================================================================

    def test_evaluator_probe13_api_failed_get_with_valid_hash_fails(self):
        """WO-017.7 Reviewer Probe 13: Mutated API payload with valid canonical hash must FAIL."""
        results = self._make_valid_results()
        f = results["facts"][2]
        f["fact_json"]["payload"].update({
            "method": "GET",
            "statusCode": 500,
            "riskLevel": 0,
            "recipeName": "wrong_recipe"
        })
        body = {k: v for k, v in f["fact_json"].items() if k != "contentHash"}
        h = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
        f["contentHash"] = h
        f["fact_json"]["contentHash"] = h
        results["manifest"]["facts"][f["factId"]]["content_hash"] = h
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_probe14_round_e_count_null_fails(self):
        """WO-017.7 Reviewer Probe 14: Round E input_methods_tested=None must FAIL without exception."""
        results = self._make_valid_results()
        results["round_e"]["audit"]["input_methods_tested"] = None
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_probe15_round_e_false_string_fails(self):
        """WO-017.7 Reviewer Probe 15: String 'false' in boolean flags must FAIL without boolean coercion."""
        results = self._make_valid_results()
        results["round_e"]["audit"].update(
            window_found="false",
            tree_parsed="false",
            input_methods_blocked="false",
            fact_desktop_spooled="false"
        )
        status, passed = evaluate_overall_rehearsal(results)
        self.assertEqual(status, "FAIL")
        self.assertFalse(passed)

    def test_evaluator_api_semantic_fields_matrix(self):
        """WO-017.7 R23: Table-driven matrix testing API semantic payload fields individually.
        Each field is mutated and re-hashed with valid RFC 8785 canonical hash.
        Evaluator must reject each malformed/invalid field with FAIL / False and zero exceptions.
        """
        mutations = [
            ("recipeName_wrong_string", {"recipeName": "not_finance_transfer"}),
            ("recipeName_none", {"recipeName": None}),
            ("recipeName_int", {"recipeName": 123}),
            ("recipeName_bool", {"recipeName": True}),
            ("recipeName_empty", {"recipeName": ""}),
            ("method_GET", {"method": "GET"}),
            ("method_PUT", {"method": "PUT"}),
            ("method_DELETE", {"method": "DELETE"}),
            ("method_lowercase_post", {"method": "post"}),
            ("method_none", {"method": None}),
            ("method_int", {"method": 1}),
            ("riskLevel_0", {"riskLevel": 0}),
            ("riskLevel_1", {"riskLevel": 1}),
            ("riskLevel_2", {"riskLevel": 2}),
            ("riskLevel_4", {"riskLevel": 4}),
            ("riskLevel_negative", {"riskLevel": -1}),
            ("riskLevel_bool_true", {"riskLevel": True}),
            ("riskLevel_string", {"riskLevel": "3"}),
            ("riskLevel_float", {"riskLevel": 3.0}),
            ("riskLevel_none", {"riskLevel": None}),
            ("statusCode_500", {"statusCode": 500}),
            ("statusCode_400", {"statusCode": 400}),
            ("statusCode_403", {"statusCode": 403}),
            ("statusCode_404", {"statusCode": 404}),
            ("statusCode_301", {"statusCode": 301}),
            ("statusCode_bool_true", {"statusCode": True}),
            ("statusCode_string", {"statusCode": "200"}),
            ("statusCode_float", {"statusCode": 200.0}),
            ("statusCode_none", {"statusCode": None}),
            ("stepNum_bool", {"stepNum": True}),
            ("stepNum_string", {"stepNum": "1"}),
        ]

        for case_name, patch_dict in mutations:
            results = self._make_valid_results()
            f = results["facts"][2]
            f["fact_json"]["payload"].update(patch_dict)
            body = {k: v for k, v in f["fact_json"].items() if k != "contentHash"}
            h = "sha256:" + hashlib.sha256(rfc8785.dumps(body)).hexdigest()
            f["contentHash"] = h
            f["fact_json"]["contentHash"] = h
            results["manifest"]["facts"][f["factId"]]["content_hash"] = h
            try:
                status, passed = evaluate_overall_rehearsal(results)
                self.assertEqual(status, "FAIL", f"Case {case_name} expected FAIL but got {status}")
                self.assertFalse(passed, f"Case {case_name} expected passed=False")
            except Exception as e:
                self.fail(f"evaluate_overall_rehearsal raised unhandled {type(e).__name__} on case '{case_name}': {e}")

    def test_evaluator_round_e_nested_fields_matrix(self):
        """WO-017.7 R23: Table-driven matrix testing Round E nested audit fields individually.
        No bool() or int() coercion is permitted: non-exact types, string falses, and
        non-finite or uninitialized coordinates/displacement must FAIL with zero exceptions.
        """
        mutations = [
            ("window_found_false", {"window_found": False}),
            ("window_found_str_true", {"window_found": "true"}),
            ("window_found_str_false", {"window_found": "false"}),
            ("window_found_int_1", {"window_found": 1}),
            ("window_found_none", {"window_found": None}),
            ("tree_parsed_false", {"tree_parsed": False}),
            ("tree_parsed_str_true", {"tree_parsed": "true"}),
            ("tree_parsed_str_false", {"tree_parsed": "false"}),
            ("tree_parsed_int_1", {"tree_parsed": 1}),
            ("tree_parsed_none", {"tree_parsed": None}),
            ("input_methods_blocked_false", {"input_methods_blocked": False}),
            ("input_methods_blocked_str_true", {"input_methods_blocked": "true"}),
            ("input_methods_blocked_str_false", {"input_methods_blocked": "false"}),
            ("input_methods_blocked_int_1", {"input_methods_blocked": 1}),
            ("input_methods_blocked_none", {"input_methods_blocked": None}),
            ("fact_desktop_spooled_false", {"fact_desktop_spooled": False}),
            ("fact_desktop_spooled_str_true", {"fact_desktop_spooled": "true"}),
            ("fact_desktop_spooled_str_false", {"fact_desktop_spooled": "false"}),
            ("fact_desktop_spooled_int_1", {"fact_desktop_spooled": 1}),
            ("fact_desktop_spooled_none", {"fact_desktop_spooled": None}),
            ("tested_count_9", {"input_methods_tested": 9}),
            ("tested_count_11", {"input_methods_tested": 11}),
            ("tested_count_0", {"input_methods_tested": 0}),
            ("tested_count_str_10", {"input_methods_tested": "10"}),
            ("tested_count_bool_true", {"input_methods_tested": True}),
            ("tested_count_bool_false", {"input_methods_tested": False}),
            ("tested_count_float_10", {"input_methods_tested": 10.0}),
            ("tested_count_none", {"input_methods_tested": None}),
            ("tested_count_list", {"input_methods_tested": [10]}),
            ("leak_tree_true", {"secret_in_tree": True}),
            ("leak_tree_str_false", {"secret_in_tree": "false"}),
            ("leak_tree_none", {"secret_in_tree": None}),
            ("leak_trace_true", {"secret_in_trace": True}),
            ("leak_trace_str_false", {"secret_in_trace": "false"}),
            ("leak_fact_true", {"secret_in_fact": True}),
            ("leak_fact_str_false", {"secret_in_fact": "false"}),
            ("leak_log_true", {"secret_in_log": True}),
            ("leak_log_str_false", {"secret_in_log": "false"}),
            ("cursor_valid_false", {"cursor_valid": False}),
            ("cursor_valid_str_true", {"cursor_valid": "true"}),
            ("cursor_valid_none", {"cursor_valid": None}),
            ("cursor_initial_origin", {"cursor_initial": (0, 0)}),
            ("cursor_initial_none", {"cursor_initial": None}),
            ("cursor_initial_nan", {"cursor_initial": (float("nan"), 100)}),
            ("cursor_initial_inf", {"cursor_initial": (float("inf"), 100)}),
            ("cursor_initial_bool", {"cursor_initial": (True, 100)}),
            ("cursor_initial_list", {"cursor_initial": [0, 0]}),
            ("cursor_initial_string", {"cursor_initial": "(100, 100)"}),
            ("cursor_final_origin", {"cursor_final": (0, 0)}),
            ("cursor_final_nan", {"cursor_final": (100, float("nan"))}),
            ("cursor_final_inf", {"cursor_final": (100, float("inf"))}),
            ("cursor_final_bool", {"cursor_final": (100, False)}),
            ("cursor_final_none", {"cursor_final": None}),
            ("cursor_displacement_nonzero", {"cursor_displacement": 5}),
            ("cursor_displacement_negative", {"cursor_displacement": -1}),
            ("cursor_displacement_nan", {"cursor_displacement": float("nan")}),
            ("cursor_displacement_inf", {"cursor_displacement": float("inf")}),
            ("cursor_displacement_bool", {"cursor_displacement": True}),
            ("cursor_displacement_str", {"cursor_displacement": "0"}),
            ("cursor_displacement_none", {"cursor_displacement": None}),
        ]

        for case_name, patch_dict in mutations:
            results = self._make_valid_results()
            results["round_e"]["audit"].update(patch_dict)
            try:
                # Test both standalone evaluate_round_e_status and overall evaluate_overall_rehearsal
                e_status, e_passed = evaluate_round_e_status(results["round_e"]["audit"])
                self.assertFalse(e_passed, f"evaluate_round_e_status passed=True on case '{case_name}'")
                self.assertNotEqual(e_status, "completed", f"evaluate_round_e_status completed on case '{case_name}'")

                status, passed = evaluate_overall_rehearsal(results)
                self.assertEqual(status, "FAIL", f"Case {case_name} expected FAIL but got {status}")
                self.assertFalse(passed, f"Case {case_name} expected passed=False")
            except Exception as e:
                self.fail(f"Evaluator raised unhandled {type(e).__name__} on case '{case_name}': {e}")

        # Method details specific mutations
        method_mutations = [
            ("method_missing_key", lambda md: md.pop("click")),
            ("method_entry_none", lambda md: md.update(click=None)),
            ("method_result_true", lambda md: md["click"].update(result=True)),
            ("method_blocked_false", lambda md: md["click"].update(blocked=False)),
            ("method_blocked_str_true", lambda md: md["click"].update(blocked="true")),
            ("method_cursor_before_none", lambda md: md["click"].update(cursor_before=None)),
            ("method_cursor_before_origin", lambda md: md["click"].update(cursor_before=(0, 0))),
            ("method_cursor_before_nan", lambda md: md["click"].update(cursor_before=(float("nan"), 100))),
            ("method_cursor_before_bool", lambda md: md["click"].update(cursor_before=(True, 100))),
            ("method_cursor_after_none", lambda md: md["click"].update(cursor_after=None)),
            ("method_cursor_after_origin", lambda md: md["click"].update(cursor_after=(0, 0))),
            ("method_cursor_after_nan", lambda md: md["click"].update(cursor_after=(100, float("nan")))),
            ("method_cursor_after_bool", lambda md: md["click"].update(cursor_after=(100, False))),
            ("method_disp_nonzero", lambda md: md["click"].update(step_displacement=5)),
            ("method_disp_nan", lambda md: md["click"].update(step_displacement=float("nan"))),
            ("method_disp_inf", lambda md: md["click"].update(step_displacement=float("inf"))),
            ("method_disp_bool", lambda md: md["click"].update(step_displacement=True)),
            ("method_disp_none", lambda md: md["click"].update(step_displacement=None)),
            ("method_disp_str", lambda md: md["click"].update(step_displacement="0")),
        ]

        for case_name, fn in method_mutations:
            results = self._make_valid_results()
            fn(results["round_e"]["audit"]["method_details"])
            try:
                e_status, e_passed = evaluate_round_e_status(results["round_e"]["audit"])
                self.assertFalse(e_passed, f"evaluate_round_e_status passed=True on method case '{case_name}'")
                self.assertNotEqual(e_status, "completed", f"evaluate_round_e_status completed on method case '{case_name}'")

                status, passed = evaluate_overall_rehearsal(results)
                self.assertEqual(status, "FAIL", f"Case {case_name} expected FAIL but got {status}")
                self.assertFalse(passed, f"Case {case_name} expected passed=False")
            except Exception as e:
                self.fail(f"Evaluator raised unhandled {type(e).__name__} on method case '{case_name}': {e}")

    # =========================================================================
    # R3 / R4: Real Production Persistence and Multi-Run Isolation
    # =========================================================================

    def test_production_run_two_consecutive_runs_sha256_unmodified(self):
        """R4 Requirement: Call production run() twice in sandbox with mocked external components.
        Verify:
        1. Run 1 artifacts are created under runs/run_1/
        2. All files in runs/run_1/ retain exact same SHA256 hashes after Run 2
        3. Calling run() again with run_1 raises FileExistsError before mutation
        """
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = root / "config.json"
            initial_cfg = b'{\n  "direct_api_enabled": false,\n  "uia_enabled": false,\n  "onevault_enabled": false\n}\n'
            cfg.write_bytes(initial_cfg)
            out = root / "output"

            active_server = [None]

            class MockServer:
                def __init__(self):
                    self.click_count = 0
                    self.botwall_solved = False
                    self.api_requests = [
                        {"method": "GET", "path": "/api/sensor", "status": 200},
                        {"method": "POST", "path": "/api/transfer", "status": 200, "headers": {"Idempotency-Key": "test-key-123"}}
                    ]
                    self.api_post_count = 0
                    self.api_get_count = 0

                def shutdown(self):
                    pass

            def create_mock_server(*args, **kwargs):
                srv = MockServer()
                active_server[0] = srv
                return srv

            class DummyPage:
                def __init__(self):
                    self.url = "http://127.0.0.1:18822"
                def goto(self, url, **kwargs):
                    self.url = url
                def wait_for_selector(self, selector, **kwargs):
                    return MagicMock()
                def wait_for_timeout(self, timeout):
                    pass
                def click(self, selector, **kwargs):
                    pass
                def content(self):
                    return "<html><body>Hello</body></html>"
                def title(self):
                    return "Dummy Title"
                def query_selector(self, selector):
                    return MagicMock()
                def query_selector_all(self, selector):
                    return []
                def evaluate(self, expr, **kwargs):
                    if "buttonClickCount" in expr:
                        srv = active_server[0]
                        return srv.click_count if srv else 0
                    if "order-status" in expr:
                        return "COMPLETED"
                    return None
                def close(self):
                    pass

            class DummyBrowser:
                def new_page(self):
                    return DummyPage()
                def close(self):
                    pass

            class DummyPlaywright:
                def __init__(self):
                    self.chromium = MagicMock()
                    self.chromium.launch.return_value = DummyBrowser()

            class DummyPlaywrightContext:
                def __enter__(self):
                    return DummyPlaywright()
                def __exit__(self, exc_type, exc_val, exc_tb):
                    pass

            class DummyTakeoverMgr:
                def __init__(self):
                    self.takeover_active = True
                    self.reason = "turnstile_challenge"
                def resume_agent(self):
                    self.takeover_active = False

            class DummyOrch:
                def __init__(self):
                    self.takeover_mgr = DummyTakeoverMgr()

                def run_task(self, page=None, goal=None, max_subtasks=3, task_id=None):
                    mgr = rehearsal_runner.approval_manager
                    srv = active_server[0]
                    if task_id:
                        if "rej" in task_id:
                            is_approved = mgr.request_approval(
                                task_id=task_id, step_num=1, action_type="click",
                                target="#btn", risk_level="HIGH", description="dummy", timeout=5.0
                            )
                            if not is_approved:
                                return {"status": "approval_required", "reason": "rejected_by_supervisor"}
                        elif "b_" in task_id or "task_b" in task_id or "_b" in task_id:
                            is_approved = mgr.request_approval(
                                task_id=task_id, step_num=1, action_type="click",
                                target="#btn", risk_level="HIGH", description="dummy", timeout=5.0
                            )
                            if is_approved:
                                if srv:
                                    srv.click_count = 1
                                return {
                                    "status": "done",
                                    "answer": "test_answer",
                                    "evidence": {"screenshot": "dummy"},
                                    "steps": 1,
                                    "actions": [{"action": "click", "selector": "#btn"}]
                                }
                        elif "c_" in task_id or "task_c" in task_id or "_c" in task_id:
                            for _ in range(60):
                                if not self.takeover_mgr.takeover_active:
                                    break
                                time.sleep(0.05)
                            return {
                                "status": "done",
                                "answer": "123,456",
                                "evidence": {"screenshot": "dummy"},
                                "steps": 1,
                                "actions": [{"action": "click", "selector": "#btn"}]
                            }
                    return {
                        "status": "done",
                        "answer": "test_answer",
                        "evidence": {"screenshot": "dummy"},
                        "steps": 1,
                        "actions": [{"action": "click", "selector": "#btn"}]
                    }

            class DummyUIAAdapter:
                def get_cursor_position(self):
                    return (500, 500)
                def click(self, *args, **kwargs):
                    return False
                def double_click(self, *args, **kwargs):
                    return False
                def right_click(self, *args, **kwargs):
                    return False
                def mouse_move(self, *args, **kwargs):
                    return False
                def drag_and_drop(self, *args, **kwargs):
                    return False
                def type_text(self, *args, **kwargs):
                    return False
                def press_key(self, *args, **kwargs):
                    return False
                def hotkey(self, *args, **kwargs):
                    return False
                def scroll(self, *args, **kwargs):
                    return False
                def wait_until(self, *args, **kwargs):
                    return False

            class DummyTreeParser:
                def __init__(self, **kwargs):
                    from tests.test_codex_closeout import CloseoutTests
                    self._kernel32 = CloseoutTests().api()
                def _get_process_creation_time(self, pid): return 123
                def find_window_with_ownership(self, **kwargs):
                    return 12345, {"verified": True, "pid": 50, "selected_hwnd": 12345}
                def get_window_info(self, hwnd): return {"pid": 50}
                def build_masked_tree(self, hwnd):
                    content = pathlib.Path(DummyPopen.last_file).read_text('utf-8')
                    return {"ok": True, "element_count": 2,
                            "window_title_hash": "sha256:" + "c" * 64,
                            "structure_hash": "sha256:" + "b" * 64,
                            "tree": {"role": "Window", "children": [{"role": "Document",
                                "text_hash": "sha256:" + hashlib.sha256(content.encode()).hexdigest(), "children": []}]}}

            class DummyDirectApiClient:
                def __init__(self, **kwargs):
                    pass
                def execute_call(self, recipe_name, parameters=None, task_id=None, step_num=1):
                    srv = active_server[0]
                    if recipe_name == "get_sensor_data":
                        if srv:
                            srv.api_get_count = 1
                        return {"ok": True, "status": "done", "response": {"temp": 25.0}}
                    elif recipe_name == "finance_transfer":
                        if srv:
                            srv.api_post_count = 1
                            srv.api_requests.append({
                                "method": "POST",
                                "path": "/api/v1/finance/transfer",
                                "status": 200,
                                "headers": {"Idempotency-Key": "test-key-123"}
                            })
                        return {"ok": True, "status": "done", "response": {"balance": 1000}}
                    return {"ok": True, "status": "done", "response": {}}

            class DummyPopen:
                pid = 50
                _handle = 777
                last_file = None
                def __init__(self, args, **kwargs):
                    DummyPopen.last_file = args[1]
                def poll(self):
                    return None
                def kill(self):
                    pass
                def wait(self, timeout=None):
                    pass

            with patch("playwright.sync_api.sync_playwright", return_value=DummyPlaywrightContext()), \
                 patch.object(rehearsal_runner, "run_rehearsal_server", side_effect=create_mock_server), \
                 patch.object(rehearsal_runner, "run_interactive_task", return_value={"status": "done", "answer": "test_answer"}), \
                 patch.object(rehearsal_runner, "InteractiveAutomationOrchestrator", DummyOrch), \
                 patch.object(rehearsal_runner, "WindowsUIAAdapter", DummyUIAAdapter), \
                 patch.object(rehearsal_runner, "DesktopTreeParser", DummyTreeParser), \
                 patch.object(rehearsal_runner, "DirectApiClient", DummyDirectApiClient), \
                 patch.object(rehearsal_runner.subprocess, "Popen", DummyPopen):

                # Run 1 - executes REAL evaluate_overall_rehearsal
                res1 = rehearsal_runner.run(run_id="run_sandbox_001", output_root=out, config_path=cfg)
                self.assertEqual(res1.get("overall_status"), "PASS")
                self.assertTrue(res1.get("overall_ok"))
                run_1_dir = out / "runs" / "run_sandbox_001"
                self.assertTrue(run_1_dir.is_dir())
                self.assertTrue((run_1_dir / "manifest.json").is_file())
                self.assertTrue((run_1_dir / "rehearsal_raw.json").is_file())

                # Hash all files in Run 1
                files_before = {
                    str(f.relative_to(run_1_dir)): hashlib.sha256(f.read_bytes()).hexdigest()
                    for f in run_1_dir.rglob("*") if f.is_file()
                }
                self.assertGreater(len(files_before), 0)

                # Run 2 with different run_id - executes REAL evaluate_overall_rehearsal
                res2 = rehearsal_runner.run(run_id="run_sandbox_002", output_root=out, config_path=cfg)
                self.assertEqual(res2.get("overall_status"), "PASS")
                self.assertTrue(res2.get("overall_ok"))
                run_2_dir = out / "runs" / "run_sandbox_002"
                self.assertTrue(run_2_dir.is_dir())

                # Verify all files in Run 1 have 100% identical SHA256 hashes
                for rel_path, exp_hash in files_before.items():
                    fpath = run_1_dir / rel_path
                    self.assertTrue(fpath.is_file(), f"File {rel_path} was removed")
                    self.assertEqual(hashlib.sha256(fpath.read_bytes()).hexdigest(), exp_hash, f"File {rel_path} changed")

                # Verify duplicate run_id rejected before mutation
                with self.assertRaises(FileExistsError):
                    rehearsal_runner.run(run_id="run_sandbox_001", output_root=out, config_path=cfg)

    # =========================================================================
    # R5: Rehearsal Execution Context Exception Restoration & Sentinel Preservation
    # =========================================================================

    def test_production_run_injected_setup_exception_restores_baseline(self):
        """R5 Requirement: Injected exception in setup after config write restores config, env, and vault baseline."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = root / "config.json"
            before = b'{"sentinel_config": true}'
            cfg.write_bytes(before)
            sent = root / "sentinel"
            sent.write_bytes(b"keep")
            oldenv = dict(os.environ)
            oldmgr = get_vault_manager()
            baseline = OneVaultManager(enabled=False)
            set_vault_manager(baseline)
            os.environ["ONEVAULT_DIR"] = "REVIEW_SENTINEL"

            def fail_setup(**kwargs):
                kwargs["config_path"].write_bytes(b'{"modified": true}')
                os.environ["ONEVAULT_DIR"] = "MODIFIED"
                set_vault_manager(OneVaultManager(enabled=False))
                raise RuntimeError("injected_after_config_write")

            try:
                with patch.object(rehearsal_runner, "run_rehearsal_server", return_value=Server()), \
                     patch.object(rehearsal_runner, "enable_onevault", side_effect=fail_setup):
                    with self.assertRaises(RuntimeError):
                        rehearsal_runner.run(run_id="review_injected", output_root=root / "out", config_path=cfg)
                self.assertEqual(cfg.read_bytes(), before)
                self.assertEqual(os.environ.get("ONEVAULT_DIR"), "REVIEW_SENTINEL")
                self.assertIs(get_vault_manager(), baseline)
                self.assertEqual(sent.read_bytes(), b"keep")
            finally:
                os.environ.clear()
                os.environ.update(oldenv)
                set_vault_manager(oldmgr)

    def test_production_run_injected_mid_run_exception_restores_baseline(self):
        """R5 Requirement: Injected exception mid-run restores config, env, and vault baseline."""
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            cfg = root / "config.json"
            before = b'{"sentinel_mid": true}'
            cfg.write_bytes(before)
            sent = root / "sentinel_mid"
            sent.write_bytes(b"keep_mid")
            oldenv = dict(os.environ)
            oldmgr = get_vault_manager()
            baseline = OneVaultManager(enabled=False)
            set_vault_manager(baseline)
            os.environ["ONEVAULT_DIR"] = "MID_SENTINEL"

            def fail_mid(*args, **kwargs):
                raise RuntimeError("injected_mid_run_crash")

            try:
                with patch.object(rehearsal_runner, "run_rehearsal_server", return_value=Server()), \
                     patch.object(rehearsal_runner, "run_interactive_task", side_effect=fail_mid):
                    with self.assertRaises(RuntimeError):
                        rehearsal_runner.run(run_id="mid_injected", output_root=root / "out", config_path=cfg)
                self.assertEqual(cfg.read_bytes(), before)
                self.assertEqual(os.environ.get("ONEVAULT_DIR"), "MID_SENTINEL")
                self.assertIs(get_vault_manager(), baseline)
                self.assertEqual(sent.read_bytes(), b"keep_mid")
            finally:
                os.environ.clear()
                os.environ.update(oldenv)
                set_vault_manager(oldmgr)

    def test_cleanup_sentinel_preservation_and_containment(self):
        """Verify sentinel files remain byte-identical after cleanup and uncontained files are rejected."""
        with tempfile.TemporaryDirectory() as td:
            td_path = pathlib.Path(td)
            allowed_dir = td_path / "allowed_sandbox"
            allowed_dir.mkdir()
            foreign_dir = td_path / "foreign_data"
            foreign_dir.mkdir()

            sentinel_file = allowed_dir / "sentinel_user_trace.jsonl"
            sentinel_bytes = b'{"pre_existing": true, "important": "do_not_delete"}\n'
            sentinel_file.write_bytes(sentinel_bytes)
            sentinel_hash_before = hashlib.sha256(sentinel_bytes).hexdigest()

            run_file = allowed_dir / "run_ephemeral_trace.jsonl"
            run_file.write_text("temporary data\n")

            foreign_file = foreign_dir / "protected_coordination_file.json"
            foreign_bytes = b'{"critical": "do_not_touch"}\n'
            foreign_file.write_bytes(foreign_bytes)
            foreign_hash_before = hashlib.sha256(foreign_bytes).hexdigest()

            created_files = {run_file, foreign_file}
            safe_cleanup_run(created_files, allowed_roots=[allowed_dir])

            self.assertFalse(run_file.exists())
            self.assertTrue(sentinel_file.exists())
            self.assertEqual(hashlib.sha256(sentinel_file.read_bytes()).hexdigest(), sentinel_hash_before)
            self.assertTrue(foreign_file.exists())
            self.assertEqual(hashlib.sha256(foreign_file.read_bytes()).hexdigest(), foreign_hash_before)

    def test_production_config_not_modified_without_finally(self):
        """Verify CONFIG_FILE on disk matches ORIGINAL_CONFIG_BYTES."""
        if ORIGINAL_CONFIG_BYTES is not None and CONFIG_FILE.is_file():
            self.assertEqual(CONFIG_FILE.read_bytes(), ORIGINAL_CONFIG_BYTES)


if __name__ == "__main__":
    unittest.main()
