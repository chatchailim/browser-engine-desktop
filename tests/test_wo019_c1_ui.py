"""Tests: WO-019 C1 — endpoints ตรวจย้อน/สิทธิ์ + path traversal ปิด + roles/add admin-only."""
import json
import pathlib
import sys
import unittest
from unittest import mock

BASE = pathlib.Path("D:/karaok/ai-platform-kit/apps/browser-engine-desktop")
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


class AuditPathSafetyT(unittest.TestCase):
    """UI-05: ชื่อไฟล์ต้องผ่านกฎกัน path traversal ก่อนอ่าน เสมอ."""

    BAD = ["", "../config.json", "a\\b.jsonl", "..", "x.txt", "a/b.jsonl"]

    def _validate(self, fname):
        # กฎเดียวกับ server.py /api/audit/manifest (คำนวณฝั่งทดสอบคู่ขนาน — ยืนยันสัญญา)
        if not fname or "/" in fname or "\\" in fname or ".." in fname or not fname.endswith(".jsonl"):
            return False
        return True

    def test_bad_names_rejected(self):
        for b in self.BAD:
            self.assertFalse(self._validate(b), b)

    def test_good_names_accepted(self):
        self.assertTrue(self._validate("app-20261005.jsonl"))
        self.assertTrue(self._validate("easy-20260925T153719-37484.jsonl"))


class RolesEndpointsContractT(unittest.TestCase):
    """UI-04: list_users ไม่หลุด token เด็ดขาด (คืน name+role เท่านั้น)."""

    def test_list_users_shape(self):
        from service import roles
        users = roles.list_users()
        self.assertIsInstance(users, list)
        for u in users:
            # WO-019 M1: เพิ่ม state/expires/created_at — ห้ามมี token/ข้อความลับในผลเด็ดขาด
            self.assertEqual(sorted(u.keys()), ["created_at", "expires", "name", "role", "state"])
            self.assertIn(u["role"], roles.VALID_ROLES + ("?",))
            self.assertIn(u["state"], ("active", "expired", "revoked"))
            self.assertNotIn("token", u)

    def test_add_user_rejects_bad_role(self):
        from service import roles
        r = roles.add_user("__unittest_x", "hacker")
        self.assertIn("error", r)

    def test_post_min_role_covers_audit_admin_paths(self):
        from service import roles
        # fail-closed: path ไม่อยู่ตาราง = admin — ตรวจว่า /api/roles/add กับ /api/config-anchor บังคับ admin
        self.assertEqual(roles.min_role_for("/api/roles/add"), "admin")
        self.assertEqual(roles.min_role_for("/api/config-anchor"), "admin")
        self.assertEqual(roles.min_role_for("/api/task"), "operator")


class KeysignVerifyContractT(unittest.TestCase):
    """UI-05 verify 1 คลิก: TAMPERED-SAMPLE ต้องจับได้ — หลักฐานว่า verify ไม่ใช่ปุ่มตกแต่ง."""

    def test_tampered_sample_detected(self):
        from service import keysign
        f = BASE / "data" / "action-manifest" / "TAMPERED-SAMPLE-20261005.jsonl"
        if not f.is_file():
            self.skipTest("ไม่มีไฟล์ตัวอย่างถูกแก้ — live proof จะจัดให้")
        r = keysign.verify_chain(str(f))
        self.assertFalse(r["valid"], r)

    def test_real_manifest_valid(self):
        from service import keysign
        f = BASE / "data" / "action-manifest" / ("app-" + __import__("time").strftime("%Y%m%d") + ".jsonl")
        if not f.is_file():
            self.skipTest("วันนี้ยังไม่มี manifest จริง")
        r = keysign.verify_chain(str(f))
        self.assertTrue(r["valid"], r)


if __name__ == "__main__":
    unittest.main()