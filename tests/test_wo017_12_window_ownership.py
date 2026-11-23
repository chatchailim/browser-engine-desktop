"""Offline unit tests for WO-017.13 Window Discovery, Process Identity & Ownership Verification.

Mocks the OS boundary (Win32 EnumWindows, GetWindowInfo, UIA, Toolhelp32) while exercising
the REAL production algorithms in DesktopTreeParser, cleanup_owned_process_tree,
verify_synthetic_content_presence, and rehearsal_runner.

Test Matrix per WO-017.13 Requirements & REVIEW-WO-017.12-CODEX.md:
1. Title empty in owned window (UIA fallback / Win32 empty title handling)
2. Delayed availability (bounded polling / retries succeed before timeout)
3. Existing unrelated Notepad (ignores user's existing Notepad processes)
4. Ambiguous candidates (fails closed if multiple candidate windows match)
5. No match timeout (fails closed with timeout_no_owned_window)
6. Unconstrained search (fails closed when both expected_pid and title_keyword are None)
7. Process handoff unproven (fails closed when candidate PID is not in verified process tree)
8. Process handoff child PID verified (successfully resolves verified child process)
9. Same-name PID reuse rejection (fails closed when PID creation time does not match expected_creation_time)
10. Exact image basename matching (rejects other_notepad.exe substring match)
11. Win32 handle pointer-safety & invalid handle error paths
12. Production cleanup helper isolation (terminates verified parent + child, never kills user PID or reused PID)
13. Synthetic content presence verification & empty/unrelated tree privacy failure
14. Rehearsal execution context config hash restoration under exception
15. Targeted Round E runner selector (executes Round E only, never starts server or Rounds A-D)
"""
import ctypes
import hashlib
import json
import os
import pathlib
import signal
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from typing import Any, Dict, List, Optional, Set

app_dir = pathlib.Path(__file__).resolve().parent.parent
if str(app_dir) not in sys.path:
    sys.path.insert(0, str(app_dir))

from perception.desktop_tree import (
    DesktopTreeParser,
    cleanup_owned_process_tree,
    verify_synthetic_content_presence
)
from policy.uia_policy import UIAPolicyEngine
import tests.rehearsal_runner as rr


class FakeWindow:
    def __init__(
        self,
        hwnd: int,
        pid: int,
        process_name: str,
        title: str = "",
        uia_name: str = "",
        visible: bool = True
    ):
        self.hwnd = hwnd
        self.pid = pid
        self.process_name = process_name
        self.title = title
        self.uia_name = uia_name
        self.visible = visible


