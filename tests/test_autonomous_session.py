"""Tests: AutonomousFrameSession (โหมดอิสระภายในกรอบ ระดับ A) + approval popup.

ยึดหลัก: กฎห้ามเดิมทุกข้อยังบังคับ (schema/bounds/stop/budget) — ใหม่คือ approval รายงานไม่รายคำสั่ง
"""
import importlib
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = pathlib.Path("D:/karaok/ai-platform-kit/apps/browser-engine-desktop")
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

TMP = tempfile.mkdtemp(prefix="autonomy_test_")


from execution.guarded_desktop import Target, InputDenied


class FakeBackend:
    """Mock ชนิดเดียวกับของ test_guarded_desktop — ไม่แตะ OS จริง"""
    def __init__(self, image=r"C:\Windows\System32\notepad.exe"):
        self.image = image
        self.rect = (100, 100, 900, 700)
        self.target_ready_flag = True
        self.performed = []
        self.validations = 0

    def capture(self):
        return Target(hwnd=4321, pid=777, created=1234567890, image=self.image, rect=self.rect)

    def target_ready(self):
        return self.target_ready_flag

    def input_scope(self):
        from contextlib import contextmanager

        @contextmanager
        def _scope():
            yield
        return _scope()

    def validate(self, target, action, point=None):
        self.validations += 1

    def perform(self, action, guard):
        self.performed.append(action)

    def close(self):
        pass


