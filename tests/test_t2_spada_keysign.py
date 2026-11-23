"""Tests: T2 (2026-11-23) — SPADA KeySign ฮาร์ดแวร์เซ็นการอนุมัติ Risk >= 2 และคำสั่ง shell (ตัวจำลองอุปกรณ์).

อุปกรณ์จริงทดสอบด้วยมือ (ต้องมีคนกดปุ่ม) — ดู service/spada_keysign.py
"""
import importlib
import pathlib
import sys
import tempfile
import threading
import time
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "execution"))

from service import spada_keysign as sk  # noqa: E402


class _Env:
    """ทะเบียนชั่วคราว + อุปกรณ์จำลอง + factory ที่ชี้ไปหาอุปกรณ์จำลอง."""

    def __init__(self, presence=lambda: True):
        self.tmp = tempfile.TemporaryDirectory()
        self.token = pathlib.Path(self.tmp.name) / "token.json"
        self.dev = sk.SimDevice(presence=presence)
        sk.save_token({"tokenId": sk.token_id_of(self.dev.pubkey), "pubkey": self.dev.pubkey.hex(),
                       "owner": "test", "lastCounter": 0, "revoked": False}, self.token)
        sk.set_signer_factory(lambda cfg: sk.KeySignSigner(self.dev, token_path=self.token))

    def close(self):
        sk.set_signer_factory(None)
        self.tmp.cleanup()


ENFORCE = {"spada_keysign_policy": "enforce"}


class ProtocolT(unittest.TestCase):
    def test_signed_message_layout_matches_pkt_proto(self):
        m = sk.build_signed_message(7, b"\x01" * 32, b"\x02" * 32)
        self.assertEqual(len(m), 81)
        self.assertEqual(m[:13], b"SPADA-PKT-v1\x00")
        self.assertEqual(m[13:17], (7).to_bytes(4, "big"))

    def test_token_id_is_sha256_prefix(self):
        self.assertEqual(sk.token_id_of(b"\x00" * 32), __import__("hashlib").sha256(b"\x00" * 32).hexdigest()[:16])

    def test_port_name_is_validated(self):
        with self.assertRaises(sk.KeySignError):
            sk._ps_script("COM7; Remove-Item x", "00", 30000)


class SignerT(unittest.TestCase):
    def setUp(self):
        self.env = _Env()

    def tearDown(self):
        self.env.close()

    def test_sign_verifies_and_advances_counter(self):
        r1 = sk.sign_if_required(ENFORCE, "shell", {"purpose": "t", "x": 1})
        r2 = sk.sign_if_required(ENFORCE, "shell", {"purpose": "t", "x": 2})
        self.assertEqual((r1["counter"], r2["counter"]), (1, 2))
        self.assertEqual(sk.load_token(self.env.token)["lastCounter"], 2)
        self.assertNotEqual(r1["contextHash"], r2["contextHash"])

    def test_off_policy_never_touches_device(self):
        self.assertIsNone(sk.sign_if_required({"spada_keysign_policy": "off"}, "shell", {"purpose": "t"}))
        self.assertIsNone(sk.sign_if_required({}, "approve", {"purpose": "t"}))
        self.assertEqual(self.env.dev.counter, 0)

    def test_scope_limits_where_it_is_required(self):
        cfg = {"spada_keysign_policy": "enforce", "spada_keysign_scope": ["approve"]}
        self.assertIsNone(sk.sign_if_required(cfg, "shell", {"purpose": "t"}))
        self.assertIsNotNone(sk.sign_if_required(cfg, "approve", {"purpose": "t"}))

    def test_no_button_press_is_denied(self):
        self.env.dev.presence = lambda: False
        with self.assertRaises(sk.KeySignError) as c:
            sk.sign_if_required(ENFORCE, "shell", {"purpose": "t"})
        self.assertEqual(c.exception.code, "device_timeout")

    def test_revoked_token_is_denied(self):
        t = sk.load_token(self.env.token); t["revoked"] = True; sk.save_token(t, self.env.token)
        with self.assertRaises(sk.KeySignError) as c:
            sk.sign_if_required(ENFORCE, "shell", {"purpose": "t"})
        self.assertEqual(c.exception.code, "revoked")

    def test_not_registered_is_denied(self):
        self.env.token.unlink()
        with self.assertRaises(sk.KeySignError) as c:
            sk.sign_if_required(ENFORCE, "shell", {"purpose": "t"})
        self.assertEqual(c.exception.code, "not_registered")

    def test_other_device_is_denied(self):
        self.env.dev = sk.SimDevice()  # อุปกรณ์อื่นที่ไม่ได้ลงทะเบียน
        with self.assertRaises(sk.KeySignError) as c:
            sk.sign_if_required(ENFORCE, "shell", {"purpose": "t"})
        self.assertEqual(c.exception.code, "verify_failed")

    def test_replayed_counter_is_denied(self):
        sk.sign_if_required(ENFORCE, "shell", {"purpose": "t"})
        self.env.dev.counter = 0  # อุปกรณ์ถูกย้อน counter / ตอบลายเซ็นเก่า
        with self.assertRaises(sk.KeySignError) as c:
            sk.sign_if_required(ENFORCE, "shell", {"purpose": "t"})
        self.assertEqual(c.exception.code, "verify_failed")

    def test_signature_bound_to_context(self):
        r = sk.sign_if_required(ENFORCE, "shell", {"purpose": "t", "command": "dir"})
        v = sk.verify_assertion(pubkey=self.env.dev.pubkey, counter=r["counter"], challenge=bytes.fromhex(r["challenge"]),
                                context_hash=sk.context_hash_of({"purpose": "t", "command": "del *"}),
                                sig=bytes.fromhex(r["sig"]), expected_challenge=bytes.fromhex(r["challenge"]),
                                challenge_issued_at=time.time(), last_counter=0,
                                expected_context_hash=sk.context_hash_of({"purpose": "t", "command": "del *"}))
        self.assertFalse(v["ok"])
        self.assertFalse(v["checks"]["signature"])


