"""Offline native-boundary tests. No real COM, windows, mouse, or keyboard calls."""
import ctypes
import json
import pathlib
import sys
import threading
import tempfile
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from execution.guarded_desktop import Action, GuardedDesktopSession, InputDenied, Target, action_from_json
from execution.win32_input import INPUT, Win32InputBackend


class FakeBackend:
    def input_scope(self):
        return nullcontext()

    def __init__(self):
        self.target = Target(100, 200, 300, r"C:\Windows\notepad.exe", (0, 0, 100, 100))
        self.reason = None
        self.calls = []
        self.closed = False
        self.on_step = lambda: None

    def capture(self):
        return self.target

    def validate(self, target, action, point=None):
        if self.reason:
            raise InputDenied(self.reason)

    def perform(self, action, guard):
        for _ in range(3 if action.kind == "drag" else 1):
            guard()
            self.calls.append(action.kind)
            self.on_step()

    def close(self):
        self.closed = True


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.records = []
        self.now = 0
        self.paused = False
        self.session = GuardedDesktopSession(self.backend, enabled=True,
            allowed_images=[self.backend.target.image], stop_requested=lambda: self.paused,
            audit=self.records.append, clock=lambda: self.now)
        self.session.bind()

    def prepare(self, kind="click", args=None):
        return self.session.prepare({"kind": kind, "args": args if args is not None else [10, 20]})[0]

    def test_disabled_by_default(self):
        with self.assertRaises(InputDenied):
            GuardedDesktopSession(self.backend, audit=self.records.append).bind()

    def test_empty_allowlist_denies(self):
        with self.assertRaises(InputDenied):
            GuardedDesktopSession(self.backend, enabled=True, audit=self.records.append).bind()

    def test_allowlist_is_exact_not_basename(self):
        with self.assertRaises(InputDenied):
            GuardedDesktopSession(self.backend, enabled=True, allowed_images=["notepad.exe"], audit=self.records.append).bind()

    def test_success_does_not_claim_verification(self):
        result = self.session.execute(self.prepare())
        self.assertEqual(result["status"], "input_submitted")
        self.assertFalse(result["verified"])

    def test_all_four_actions(self):
        for kind, args in [("click", [2, 3]), ("drag", [2, 3, 8, 9]), ("type", ["ทดสอบ😀"]), ("key", ["LEFT"])]:
            self.session.execute(self.prepare(kind, args))
        self.assertEqual(self.session.count, 4)

    def test_no_approval(self):
        with self.assertRaises(InputDenied):
            self.session.execute("unknown")
        self.assertFalse(self.backend.calls)

    def test_replay_rejected(self):
        token = self.prepare()
        self.session.execute(token)
        with self.assertRaises(InputDenied):
            self.session.execute(token)
        self.assertEqual(self.backend.calls, ["click"])

    def test_old_token_cannot_authorize_new_action(self):
        old = self.prepare()
        self.prepare("key", ["ENTER"])
        with self.assertRaises(InputDenied):
            self.session.execute(old)
        self.assertFalse(self.backend.calls)

    def test_approval_expires(self):
        token = self.prepare()
        self.now = 30
        with self.assertRaises(InputDenied):
            self.session.execute(token)
        self.assertFalse(self.backend.calls)

    def test_session_expires(self):
        self.now = 300
        with self.assertRaises(InputDenied):
            self.prepare()

    def test_control_pause_and_unknown_deny(self):
        for paused in [True, None, "false", 0]:
            with self.subTest(paused=paused):
                self.setUp()
                token = self.prepare()
                self.paused = paused
                with self.assertRaises(InputDenied):
                    self.session.execute(token)
                self.assertFalse(self.backend.calls)

    def test_external_stop_denies(self):
        token = self.prepare()
        self.session.stop()
        with self.assertRaises(InputDenied):
            self.session.execute(token)
        self.assertFalse(self.backend.calls)

    def test_stop_mid_drag_no_retry(self):
        self.backend.on_step = self.session.stop
        with self.assertRaises(InputDenied):
            self.session.execute(self.prepare("drag", [2, 3, 8, 9]))
        self.assertEqual(self.backend.calls, ["drag"])
        with self.assertRaises(InputDenied):
            self.prepare()

    def test_native_guard_failure_stops(self):
        token = self.prepare()
        self.backend.reason = "target_changed"
        with self.assertRaises(InputDenied):
            self.session.execute(token)
        self.assertFalse(self.backend.calls)
        self.assertTrue(self.session.stopped.is_set())

    def test_audit_intent_failure_prevents_input(self):
        token = self.prepare()
        self.session.audit = Mock(side_effect=OSError("disk unavailable"))
        with self.assertRaises(OSError):
            self.session.execute(token)
        self.assertFalse(self.backend.calls)

    def test_audit_contains_no_text_or_hash(self):
        self.session.execute(self.prepare("type", ["private dummy text"]))
        record = json.dumps(self.records)
        self.assertNotIn("private", record)
        self.assertNotIn("hash", record)
        self.assertEqual([r["event"] for r in self.records], ["bound", "input_intent", "input_submitted"])

    def test_action_budget(self):
        self.session.count = 30
        with self.assertRaises(InputDenied):
            self.prepare()

    def test_coordinates_client_only(self):
        for args in [[-1, 0], [100, 20], [0, 100]]:
            with self.assertRaises(InputDenied):
                self.prepare(args=args)
        with self.assertRaises(InputDenied):
            self.prepare("drag", [10, 10, 101, 20])

    def test_strict_schema(self):
        cases = [None, [], {}, {"kind":"click", "args":[True, 0]},
                 {"kind":"click", "args":[1.2, 3]}, {"kind":"key", "args":["WIN"]},
                 {"kind":"key", "args":["CTRL+S"]}, {"kind":"type", "args":["abc\n"]},
                 {"kind":"type", "args":["x"*501]}, {"kind":"type", "args":[""]},
                 {"kind":"type", "args":["\ud800"]}, {"kind":"shell", "args":["cmd"]},
                 {"kind":"key", "args":["LEFT"], "extra":True}]
        for data in cases:
            with self.subTest(data=repr(data)), self.assertRaises(InputDenied):
                action_from_json(data)

    def test_close_releases_backend(self):
        self.session.close()
        self.assertTrue(self.backend.closed)
        with self.assertRaises(InputDenied):
            self.session.bind()

    def test_concurrent_execution_denied(self):
        token = self.prepare()
        self.session.lock.acquire()
        try:
            with self.assertRaises(InputDenied):
                self.session.execute(token)
        finally:
            self.session.lock.release()
        self.assertFalse(self.backend.calls)