class _T(unittest.TestCase):
    def setUp(self):
        self.asmod = importlib.reload(importlib.import_module("execution.autonomous_session"))
        self.asmod.AUTONOMY_STOP = pathlib.Path(TMP) / "AUTONOMY-STOP"
        self.audits = []
        self.audit = lambda r: self.audits.append(dict(r, at="t"))

    def mk(self, **kw):
        b = kw.pop("backend", FakeBackend())
        s = self.asmod.AutonomousFrameSession(b, audit=self.audit,
                                              require_task_approval=False, **kw)
        s.start()
        return s, b

    def test_approval_per_task_not_per_action(self):
        s, b = self.mk(max_actions=50)
        act = lambda i, k="click", a=(200, 200): (s.do({"kind": k, "args": list(a)}), None)[1]
        for i in range(5):
            r = s.do({"kind": "click", "args": [200 + i, 200]})
        self.assertEqual(len(b.performed), 5)
        self.assertEqual(s.actions_done, 5)
        kinds = [a["event"] for a in self.audits]
        self.assertEqual(kinds.count("autonomy_task_requested"), 1)  # ขออนุมัติงานครั้งเดียว
        self.assertNotIn("approval_declined", kinds)
        s.close()

    def test_budget_exhausted(self):
        s, b = self.mk(max_actions=3)
        for i in range(3):
            s.do({"kind": "click", "args": [200 + i, 200]})
        with self.assertRaises(self.asmod.InputDenied) as cm:
            s.do({"kind": "click", "args": [400, 400]})
        self.assertIn("budget", str(cm.exception))
        self.assertEqual(len(b.performed), 3)
        s.close()

    def test_stop_file_rejects_next_action(self):
        s, b = self.mk()
        s.do({"kind": "click", "args": [200, 200]})
        self.asmod.AUTONOMY_STOP.write_text("stop")
        with self.assertRaises(self.asmod.InputDenied):
            s.do({"kind": "click", "args": [200, 200]})
        self.assertEqual(len(b.performed), 1, "งานที่ 2 ต้องไม่เกิด")
        s.close()

    def test_bounds_not_bypassed(self):
        s, b = self.mk()
        with self.assertRaises(Exception):
            s.do({"kind": "click", "args": [9999, 9999]})  # นอกกรอบ — prepare ปฏิเสธ
        self.assertEqual(len(b.performed), 0, "ห้ามกระทำนอกกรอบหน้าต่าง")
        s.close()

    def test_schema_still_enforced(self):
        s, b = self.mk()
        for bad in ({"kind": "shell", "args": ["dir"]}, {"kind": "key", "args": ["WIN"]},
                    {"kind": "type", "args": ["x" * 501]}, {"kind": "click", "args": [1, 2, 3]}):
            with self.assertRaises(Exception):
                s.do(bad)
        self.assertEqual(len(b.performed), 0)
        s.close()

    def test_approval_popup_default_deny(self):
        from execution import approval_ui
        t0 = time.monotonic()
        approved, reason = approval_ui.ask_task_approval("x", {"เป้า": "ทดสอบ"}, timeout_s=1.0)
        self.assertFalse(approved)
        self.assertLessEqual(time.monotonic() - t0, 5)
        # ทั้ง timeout (default deny) และ user_denied คือ "ไม่อนุมัติ" ที่ปลอดภัยเท่ากัน
        self.assertIn(reason, ("approval_timeout_default_deny", "user_denied"))

    def test_approval_popup_reject_flow_is_safe(self):
        """เส้นทาง popup ปฏิเสธ = งานไม่เกิด (ปกป้องโดยค่าเริ่มต้น) — ทดสอบผ่าน session จริงกับ require_task_approval"""
        s = self.asmod.AutonomousFrameSession(FakeBackend(), audit=self.audit,
                                              require_task_approval=True)
        with mock.patch.object(self.asmod.approval_ui, "ask_task_approval",
                               return_value=(False, "approval_timeout_default_deny")):
            with self.assertRaises(self.asmod.InputDenied) as cm:
                s.start()
        self.assertIn("task_approval", str(cm.exception))
        self.assertFalse(any(a["event"] == "autonomy_session_started" for a in self.audits))
        self.assertTrue(any(a["event"] == "autonomy_task_approval" and a.get("approved") is False for a in self.audits))

    def test_approval_popup_accept_flow(self):
        """popup อนุมัติ → เซสชันเริ่มได้ (ทางเดียวกับผู้ใช้กดปุ่มเขียว)"""
        s = self.asmod.AutonomousFrameSession(FakeBackend(), audit=self.audit,
                                              require_task_approval=True, max_actions=5)
        with mock.patch.object(self.asmod.approval_ui, "ask_task_approval",
                               return_value=(True, "approved")):
            s.start()
        r = s.do({"kind": "click", "args": [200, 200]})
        self.assertEqual(r["status"], "input_submitted")
        self.assertEqual(s.actions_done, 1)
        s.close()

    def test_notepad_only_default_allowlist(self):
        b2 = FakeBackend(image=r"C:\Windows\System32\cmd.exe")
        s = self.asmod.AutonomousFrameSession(b2, audit=self.audit, require_task_approval=False)
        with self.assertRaises(Exception):
            s.start()  # cmd = ห้ามเด็ดขาด แม้เปิดงานอนุมัติ
        self.assertFalse(any(a["event"] == "autonomy_session_started" for a in self.audits))

    def test_pause_stops_session(self):
        """control.json อ่านไม่ได้/paused → หยุดเซสชัน (fail-closed) — พฤติกรรมจริงของ guard"""
        s, b = self.mk()
        s.do({"kind": "click", "args": [200, 200]})  # ปกติทำได้
        # จำลอง control.json หาย (อ่านไม่ได้) → stop_requested ต้องเป็นจริง → งานถัดไปถูกปฏิเสธ
        with mock.patch.object(self.asmod, "CONTROL", pathlib.Path(TMP) / "does-not-exist.json"), \
             mock.patch.object(self.asmod, "AUTONOMY_STOP", pathlib.Path(TMP) / "no-stop"):
            with self.assertRaises(self.asmod.InputDenied):
                s.do({"kind": "click", "args": [200, 200]})
        self.assertEqual(len(b.performed), 1, "งานหลัง stop ต้องไม่เกิด")
        s.close()


if __name__ == "__main__":
    unittest.main()