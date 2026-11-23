"""Tests: WO-018 step3 — roles 3 ระดับ + keysign Ed25519 (และโซ่ผสม HMAC/ed25519)."""
import json
import pathlib
import sys
import tempfile
import unittest

BASE = pathlib.Path("D:/karaok/ai-platform-kit/apps/browser-engine-desktop")
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="step3_"))


class RolesT(unittest.TestCase):
    def test_rank_and_gates(self):
        from service import roles
        self.assertTrue(roles.role_at_least("admin", "operator"))
        self.assertTrue(roles.role_at_least("operator", "operator"))
        self.assertFalse(roles.role_at_least("viewer", "operator"))
        self.assertFalse(roles.role_at_least("", "viewer"))
        # ตารางสิทธิ์: shell-run = admin เท่านั้น, task = operator ขึ้นไป
        self.assertEqual(roles.min_role_for("/api/shell-run"), "admin")
        self.assertEqual(roles.min_role_for("/api/task"), "operator")
        self.assertEqual(roles.min_role_for("/api/unknown-x"), "admin")  # fail-closed

    def test_add_and_resolve(self):
        from service import roles
        r = roles.add_user("tester-op", "operator")
        self.assertIn("token", r, r)
        if "error" in r:
            return
        role = roles.resolve_role(r["token"])
        # resolve_role อาจคืน admin ผ่าน machine token path ถ้า import server ได้ — ตรวจด้วยไฟล์ตรง
        files = {f.stem: f.with_suffix(".json") for f in roles._token_files()}
        self.assertIn("tester-op", files)
        self.assertEqual(json.loads(files["tester-op"].read_text(encoding="utf-8")).get("role"), "operator")
        self.assertEqual(role, "operator")

    def test_unknown_token_no_role(self):
        from service import roles
        # token แปลกที่ไม่ลงทะเบียน: resolve ต้องไม่ให้สิทธิ์ (machine token อ่านจากไฟล์ ไม่ import server)
        self.assertEqual(roles.resolve_role("deadbeef" * 8), "")
        # และถ้าไฟล์ machine token อยู่จริง resolve ต้อง admin ตรงค่า (ความสมบูรณ์ของตรรกะหลัก)
        mt = roles._machine_token()
        if mt:
            self.assertEqual(roles.resolve_role(mt), "admin")


class KeysignEdT(unittest.TestCase):
    def test_ed25519_sign_verify(self):
        from service import keysign
        priv, pub = keysign.ensure_keypair()
        self.assertEqual(len(pub), 64)
        base = {"seq": 1, "timestamp": "t", "source": "ai", "kind": "shell_run", "detail": {"c": 1}}
        sig = keysign.sign_entry_asym(base, priv, keysign.GENESIS)
        self.assertTrue(sig.startswith("ed25519:"))
        # ตรวจผ่าน verify_chain จริง (เขียนไฟล์ 2 บรรทัด: HMAC + ed25519 — โซ่ผสม)
        mpath = TMP / f"mixed-{time.time_ns() if False else 0}.jsonl"
        key = keysign.ensure_key()
        hmac_sig = keysign.sign_entry(base, key, keysign.GENESIS)
        base2 = dict(base, seq=2)
        ed_sig = keysign.sign_entry_asym(base2, priv, hmac_sig)
        mpath.write_text(
            json.dumps(dict(base, signature=hmac_sig), ensure_ascii=False) + "\n" +
            json.dumps(dict(base2, signature=ed_sig), ensure_ascii=False) + "\n", encoding="utf-8")
        r = keysign.verify_chain(mpath, key)
        self.assertTrue(r["valid"], r)
        self.assertEqual(r["schemes"]["hmac"], 1)
        self.assertEqual(r["schemes"]["ed25519"], 1)

    def test_ed25519_tamper_caught(self):
        from service import keysign
        priv, pub = keysign.ensure_keypair()
        base = {"seq": 9, "timestamp": "t", "source": "ai", "kind": "k", "detail": {"x": 1}}
        mpath = TMP / "ed-tamper.jsonl"
        ed_sig = keysign.sign_entry_asym(base, priv, keysign.GENESIS)
        bad = dict(base, detail={"x": 999})
        mpath.write_text(json.dumps(dict(bad, signature=ed_sig), ensure_ascii=False) + "\n", encoding="utf-8")
        r = keysign.verify_chain(mpath)
        self.assertFalse(r["valid"])
        self.assertEqual(r["first_problem"]["reason"], "ed25519_mismatch")


import time  # noqa: E402

if __name__ == "__main__":
    unittest.main()