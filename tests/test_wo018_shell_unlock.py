"""Tests: WO-018 — shell runner / ธง unrestricted / ActionManifest (keysign-ready).

ทุกเทสต์ mock/แยกไฟล์ config — ไม่แตะระบบจริง, ไม่รันคำสั่งจริง (ยกเว้น echo ถูก mock)
"""
import importlib
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

BASE = pathlib.Path(__file__).resolve().parent.parent  # repo แยกแล้ว — ห้ามชี้ path เดิมใน ai-platform-kit
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="wo018_test_"))
SR = importlib.import_module("execution.shell_runner")


class _Cfg:
    def __init__(self, **kw):
        self.cfg = {"shell_execution_enabled": False, "shell_elevated_allowed": False,
                    "shell_timeout_seconds": 600, "unrestricted_desktop": {}, **kw}

    def __enter__(self):
        self.p = mock.patch.object(SR, "CONFIG_FILE", TMP / "cfg.json")
        (TMP / "cfg.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        self.p.start()

    def __exit__(self, *a):
        self.p.stop()


class _M(unittest.TestCase):
    def setUp(self):
        self.man = SR.Manifest()
        self.man.path = TMP / "manifest-test.jsonl"

    # ---------- R1: shell runner ----------
    def test_flag_off_rejects_all(self):
        with _Cfg():
            r = SR.ShellRunner(self.man).run("git --version")
        self.assertFalse(r.ok)
        self.assertEqual(r.denided_reason, "shell_execution_disabled")

    def test_empty_command(self):
        with _Cfg(shell_execution_enabled=True):
            r = SR.ShellRunner(self.man).run("   ")
        self.assertFalse(r.ok)
        self.assertEqual(r.denided_reason, "empty_command")

    def test_plain_command_runs_ok(self):
        with _Cfg(shell_execution_enabled=True):
            r = SR.ShellRunner(self.man).run(sys.executable + " -c \"print('wo018-ok')\"")
        self.assertTrue(r.ok, r.stderr)
        self.assertEqual(r.exit_code, 0)
        self.assertIn("wo018-ok", r.stdout)

    def test_failed_command_exit_code_kept(self):
        with _Cfg(shell_execution_enabled=True):
            r = SR.ShellRunner(self.man).run(sys.executable + " -c \"import sys; sys.exit(3)\"")
        self.assertFalse(r.ok)
        self.assertEqual(r.exit_code, 3)

    def test_elevated_requires_flag(self):
        with _Cfg(shell_execution_enabled=True):
            r = SR.ShellRunner(self.man).run("whoami", elevated=True)
        self.assertEqual(r.denided_reason, "shell_elevated_not_allowed")

    def test_manifest_written_with_signature_null(self):
        with _Cfg(shell_execution_enabled=True):
            r = SR.ShellRunner(self.man).run(sys.executable + " -c \"print('m')\"")
        self.assertIsNotNone(r.manifest_entry)
        lines = self.man.path.read_text(encoding="utf-8").strip().splitlines()
        entry = json.loads(lines[-1])
        self.assertEqual(entry["source"], "ai")
        self.assertEqual(entry["kind"], "shell_run")
        self.assertIsNone(entry["signature"], "field signature เว้นไว้ให้ keysign")
        for f in ("seq", "timestamp", "detail", "result", "machine", "engine_version"):
            self.assertIn(f, entry)

    def test_redact_password_like(self):
        s = SR._redact("login: admin password: hunter2 done")
        self.assertNotIn("hunter2", s)
        self.assertIn("***", s)

    # ---------- R2/R3: ธง unrestricted ----------
    def test_flags_default_closed(self):
        from execution import guarded_desktop as gd
        from execution import win32_input as wi
        self.assertFalse(gd._bound_bypassed(), "ธงปิด = กรอบเดิม")
        self.assertTrue(len(wi._blocked_executables_effective()) > 5, "ธงปิด = รายบล็อกยังบังคับ")

    def test_blocked_executables_bypass_flag(self):
        with _Cfg(unrestricted_desktop={"blocked_executables_bypass": True}):
            from execution import win32_input as wi
            self.assertEqual(wi._blocked_executables_effective(), set())

    def test_bound_bypass_extends_frame(self):
        with _Cfg(unrestricted_desktop={"bound_bypass": True}):
            from execution import guarded_desktop as gd
            self.assertTrue(gd._bound_bypassed())
            t = type("T", (), {"rect": (100, 100, 900, 700)})()
            big = gd._extended_bounds(t)
            self.assertLess(big[0], 100)
            self.assertGreater(big[2], 900)


if __name__ == "__main__":
    unittest.main()