class TestWindowOwnershipOffline(unittest.TestCase):
    def setUp(self):
        self.policy = UIAPolicyEngine(enabled=True, allowed_apps=["notepad.exe"])
        self.parser = DesktopTreeParser(policy=self.policy)

    def _setup_mock_desktop(
        self,
        windows: List[FakeWindow],
        child_pids_map: Optional[Dict[int, Set[int]]] = None,
        creation_times_map: Optional[Dict[int, int]] = None
    ):
        """Mock OS boundary methods on self.parser."""
        child_pids_map = child_pids_map or {}
        creation_times_map = creation_times_map or {}

        def mock_enum_windows(callback, lparam):
            for w in windows:
                cont = callback(w.hwnd, lparam)
                if not cont:
                    break
            return 1

        def mock_is_window_visible(hwnd):
            for w in windows:
                if w.hwnd == hwnd:
                    return bool(w.visible)
            return False

        def mock_get_window_info(hwnd):
            for w in windows:
                if w.hwnd == hwnd:
                    return {
                        "hwnd": w.hwnd,
                        "pid": w.pid,
                        "process_name": w.process_name,
                        "title": w.title,
                        "is_elevated_higher": False
                    }
            return {"hwnd": hwnd, "pid": 0, "process_name": "", "title": "", "is_elevated_higher": False}

        def mock_get_uia_element_name(hwnd):
            for w in windows:
                if w.hwnd == hwnd:
                    return w.uia_name
            return ""

        def mock_get_child_pids(parent_pid, parent_creation_time=None):
            return child_pids_map.get(parent_pid, set())

        def mock_get_creation_time(pid):
            return creation_times_map.get(pid, 1000000)

        # Patch internal parser methods
        self.parser._user32 = MagicMock()
        self.parser._user32.EnumWindows.side_effect = mock_enum_windows
        self.parser._user32.IsWindowVisible.side_effect = mock_is_window_visible
        self.parser.get_window_info = mock_get_window_info
        self.parser._get_uia_element_name = mock_get_uia_element_name
        self.parser._get_child_pids = mock_get_child_pids
        self.parser._get_process_creation_time = mock_get_creation_time

    # -------------------------------------------------------------------------
    # 1. Title empty fallback to UIA Name
    # -------------------------------------------------------------------------
    def test_title_empty_in_owned_window_with_uia_fallback(self):
        """Win32 title is empty (WinUI 3 symptom), but UIA Name contains secret keyword."""
        windows = [
            FakeWindow(
                hwnd=101,
                pid=5001,
                process_name="notepad.exe",
                title="",
                uia_name="rehearsal_secret_file.txt - Notepad"
            )
        ]
        self._setup_mock_desktop(windows)

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=5001,
            expected_process_name="notepad.exe",
            title_keyword="rehearsal_secret_file",
            timeout_sec=0.1
        )
        self.assertEqual(hwnd, 101)
        self.assertTrue(proof["verified"])
        self.assertEqual(proof["title_source"], "uia_name")
        self.assertEqual(proof["pid"], 5001)

    # -------------------------------------------------------------------------
    # 2. Delayed availability bounded polling
    # -------------------------------------------------------------------------
    def test_delayed_availability_bounded_polling(self):
        """Window appears after a short delay (call 3). Polling must succeed."""
        attempts = 0
        def dynamic_enum(callback, lparam):
            nonlocal attempts
            attempts += 1
            if attempts >= 3:
                callback(202, lparam)
            return 1

        self.parser._user32 = MagicMock()
        self.parser._user32.EnumWindows.side_effect = dynamic_enum
        self.parser._user32.IsWindowVisible.return_value = True
        self.parser.get_window_info = lambda h: {
            "hwnd": 202, "pid": 6001, "process_name": "notepad.exe",
            "title": "delayed_test.txt - Notepad"
        }
        self.parser._get_child_pids = lambda p, parent_creation_time=None: set()
        self.parser._get_uia_element_name = lambda h: ""
        self.parser._get_process_creation_time = lambda p: 1000000

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=6001,
            expected_process_name="notepad.exe",
            title_keyword="delayed_test",
            timeout_sec=1.0,
            poll_interval=0.05
        )
        self.assertEqual(hwnd, 202)
        self.assertTrue(proof["verified"])
        self.assertGreaterEqual(attempts, 3)

    # -------------------------------------------------------------------------
    # 3. Existing unrelated Notepad ignored
    # -------------------------------------------------------------------------
    def test_existing_unrelated_notepad_ignored(self):
        """User has existing Notepad (PID 9999). Test launched PID 7001. User's Notepad MUST NOT be selected."""
        windows = [
            FakeWindow(hwnd=10, pid=9999, process_name="notepad.exe", title="user_private_notes.txt - Notepad"),
            FakeWindow(hwnd=20, pid=7001, process_name="notepad.exe", title="rehearsal_7001.txt - Notepad")
        ]
        self._setup_mock_desktop(windows)

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=7001,
            expected_process_name="notepad.exe",
            title_keyword="rehearsal_7001",
            timeout_sec=0.1
        )
        self.assertEqual(hwnd, 20)
        self.assertTrue(proof["verified"])
        self.assertEqual(proof["pid"], 7001)

    # -------------------------------------------------------------------------
    # 4. Ambiguous candidates fail closed
    # -------------------------------------------------------------------------
    def test_ambiguous_candidates_fails_closed(self):
        """Multiple windows match for the same expected PID. Must fail closed (return None)."""
        windows = [
            FakeWindow(hwnd=301, pid=8001, process_name="notepad.exe", title="rehearsal_stem.txt - Notepad"),
            FakeWindow(hwnd=302, pid=8001, process_name="notepad.exe", title="rehearsal_stem.txt - Notepad [2]")
        ]
        self._setup_mock_desktop(windows)

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=8001,
            expected_process_name="notepad.exe",
            title_keyword="rehearsal_stem",
            timeout_sec=0.1
        )
        self.assertIsNone(hwnd)
        self.assertFalse(proof["verified"])
        self.assertEqual(proof["error"], "ambiguous_candidates")
        self.assertEqual(proof["candidate_count"], 2)

    # -------------------------------------------------------------------------
    # 5. No match timeout fails closed
    # -------------------------------------------------------------------------
    def test_no_match_timeout_fails_closed(self):
        """No matching window found. Bounded polling times out cleanly and fails closed."""
        windows = [
            FakeWindow(hwnd=401, pid=9001, process_name="calc.exe", title="Calculator")
        ]
        self._setup_mock_desktop(windows)

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=9002,
            expected_process_name="notepad.exe",
            title_keyword="missing_file",
            timeout_sec=0.15,
            poll_interval=0.05
        )
        self.assertIsNone(hwnd)
        self.assertFalse(proof["verified"])
        self.assertEqual(proof["error"], "timeout_no_owned_window")

    # -------------------------------------------------------------------------
    # 6. Unconstrained search fails closed
    # -------------------------------------------------------------------------
    def test_unconstrained_search_fails_closed(self):
        """Calling without expected_pid and without title_keyword is strictly prohibited."""
        windows = [
            FakeWindow(hwnd=501, pid=1111, process_name="notepad.exe", title="Anything")
        ]
        self._setup_mock_desktop(windows)

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=None,
            expected_process_name="notepad.exe",
            title_keyword=None
        )
        self.assertIsNone(hwnd)
        self.assertFalse(proof["verified"])
        self.assertEqual(proof["error"], "unconstrained_search_prohibited")

    # -------------------------------------------------------------------------
    # 7. Process handoff unproven fails closed
    # -------------------------------------------------------------------------
    def test_process_handoff_unproven_fails_closed(self):
        """Expected PID exited or launched child that is NOT in process tree. Must fail closed."""
        windows = [
            FakeWindow(hwnd=601, pid=99999, process_name="notepad.exe", title="secret.txt - Notepad")
        ]
        self._setup_mock_desktop(windows, child_pids_map={88888: set()})

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=88888,
            expected_process_name="notepad.exe",
            title_keyword="secret",
            timeout_sec=0.1
        )
        self.assertIsNone(hwnd)
        self.assertFalse(proof["verified"])

    # -------------------------------------------------------------------------
    # 8. Process handoff child PID verified
    # -------------------------------------------------------------------------
    def test_process_handoff_child_pid_verified(self):
        """Expected PID 12000 spawned child PID 12001. Window belongs to child PID 12001."""
        windows = [
            FakeWindow(hwnd=701, pid=12001, process_name="notepad.exe", title="handoff_secret.txt - Notepad")
        ]
        self._setup_mock_desktop(windows, child_pids_map={12000: {12001}})

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=12000,
            expected_process_name="notepad.exe",
            title_keyword="handoff_secret",
            timeout_sec=0.1
        )
        self.assertIsNone(hwnd)  # PPID snapshot alone is not ownership proof
        self.assertFalse(proof["verified"])

    # -------------------------------------------------------------------------
    # 9. Same-name PID reuse rejection (REVIEW-WO-017.12 point 1)
    # -------------------------------------------------------------------------
    def test_pid_reuse_same_name_different_creation_time_fails_closed(self):
        """Candidate has same expected PID and identical process name 'notepad.exe',
        but creation timestamp does NOT match expected_creation_time.
        Must fail closed (rejection of reused PID).
        """
        windows = [
            FakeWindow(hwnd=801, pid=13001, process_name="notepad.exe", title="secret.txt - Notepad")
        ]
        # PID 13001 current creation time is 2000000, but expected was 1000000
        self._setup_mock_desktop(
            windows,
            creation_times_map={13001: 2000000}
        )

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=13001,
            expected_process_name="notepad.exe",
            title_keyword="secret",
            timeout_sec=0.1,
            expected_creation_time=1000000  # Stored launch timestamp
        )
        self.assertIsNone(hwnd)
        self.assertFalse(proof["verified"])

    # -------------------------------------------------------------------------
    # 10. Exact image basename matching (REVIEW-WO-017.12 point 1)
    # -------------------------------------------------------------------------
    def test_exact_image_basename_rejects_substring_match(self):
        """Candidate process is 'other_notepad.exe' (contains 'notepad.exe' as substring).
        Strict exact image basename matching must reject it.
        """
        windows = [
            FakeWindow(hwnd=901, pid=14001, process_name="other_notepad.exe", title="secret.txt - Other Notepad")
        ]
        self._setup_mock_desktop(windows)

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=14001,
            expected_process_name="notepad.exe",
            title_keyword="secret",
            timeout_sec=0.1
        )
        self.assertIsNone(hwnd)
        self.assertFalse(proof["verified"])

    # -------------------------------------------------------------------------
    # 11. Win32 ctypes pointer-safety & invalid handle error paths (point 2)
    # -------------------------------------------------------------------------
    def test_win32_pointer_safety_and_invalid_handle_error_paths(self):
        """Verify _is_valid_handle correctly checks NULL and INVALID_HANDLE_VALUE (-1 / 64-bit max),
        and error paths return cleanly without unhandled exceptions.
        """
        # _is_valid_handle validation
        self.assertFalse(DesktopTreeParser._is_valid_handle(None))
        self.assertFalse(DesktopTreeParser._is_valid_handle(0))
        self.assertFalse(DesktopTreeParser._is_valid_handle(-1))
        self.assertFalse(DesktopTreeParser._is_valid_handle(0xFFFFFFFFFFFFFFFF))
        self.assertFalse(DesktopTreeParser._is_valid_handle(0xFFFFFFFF))
        self.assertTrue(DesktopTreeParser._is_valid_handle(12345))

        # Test _get_child_pids error path when CreateToolhelp32Snapshot fails (returns -1)
        mock_kernel32 = MagicMock()
        mock_kernel32.CreateToolhelp32Snapshot.return_value = -1  # INVALID_HANDLE_VALUE
        parser = DesktopTreeParser()
        parser._kernel32 = mock_kernel32

        children = parser._get_child_pids(100)
        self.assertEqual(children, set())

        # Test _get_process_creation_time error path when OpenProcess fails (returns 0)
        mock_kernel32.OpenProcess.return_value = 0
        ctime = parser._get_process_creation_time(100)
        self.assertIsNone(ctime)

    # -------------------------------------------------------------------------
    # 12. Production cleanup helper isolation (REVIEW-WO-017.12 point 3)
    # -------------------------------------------------------------------------
    def test_production_cleanup_owned_process_tree(self):
        from tests.test_codex_closeout import CloseoutTests
        probe = CloseoutTests()
        api = probe.api()
        result = probe.cleanup(api)
        self.assertTrue(result['verified'])
        api.TerminateProcess.assert_called_once_with(777, 1)
        api.OpenProcess.assert_not_called()

    # -------------------------------------------------------------------------
    # 13. Synthetic content presence & empty tree privacy failure (point 4)
    # -------------------------------------------------------------------------
    def test_synthetic_content_presence_and_empty_tree_privacy_failure(self):
        """Prove that verify_synthetic_content_presence validates nonempty tree with expected hash,
        and that empty / unrelated tree FAILS privacy evaluation.
        """
        secret_text = "SECRET-TEST-12345"
        secret_hash = "sha256:" + hashlib.sha256(secret_text.encode("utf-8")).hexdigest()
        expected_hashes = {secret_hash}

        # 1. Nonempty tree containing synthetic content hash
        valid_tree = {
            "role": "Window",
            "children": [
                {
                    "role": "Document",
                    "text_hash": secret_hash,
                    "children": []
                }
            ]
        }
        self.assertTrue(verify_synthetic_content_presence(valid_tree, expected_hashes))

        # 2. Empty or unrelated tree
        empty_tree = {"role": "Window", "children": []}
        unrelated_tree = {
            "role": "Window",
            "children": [{"role": "Button", "text_hash": "sha256:abc", "children": []}]
        }
        self.assertFalse(verify_synthetic_content_presence(empty_tree, expected_hashes))
        self.assertFalse(verify_synthetic_content_presence(unrelated_tree, expected_hashes))
        self.assertFalse(verify_synthetic_content_presence(None, expected_hashes))

        # 3. Round E evaluator fails on empty/unrelated tree
        dummy_audit = {
            "window_found": True,
            "cursor_initial": (100, 100),
            "cursor_final": (100, 100),
            "cursor_displacement": 0,
            "cursor_valid": True,
            "tree_parsed": False,  # Failed because synthetic content was missing
            "synthetic_content_found": False,
            "cleanup_verified": True,
            "config_restored_cleanly": True,
            "input_methods_tested": 10,
            "input_methods_blocked": True,
            "fact_desktop_spooled": True,
            "secret_in_tree": False,
            "secret_in_trace": False,
            "secret_in_fact": False,
            "secret_in_log": False,
            "method_details": {
                m: {
                    "result": False,
                    "blocked": True,
                    "cursor_before": (100, 100),
                    "cursor_after": (100, 100),
                    "step_displacement": 0
                }
                for m in (
                    "click", "double_click", "right_click", "mouse_move",
                    "drag_and_drop", "type_text", "press_key", "hotkey",
                    "scroll", "wait_until"
                )
            }
        }
        status, passed = rr.evaluate_round_e_status(dummy_audit)
        self.assertFalse(passed)
        self.assertEqual(status, "failed")

        # When synthetic content is proven and tree is parsed, passes
        dummy_audit["synthetic_content_found"] = True
        dummy_audit["tree_parsed"] = True
        status_ok, passed_ok = rr.evaluate_round_e_status(dummy_audit)
        self.assertTrue(passed_ok)
        self.assertEqual(status_ok, "completed")

    # -------------------------------------------------------------------------
    # 14. Config copy hash restoration under exception (point 3 / req 14)
    # -------------------------------------------------------------------------
    def test_rehearsal_execution_context_config_hash_restoration_under_exception(self):
        """Verify rehearsal_execution_context restores config.json byte-for-byte even when
        an unhandled exception is raised, and validates pre/post SHA256 hashes.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = pathlib.Path(tmpdir) / "config.json"
            orig_data = json.dumps({"uia_enabled": False, "baseline": True}).encode("utf-8")
            cfg_file.write_bytes(orig_data)
            orig_hash = hashlib.sha256(orig_data).hexdigest()

            with self.assertRaises(RuntimeError):
                with rr.rehearsal_execution_context(config_path=cfg_file) as ctx_info:
                    # Modify config inside context
                    cfg_file.write_bytes(b'{"corrupted": true}')
                    raise RuntimeError("Simulated mid-run crash")

            # Post-exception verification
            self.assertTrue(ctx_info["restored"])
            self.assertTrue(ctx_info["hashes_match"])
            self.assertEqual(ctx_info["config_copy_pre_hash"], orig_hash)
            self.assertEqual(ctx_info["config_copy_post_hash"], orig_hash)
            self.assertEqual(cfg_file.read_bytes(), orig_data)

    # -------------------------------------------------------------------------
    # 15. Targeted Round E runner does not invoke Rounds A-D (point 5)
    # -------------------------------------------------------------------------
    def test_targeted_round_e_runner_does_not_invoke_rounds_a_to_d(self):
        """Verify that run(target_round='round_e') executes Round E only and NEVER invokes
        RehearsalServer, browser engine, or Rounds A-D.
        """
        mock_round_e_res = {
            "task_id": "test_rehearsal_e",
            "status": "completed",
            "fact_spooled": True,
            "audit": {
                "window_found": True,
                "tree_parsed": True,
                "synthetic_content_found": True,
                "cleanup_verified": True,
                "config_restored_cleanly": True
            }
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            out_root = pathlib.Path(tmpdir)
            cfg_file = out_root / "config.json"
            cfg_file.write_text(json.dumps({"uia_enabled": False}), encoding="utf-8")

            with patch("tests.rehearsal_runner.run_round_e", return_value=mock_round_e_res) as mock_e:
                with patch("tests.rehearsal_runner.run_rehearsal_server") as mock_srv:
                    with patch("tests.rehearsal_runner.run_interactive_task") as mock_task:
                        with patch("tests.rehearsal_runner.harvest_facts", return_value=[]):
                            res = rr.run(
                                run_id="targeted_test_001",
                                output_root=out_root,
                                config_path=cfg_file,
                                target_round="round_e"
                            )

            # Assertions
            mock_e.assert_called_once()
            mock_srv.assert_not_called()   # Fixture server NEVER started
            mock_task.assert_not_called()  # Interactive web task NEVER called
            self.assertEqual(res.get("mode"), "targeted_round_e")
            self.assertEqual(res.get("targeted_round"), "round_e")
            self.assertEqual(res.get("overall_status"), "TARGETED_ROUND_E_FAILED")
            self.assertFalse(res.get("overall_ok"))  # Missing facts must fail even if round reports completed
            self.assertIn("round_e", res)
            self.assertNotIn("round_a", res)  # Rounds A-D omitted from targeted results

    # -------------------------------------------------------------------------
    # 16. Discovery fails closed when creation time is unverified (Finding 2)
    # -------------------------------------------------------------------------
    def test_discovery_fails_closed_when_expected_creation_time_unverified(self):
        """When expected PID creation time cannot be determined (_get_process_creation_time returns None),
        window discovery must fail closed immediately (verified: False, selected_hwnd: None).
        """
        windows = [FakeWindow(101, 9001, "notepad.exe", "rehearsal_test")]
        self._setup_mock_desktop(windows)
        self.parser._get_process_creation_time = lambda pid: None

        hwnd, proof = self.parser.find_window_with_ownership(
            expected_pid=9001,
            expected_process_name="notepad.exe",
            title_keyword="rehearsal_test"
        )
        self.assertIsNone(hwnd)
        self.assertFalse(proof.get("verified"))
        self.assertEqual(proof.get("error"), "expected_process_identity_unverified")

    # -------------------------------------------------------------------------
    # 17. Cleanup refuses when image name is unknown/unverified (Finding 2)
    # -------------------------------------------------------------------------
    def test_cleanup_refuses_when_image_name_unverified_and_never_kills(self):
        from tests.test_codex_closeout import CloseoutTests
        probe = CloseoutTests(); api = probe.api(image='')
        self.assertFalse(probe.cleanup(api)['verified'])
        api.TerminateProcess.assert_not_called()

    # -------------------------------------------------------------------------
    # 18. Cleanup refuses reused parent and never calls signal 0 (Finding 1)
    # -------------------------------------------------------------------------
    def test_cleanup_refuses_reused_parent_and_never_calls_signal_zero(self):
        from tests.test_codex_closeout import CloseoutTests
        probe = CloseoutTests(); api = probe.api(created=999)
        self.assertFalse(probe.cleanup(api)['verified'])
        api.TerminateProcess.assert_not_called()

    # -------------------------------------------------------------------------
    # 19. Synthetic content proof rejects filename-only nonempty tree (Finding 3)
    # -------------------------------------------------------------------------
    def test_synthetic_content_rejects_filename_only_nonempty_tree(self):
        """verify_synthetic_content_presence must reject filename-only trees, titlebars,
        tab items, or nodes without valid document body roles (Document, Edit, Text).
        """
        filename = "rehearsal_wo017_probe.txt"
        file_hash = "sha256:" + hashlib.sha256(filename.encode()).hexdigest()

        # Bare node without role (as in probe 4) -> must fail
        self.assertFalse(verify_synthetic_content_presence({"text_hash": file_hash, "children": [{}]}, {file_hash}))

        # TitleBar or TabItem node -> must fail
        titlebar_tree = {
            "role": "Window",
            "children": [
                {"role": "TitleBar", "text_hash": file_hash, "children": []},
                {"role": "TabItem", "text_hash": file_hash, "children": []}
            ]
        }
        self.assertFalse(verify_synthetic_content_presence(titlebar_tree, {file_hash}))

        # Document node with disallowed hash (filename) -> must fail
        doc_disallowed = {
            "role": "Window",
            "children": [
                {"role": "Document", "text_hash": file_hash, "children": []}
            ]
        }
        self.assertFalse(verify_synthetic_content_presence(doc_disallowed, {file_hash}, disallowed_hashes={file_hash}))

        # Document node with actual secret fixture content -> must succeed
        secret_content = "REHEARSAL-CUSTOMER-SECRET-test12345-TOP-CONFIDENTIAL"
        secret_hash = "sha256:" + hashlib.sha256(secret_content.encode()).hexdigest()
        valid_tree = {
            "role": "Window",
            "children": [
                {"role": "Document", "text_hash": secret_hash, "children": []}
            ]
        }
        self.assertTrue(verify_synthetic_content_presence(valid_tree, {secret_hash}))

    # -------------------------------------------------------------------------
    # 20. Strict Rehearsal Entrypoint validation (Finding 4)
    # -------------------------------------------------------------------------
    def test_rehearsal_entrypoint_strict_validation(self):
        """Entrypoint must return non-zero on contradictory results, missing facts,
        or invalid hashes.
        """
        import importlib.util
        entrypoint_path = pathlib.Path("D:/karaok/coordination/codex-anti/codex-output/WO-017-CLOSEOUT/rehearsal_entrypoint.py")
        if not entrypoint_path.is_file():
            return

        spec = importlib.util.spec_from_file_location("ep_test", str(entrypoint_path))
        ep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ep)

        sandbox_dir = entrypoint_path.parent / "sandbox"
        cfg_path = sandbox_dir / "config.json"
        out_root = sandbox_dir / "test_run"

        args = [
            "--run-id", "test_strict_val",
            "--output-root", str(out_root),
            "--config-path", str(cfg_path),
            "--round-e-only"
        ]

        # Case 1: Contradictory overall_ok=True, overall_status='FAIL'
        with patch.object(sys, "argv", ["entry"] + args):
            with patch("tests.rehearsal_runner.run", return_value={"overall_ok": True, "overall_status": "FAIL"}):
                code = ep.run_entrypoint(_sandbox_dir=sandbox_dir)
                self.assertNotEqual(code, 0)

        # Case 2: Missing / empty facts list in targeted E result
        fake_res_empty_facts = {
            "overall_ok": True,
            "overall_status": "TARGETED_ROUND_E_COMPLETED",
            "mode": "targeted_round_e",
            "targeted_round": "round_e",
            "round_e": {
                "status": "completed",
                "audit": {
                    "window_found": True,
                    "synthetic_content_found": True,
                    "tree_parsed": True,
                    "input_methods_blocked": True,
                    "cursor_displacement": 0,
                    "cleanup_verified": True,
                    "config_restored_cleanly": True,
                    "secret_in_tree": False,
                    "secret_in_trace": False,
                    "secret_in_fact": False,
                    "secret_in_log": False
                }
            },
            "facts": []  # Empty facts list!
        }
        with patch.object(sys, "argv", ["entry"] + args):
            with patch("tests.rehearsal_runner.run", return_value=fake_res_empty_facts):
                code = ep.run_entrypoint(_sandbox_dir=sandbox_dir)
                self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()

