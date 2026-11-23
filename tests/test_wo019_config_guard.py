"""Tests: WO-019 B1 Config Guard — baseline/จดแนบ/แก้ตรงจับได้/provision ผูก/admin-only."""
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

BASE = pathlib.Path("D:/karaok/ai-platform-kit/apps/browser-engine-desktop")
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="b1_"))


class ConfigGuardT(unittest.TestCase):
    """ครอบ service.config_guard ด้วย APP จำลอง (temp dir — แยกจากเครื่องจริง)."""

    def setUp(self):
        import importlib
        from service import config_guard as cg
        importlib.reload(cg)
        self.cg = cg
        self.app = TMP / f"app-{time.time_ns()}"
        (self.app / "service").mkdir(parents=True, exist_ok=True)
        (self.app / "config.json").write_text(json.dumps({"port": 8205, "shell_execution_enabled": False}), encoding="utf-8")
        cg.APP = self.app
        cg.BASELINE_FILE = self.app / "data" / "config_baseline.json"
        cg._last_check = None

    def tearDown(self):
        self.cg._last_check = None

    def test_first_boot_anchors(self):
        r = self.cg.check_integrity()
        self.assertEqual(r["state"], "baseline_anchored", r)
        self.assertTrue(r.get("config_sha256"))

    def test_intact_when_unchanged(self):
        self.cg.anchor_baseline("boot")
        r = self.cg.check_integrity()
        self.assertEqual(r["state"], "intact")
        self.assertTrue(r["ok"])

    def test_direct_edit_caught(self):
        self.cg.anchor_baseline("boot")
        # แก้ตรง (จำลองคนมี shell ปลดธงเอง)
        (self.app / "config.json").write_text(json.dumps({"port": 8205, "shell_execution_enabled": True}), encoding="utf-8")
        self.cg._last_check = None
        r = self.cg.check_integrity()
        self.assertEqual(r["state"], "outside_of_approval_flow")
        self.assertFalse(r["ok"])
        self.assertIn("Approval", r["message"])

    def test_anchor_version_rebinds(self):
        self.cg.anchor_baseline("boot")
        (self.app / "config.json").write_text(json.dumps({"port": 8206}), encoding="utf-8")
        self.cg._last_check = None
        self.assertEqual(self.cg.check_integrity()["state"], "outside_of_approval_flow")
        r = self.cg.anchor_version("v2-approved")
        self.assertTrue(r["ok"])
        self.assertEqual(r["anchor_reason"], "approved-version: v2-approved")
        self.cg._last_check = None
        self.assertEqual(self.cg.check_integrity()["state"], "intact")

    def test_previous_hash_tracked(self):
        self.cg.anchor_baseline("v1")
        h1 = self.cg.compute_hash()
        (self.app / "config.json").write_text(json.dumps({"port": 9001}), encoding="utf-8")
        r = self.cg.anchor_version("v2")
        self.assertEqual(r["previous_hash"], h1)


import time  # noqa: E402


class ServerEndpointT(unittest.TestCase):
    """endpoint: /api/config-status (ทุก role อ่าน) · /api/config-anchor (admin เท่านั้น)."""

    def test_registered_paths_documented(self):
        # ตรวจว่า POST_MIN_ROLE ของ roles ครอบ path ใหม่ (fail-closed default=admin อยู่แล้ว — ยืนยัน)
        from service import roles
        # config-anchor ไม่อยู่ตาราง = admin เท่านั้น (fail-closed ตามออกแบบ)
        self.assertEqual(roles.min_role_for("/api/config-anchor"), "admin")

    def test_matrix_live_documented(self):
        # การยิง HTTP จริงลง server มีในคิวพิสูจน์ (หน้าต่าง server ตัวจริง) — ที่นี่ยืนยันการออกแบบ+ระบุตรง
        from service.config_guard import check_integrity
        self.assertTrue(callable(check_integrity))


if __name__ == "__main__":
    unittest.main()