class NativeBoundaryTests(unittest.TestCase):
    def test_input_scope_releases_lock_on_exception(self):
        native = Win32InputBackend.__new__(Win32InputBackend)
        native.mutex = None
        native.k = SimpleNamespace(CreateMutexW=Mock(return_value=10), WaitForSingleObject=Mock(return_value=0), ReleaseMutex=Mock(), CloseHandle=Mock())
        with self.assertRaises(RuntimeError):
            with native.input_scope():
                raise RuntimeError("failed input")
        native.k.ReleaseMutex.assert_called_once_with(10)
        native.k.CloseHandle.assert_called_once_with(10)
        self.assertIsNone(native.mutex)

    def test_capture_does_not_hold_input_lock_while_operator_is_idle(self):
        native = Win32InputBackend.__new__(Win32InputBackend)
        native.handle = None
        native._desktop_safe = Mock()
        native._capture_target = Mock(return_value="target")
        native.k = Mock()
        self.assertEqual(native.capture(), "target")
        native.k.CreateMutexW.assert_not_called()

    def setUp(self):
        # Deliberately bypass __init__: no WinDLL loading or COM creation.
        self.native = Win32InputBackend.__new__(Win32InputBackend)
        self.native.u = SimpleNamespace(SetCursorPos=Mock(return_value=True), GetAsyncKeyState=Mock(return_value=0))
        self.sent = []
        self.native._send = lambda events: self.sent.extend(events)

    def test_ctypes_input_layout(self):
        self.assertEqual(ctypes.sizeof(INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_mouse_release_on_revoked_drag(self):
        guard = Mock(side_effect=[None, None, InputDenied("stopped")])
        with self.assertRaises(InputDenied):
            self.native.perform(Action("drag", (0, 0, 10, 10)), guard)
        self.assertEqual([e.mi.dwFlags for e in self.sent], [2, 4])

    def test_key_release_even_on_partial_send(self):
        events = []
        def send(items):
            events.extend(items)
            if len(events) == 1:
                raise InputDenied("partial")
        self.native._send = send
        with self.assertRaises(InputDenied):
            self.native.perform(Action("key", ("LEFT",)), lambda *_: None)
        self.assertEqual([e.ki.dwFlags for e in events], [1, 3])

    def test_unicode_preserved_as_utf16(self):
        self.native.perform(Action("type", ("ก😀",)), lambda *_: None)
        units = [e.ki.wScan for e in self.sent if e.ki.dwFlags == 4]
        self.assertEqual(b"".join(u.to_bytes(2, "little") for u in units).decode("utf-16-le"), "ก😀")

    def test_move_failed_sends_no_button(self):
        self.native.u.SetCursorPos.return_value = False
        with self.assertRaises(InputDenied):
            self.native.perform(Action("click", (0, 0)), lambda *_: None)
        self.assertFalse(self.sent)

    def test_guard_before_every_character(self):
        guard = Mock(side_effect=[None, InputDenied("focus changed")])
        with self.assertRaises(InputDenied):
            self.native.perform(Action("type", ("ab",)), guard)
        self.assertEqual(len(self.sent), 2)

    def test_short_send_raises_no_retry(self):
        self.native.u.SendInput = Mock(return_value=0)
        with self.assertRaises(InputDenied):
            Win32InputBackend._send(self.native, [self.native._mouse(2)])
        self.native.u.SendInput.assert_called_once()

    def test_physical_mouse_held_denies(self):
        self.native.u.GetAsyncKeyState.return_value = 0x8000
        with self.assertRaises(InputDenied):
            self.native.perform(Action("click", (0, 0)), lambda *_: None)
        self.assertFalse(self.sent)


class NativeValidationTests(unittest.TestCase):
    def setUp(self):
        self.native = Win32InputBackend.__new__(Win32InputBackend)
        self.native.hwnd, self.native.handle, self.native.root_id = 100, 99, (1, 2)
        self.target = Target(100, 200, 300, r"C:\Windows\notepad.exe", (0, 0, 100, 100))
        self.native._desktop_safe = Mock()
        self.native._identity = Mock(return_value=(300, self.target.image))
        self.native._rect = Mock(return_value=self.target.rect)
        self.element = SimpleNamespace(CurrentProcessId=200, CurrentIsEnabled=True,
            CurrentIsOffscreen=False, CurrentIsPassword=False, CurrentName="Text editor",
            CurrentAutomationId="text", CurrentClassName="Edit", CurrentControlType=50004,
            GetRuntimeId=lambda: (3, 4))
        self.root = SimpleNamespace(GetRuntimeId=lambda: (1, 2))
        def pid(_hwnd, output):
            output._obj.value = 200
        self.native.u = SimpleNamespace(GetAsyncKeyState=Mock(return_value=0),
            GetWindowThreadProcessId=pid, GetForegroundWindow=Mock(return_value=100),
            IsWindowVisible=Mock(return_value=True), IsWindowEnabled=Mock(return_value=True),
            GetWindowTextW=Mock(return_value=0), WindowFromPoint=Mock(return_value=100),
            GetAncestor=Mock(return_value=100))
        self.native.mod = SimpleNamespace(tagPOINT=lambda: SimpleNamespace(x=0,y=0))
        self.native.uia = SimpleNamespace(ElementFromHandle=Mock(return_value=self.root),
            GetFocusedElement=Mock(return_value=self.element), ElementFromPoint=Mock(return_value=self.element),
            RawViewWalker=SimpleNamespace(GetParentElement=Mock(return_value=self.root)))

    def validate(self, action=None):
        self.native.validate(self.target, action or Action("type", ("test",)))

    def test_matching_target_passes(self):
        self.validate()

    def test_foreground_changed_denies(self):
        self.native.u.GetForegroundWindow.return_value = 999
        with self.assertRaises(InputDenied):
            self.validate()

    def test_reused_process_denies(self):
        self.native._identity.return_value = (999, self.target.image)
        with self.assertRaises(InputDenied):
            self.validate()

    def test_changed_executable_denies(self):
        self.native._identity.return_value = (300, "other.exe")
        with self.assertRaises(InputDenied):
            self.validate()

    def test_changed_hwnd_runtime_denies(self):
        self.root.GetRuntimeId = lambda: (8, 8)
        with self.assertRaises(InputDenied):
            self.validate()

    def test_moved_window_denies(self):
        self.native._rect.return_value = (1, 0, 101, 100)
        with self.assertRaises(InputDenied):
            self.validate()

    def test_password_and_semantic_sensitive_denied(self):
        self.element.CurrentIsPassword = True
        with self.assertRaises(InputDenied):
            self.validate()
        self.element.CurrentIsPassword = False
        self.element.CurrentName = "OTP"
        with self.assertRaises(InputDenied):
            self.validate()

    def test_wrong_focused_window_same_process_denies(self):
        self.native.uia.RawViewWalker.GetParentElement.return_value = None
        with self.assertRaises(InputDenied):
            self.validate()

    def test_occluded_point_denies(self):
        self.native.u.GetAncestor.return_value = 999
        with self.assertRaises(InputDenied):
            self.validate(Action("click", (10, 10)))

    def test_modifier_or_escape_held_denies(self):
        self.native.u.GetAsyncKeyState.return_value = 0x8000
        with self.assertRaises(InputDenied):
            self.validate()

    def test_secure_desktop_denies(self):
        self.native._desktop_safe.side_effect = InputDenied("secure desktop")
        with self.assertRaises(InputDenied):
            self.validate()

    def test_unknown_uia_fails_closed(self):
        self.native.uia.GetFocusedElement.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_noneditable_type_denies(self):
        self.element.CurrentControlType = 50000
        with self.assertRaises(InputDenied):
            self.validate()


class EntryAndCompatibilityTests(unittest.TestCase):
    def test_readback_requires_actual_text(self):
        from service.easy_desktop_test import verify_test_text
        backend = Mock()
        pattern = backend.uia.GetFocusedElement.return_value.GetCurrentPattern.return_value.QueryInterface.return_value
        pattern.DocumentRange.GetText.return_value = " TEST-456"
        self.assertTrue(verify_test_text(backend, Mock())["expected_suffix_matches"])
        pattern.DocumentRange.GetText.return_value = "wrong"
        self.assertFalse(verify_test_text(backend, Mock())["expected_suffix_matches"])

    def test_classic_notepad_value_pattern_fallback(self):
        from service.easy_desktop_test import verify_test_text
        backend = Mock()
        value_pattern = Mock()
        value_pattern.CurrentValue = " TEST-456"
        value_unknown = Mock()
        value_unknown.QueryInterface.return_value = value_pattern
        backend.uia.GetFocusedElement.return_value.GetCurrentPattern.side_effect = [ValueError("NULL COM pointer"), value_unknown]
        self.assertTrue(verify_test_text(backend, Mock())["expected_suffix_matches"])

    def test_easy_gui_new_notepad_requires_exact_created_pid(self):
        from service import easy_desktop_test as gui
        app = gui.EasyTest.__new__(gui.EasyTest)
        app.root = Mock()
        app.backend = Mock()
        app.created_notepad = SimpleNamespace(pid=22)
        app.backend.list_allowed_windows.return_value = [
            {"title":"Untitled - Notepad", "pid":11}, {"title":"Untitled - Notepad", "pid":22}]
        app.show_candidates = Mock()
        with patch.object(gui, "stop_requested", return_value=False):
            app.refresh_created_notepad()
        self.assertEqual([w["pid"] for w in app.candidates], [22])
        app.show_candidates.assert_called_once()

    def test_easy_gui_opening_can_be_stopped(self):
        from service import easy_desktop_test as gui
        app = gui.EasyTest.__new__(gui.EasyTest)
        app.backend = Mock()
        app.failure = Mock()
        with patch.object(gui, "stop_requested", return_value=True):
            app.refresh_created_notepad()
        app.backend.list_allowed_windows.assert_not_called()
        app.failure.assert_called_once()

    def test_easy_gui_construction_does_not_initialize_native(self):
        from service import easy_desktop_test as gui
        with patch.object(gui.tk, "Label"), patch.object(gui.tk, "Button"), patch.object(gui.tk, "Listbox"), patch.object(gui.tk, "StringVar"), patch("ctypes.WinDLL", side_effect=AssertionError("native forbidden")):
            app = gui.EasyTest(Mock())
        self.assertIsNone(app.backend)
        self.assertIsNone(app.session)

    def test_easy_gui_filters_out_named_work_documents(self):
        from service.easy_desktop_test import test_documents
        items = [{"title": title} for title in ["lyrics.txt - Notepad", "Untitled - Notepad", "*Untitled - Notepad", "cmd"]]
        self.assertEqual(test_documents(items), items[1:3])

    def test_easy_gui_does_not_send_until_target_ready(self):
        from service.easy_desktop_test import EasyTest
        app = EasyTest.__new__(EasyTest)
        app.root = Mock()
        app.backend = Mock()
        app.backend.target_ready.return_value = False
        app.session = Mock()
        app.session.clock.return_value = 0
        app.session.pending = ("token", None, 30)
        app.poll()
        app.session.execute.assert_not_called()
        app.root.after.assert_called_once()

    def test_easy_gui_expired_approval_never_sends(self):
        from service.easy_desktop_test import EasyTest
        app = EasyTest.__new__(EasyTest)
        app.session = Mock()
        app.session.clock.return_value = 31
        app.session.pending = ("token", None, 30)
        app.failure = Mock()
        app.poll()
        app.session.execute.assert_not_called()
        app.failure.assert_called_once()

    def test_easy_gui_selection_identity_must_match(self):
        from service.easy_desktop_test import same_target
        target = Target(1, 2, 3, "C:\\Windows\\notepad.exe", (0,0,100,100))
        selected = {"hwnd":1, "pid":2, "created":3, "image":target.image}
        self.assertTrue(same_target(target, selected))
        selected["created"] = 4
        self.assertFalse(same_target(target, selected))

    def test_picker_selects_exact_number(self):
        from service.desktop_console import pick_window
        entries = [{"title": "working song", "pid": 1}, {"title": "Untitled - Notepad", "pid": 2}]
        with patch("builtins.input", return_value="2"), patch("builtins.print"):
            self.assertIs(pick_window(entries), entries[1])

    def test_picker_invalid_or_empty_denies(self):
        from service.desktop_console import pick_window
        with self.assertRaises(InputDenied):
            pick_window([])
        for answer in ["0", "2", "STOP", "", "-1"]:
            with patch("builtins.input", return_value=answer), patch("builtins.print"), self.assertRaises(InputDenied):
                pick_window([{"title":"Untitled", "pid":1}])

    def test_waits_for_target_without_sending_or_extending_approval(self):
        from service.desktop_console import wait_for_target
        now = [0]
        session = SimpleNamespace(pending=("token", None, 5), _check_stop=Mock())
        backend = Mock()
        backend.target_ready.side_effect = [False, False, True]
        wait_for_target(backend, session, clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0]+n))
        self.assertEqual(backend.target_ready.call_count, 3)
        backend.perform.assert_not_called()
        self.assertEqual(session.pending[2], 5)

    def test_focus_wait_expiry_sends_no_input(self):
        from service.desktop_console import wait_for_target
        now = [0]
        session = SimpleNamespace(pending=("token", None, .2), _check_stop=Mock())
        backend = Mock()
        backend.target_ready.return_value = False
        with self.assertRaises(InputDenied):
            wait_for_target(backend, session, clock=lambda: now[0], sleep=lambda n: now.__setitem__(0, now[0]+n))
        backend.perform.assert_not_called()

    def test_native_initialization_failure_is_saved_without_sensitive_message(self):
        from service import desktop_console
        with tempfile.TemporaryDirectory() as folder:
            args = ["desktop_console.py", "--enable-input", "--select-foreground", "--allow-exe", sys.executable]
            with patch.object(desktop_console, "APP", pathlib.Path(folder)), patch.object(sys, "argv", args), patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True), patch.object(desktop_console, "stop_requested", return_value=False), patch("builtins.input", return_value="ENABLE"), patch("builtins.print"), patch("execution.win32_input.Win32InputBackend", side_effect=RuntimeError("private error detail")):
                self.assertEqual(desktop_console.main(), 1)
            text = next(pathlib.Path(folder).rglob("*.jsonl")).read_text("utf-8")
            records = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(records[-1]["stage"], "native_initialization")
            self.assertEqual(records[-1]["event"], "session_error")
            self.assertNotIn("private error detail", text)

    def test_binding_failure_is_saved_and_backend_closed(self):
        from service import desktop_console
        backend = Mock()
        backend.capture.side_effect = InputDenied("executable_not_allowed")
        with tempfile.TemporaryDirectory() as folder:
            args = ["desktop_console.py", "--enable-input", "--select-foreground", "--allow-exe", sys.executable]
            with patch.object(desktop_console, "APP", pathlib.Path(folder)), patch.object(sys, "argv", args), patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True), patch.object(desktop_console, "stop_requested", return_value=False), patch("builtins.input", return_value="ENABLE"), patch("builtins.print"), patch("execution.win32_input.Win32InputBackend", return_value=backend), patch.object(desktop_console.time, "sleep"):
                self.assertEqual(desktop_console.main(), 1)
            record = json.loads(next(pathlib.Path(folder).rglob("*.jsonl")).read_text("utf-8").splitlines()[-1])
            self.assertEqual(record["stage"], "binding_target")
            self.assertEqual(record["reason"], "executable_not_allowed")
            backend.close.assert_called_once()

    def test_console_default_never_loads_backend(self):
        from service import desktop_console
        with patch.object(sys, "argv", ["desktop_console.py"]), patch("builtins.print"), patch("ctypes.WinDLL", side_effect=AssertionError("native not allowed")):
            self.assertEqual(desktop_console.main(), 2)

    def test_console_rejects_piped_optin(self):
        from service import desktop_console
        with patch.object(sys, "argv", ["desktop_console.py", "--enable-input"]), patch.object(sys.stdin, "isatty", return_value=False), patch("builtins.print"), patch("ctypes.WinDLL", side_effect=AssertionError("native not allowed")):
            self.assertEqual(desktop_console.main(), 2)

    def test_missing_control_is_stop(self):
        from service import desktop_console
        with patch.object(pathlib.Path, "read_text", side_effect=OSError("missing")), patch.object(pathlib.Path, "exists", return_value=False):
            self.assertTrue(desktop_console.stop_requested())

    def test_original_read_only_adapter_still_blocks(self):
        from execution.windows_uia import WindowsUIAAdapter
        adapter = WindowsUIAAdapter.__new__(WindowsUIAAdapter)
        with patch("logging.Logger.warning"):
            self.assertFalse(adapter.click(1, 2))
            self.assertFalse(adapter.type_text("test"))
            self.assertFalse(adapter.drag_and_drop(1, 2, 3, 4))
            self.assertFalse(adapter.press_key("ENTER"))

    def test_named_mutex_busy_blocks_before_dpi_or_capture(self):
        native = Win32InputBackend.__new__(Win32InputBackend)
        native.hwnd, native.handle, native.mutex, native.old_dpi = 100, None, None, None
        native._desktop_safe = Mock()
        native.k = SimpleNamespace(CreateMutexW=Mock(return_value=10), WaitForSingleObject=Mock(return_value=258),
                                   CloseHandle=Mock(), ReleaseMutex=Mock())
        native.u = SimpleNamespace(SetThreadDpiAwarenessContext=Mock())
        with self.assertRaises(InputDenied):
            with native.input_scope():
                self.fail("busy mutex entered")
        native.u.SetThreadDpiAwarenessContext.assert_not_called()
        native.k.CloseHandle.assert_called_once_with(10)

    def test_abandoned_mutex_released_but_no_input(self):
        native = Win32InputBackend.__new__(Win32InputBackend)
        native.hwnd, native.handle, native.mutex, native.old_dpi = 100, None, None, None
        native._desktop_safe = Mock()
        native.k = SimpleNamespace(CreateMutexW=Mock(return_value=10), WaitForSingleObject=Mock(return_value=128),
                                   CloseHandle=Mock(), ReleaseMutex=Mock())
        with self.assertRaises(InputDenied):
            with native.input_scope():
                self.fail("abandoned mutex entered")
        native.k.ReleaseMutex.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
