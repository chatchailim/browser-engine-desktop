"""Tests: WO-019 M1 Token lifecycle — revoke / expires / re-issue / list states (ใช้ ROLES_DIR จำลอง)."""
import datetime
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

BASE = pathlib.Path(__file__).resolve().parent.parent  # repo แยกแล้ว — ห้ามชี้ path เดิมใน ai-platform-kit
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


class M1Lifecycle(unittest.TestCase):
    def setUp(self):
        from service import roles as roles_mod
        importlib = __import__("importlib")
        self.c = importlib.reload(roles_mod)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="m1_"))
        self.c.ROLES_DIR = self.tmp
        # machine token จำลอง (ต่างจากเครื่องจริง)
        self.pw = pathlib.Path(tempfile.mkdtemp(prefix="m1home_")) / ".browser-engine-desktop"
        self.pw.mkdir(parents=True)
        (self.pw / "token").write_text("MACHINE-TOKEN-x" * 4, encoding="utf-8")
        # จำลอง home ด้วย patch
        self.p = mock.patch.object(
            self.c, "_machine_token",
            lambda: (self.pw / "token").read_text(encoding="utf-8").strip())
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_add_with_expires(self):
        r = self.c.add_user("somchai", "operator", "2026-12-31")
        self.assertEqual(r["expires"], "2026-12-31")
        tok = r["token"]
        self.assertEqual(self.c.resolve_role(tok), "operator")

    def test_expired_token_denied(self):
        r = self.c.add_user("korn", "operator", "2020-01-01")  # วันที่ผ่านมาแล้ว
        self.assertEqual(self.c.resolve_role(r["token"]), "")  # 401 เชิง role ว่าง

    def test_bad_expires_format_fail_closed(self):
        r = self.c.add_user("mai", "viewer", "31/12/2026")
        self.assertIn("error", r)
        r2 = self.c.add_user("mai2", "viewer", "not-a-date")
        self.assertIn("error", r2)

    def test_revoke_denies_immediately(self):
        r = self.c.add_user("ploy", "operator")
        tok = r["token"]
        self.assertEqual(self.c.resolve_role(tok), "operator")
        rv = self.c.revoke_user("ploy", "ลาออก")
        self.assertTrue(rv["ok"])
        self.assertEqual(self.c.resolve_role(tok), "")  # ตายทันที

    def test_revoke_unknown_and_reissue(self):
        self.assertIn("error", self.c.revoke_user("nobody-here"))
        r = self.c.add_user("ploy", "operator")
        self.c.revoke_user("ploy")
        r2 = self.c.add_user("ploy", "viewer")  # re-issue หลัง revoke
        self.assertEqual(self.c.resolve_role(r2["token"]), "viewer")

    def test_list_states(self):
        a = self.c.add_user("aa", "operator", "2020-01-01")
        b = self.c.add_user("bb", "viewer")
        self.c.revoke_user("bb")
        users = {u["name"]: u["state"] for u in self.c.list_users()}
        self.assertEqual(users["aa"], "expired")
        self.assertEqual(users["bb"], "revoked")

    def test_min_role_covers_revoke_path(self):
        from service import roles
        self.assertEqual(roles.min_role_for("/api/roles/revoke"), "admin")


if __name__ == "__main__":
    unittest.main()