class ShellT(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.SR = importlib.import_module("execution.shell_runner")
        self._orig = self.SR._cfg
        self.SR._cfg = lambda: {"shell_execution_enabled": True, "spada_keysign_policy": "enforce"}
        self.tmp = tempfile.TemporaryDirectory()
        self.man = self.SR.Manifest()
        self.man.path = pathlib.Path(self.tmp.name) / "m.jsonl"

    def tearDown(self):
        self.SR._cfg = self._orig
        self.env.close()
        self.tmp.cleanup()

    def test_shell_runs_after_press_and_records_assertion(self):
        r = self.SR.ShellRunner(self.man).run(sys.executable + " -c \"print('t2-ok')\"")
        self.assertTrue(r.ok, r.denided_reason)
        self.assertIn("t2-ok", r.stdout)
        import json
        last = json.loads(self.man.path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(last["detail"]["spada_keysign"]["counter"], 1)
        self.assertEqual(last["detail"]["spada_keysign"]["scheme"], "spada-pkt-v1")

    def test_shell_refused_without_press(self):
        self.env.dev.presence = lambda: False
        r = self.SR.ShellRunner(self.man).run(sys.executable + " -c \"print('never')\"")
        self.assertFalse(r.ok)
        self.assertEqual(r.denided_reason, "spada_keysign_denied:device_timeout")
        self.assertFalse(self.man.path.exists() and "never" in self.man.path.read_text(encoding="utf-8"))


class ApprovalT(unittest.TestCase):
    def setUp(self):
        self.env = _Env()
        self.ap = importlib.import_module("policy.approval")
        self._orig = self.ap._cfg
        self.ap._cfg = lambda: dict(ENFORCE)
        self.mgr = self.ap.ApprovalManager()
        self.result = {}

    def tearDown(self):
        self.ap._cfg = self._orig
        self.env.close()

    def _start(self, step=3):
        t = threading.Thread(target=lambda: self.result.setdefault(
            "ok", self.mgr.request_approval("task_t2", step, "click", "#buy", "Risk 3", "สั่งซื้อ", timeout=5)))
        t.start()
        for _ in range(100):
            if self.mgr.get_pending("task_t2"):
                break
            time.sleep(0.01)
        return t

    def test_approval_needs_press_and_keeps_waiting_on_failure(self):
        t = self._start()
        self.env.dev.presence = lambda: False
        with self.assertRaises(sk.KeySignError):
            self.mgr.approve("task_t2", 3)
        self.assertEqual(len(self.mgr.get_pending("task_t2")), 1)  # ยังรออยู่ ไม่ถูกอนุมัติ
        self.env.dev.presence = lambda: True
        self.assertTrue(self.mgr.approve("task_t2", 3))
        t.join(2)
        self.assertTrue(self.result.get("ok"))

    def test_signature_is_bound_to_this_step(self):
        req = self.ap.ApprovalRequest("task_t2", 3, "click", "#buy", "Risk 3", "สั่งซื้อ")
        other = self.ap.ApprovalRequest("task_t2", 4, "click", "#buy", "Risk 3", "สั่งซื้อ")
        self.assertNotEqual(sk.context_hash_of(self.ap.approval_context(req)),
                            sk.context_hash_of(self.ap.approval_context(other)))


if __name__ == "__main__":
    unittest.main()
