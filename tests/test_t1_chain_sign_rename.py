"""Tests: T1 (2026-11-23) — เปลี่ยนชื่อ keysign → chain-sign โดยของเดิมยังใช้ได้."""
import json
import pathlib
import sys
import tempfile
import unittest

APP = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "execution"))


class ChainSignRenameT(unittest.TestCase):
    def test_old_import_is_same_module(self):
        from service import chain_sign
        from service import keysign
        self.assertIs(keysign, chain_sign)

    def test_policy_prefers_new_key(self):
        from service import chain_sign as cs
        self.assertEqual(cs.policy_from_cfg({"chain_sign_policy": "enforce", "keysign_policy": "off"}), "enforce")

    def test_policy_accepts_old_key(self):
        from service import chain_sign as cs
        self.assertEqual(cs.policy_from_cfg({"keysign_policy": "audit"}), "audit")

    def test_policy_unknown_is_off(self):
        from service import chain_sign as cs
        self.assertEqual(cs.policy_from_cfg({"chain_sign_policy": "yes"}), "off")
        self.assertEqual(cs.policy_from_cfg({}), "off")

    def test_old_chain_still_verifies(self):
        from service import chain_sign as cs
        key = cs.ensure_key()
        priv, _pub = cs.ensure_keypair()
        with tempfile.TemporaryDirectory() as d:
            m = pathlib.Path(d) / "m.jsonl"
            b1 = {"seq": 1, "timestamp": "t1", "source": "ai", "kind": "shell_run", "detail": {"command": "a"}}
            s1 = cs.sign_entry(b1, key, cs.GENESIS)
            b2 = {"seq": 2, "timestamp": "t2", "source": "ai", "kind": "shell_run", "detail": {"command": "b"}}
            s2 = cs.sign_entry_asym(b2, priv, s1)
            m.write_text(json.dumps(dict(b1, signature=s1)) + "\n" + json.dumps(dict(b2, signature=s2)) + "\n", encoding="utf-8")
            r = cs.verify_chain(m, key)
        self.assertTrue(r["valid"], r)

    def test_shell_runner_reads_both_keys(self):
        import importlib
        shell_runner = importlib.import_module("execution.shell_runner")
        orig = shell_runner._cfg
        try:
            shell_runner._cfg = lambda: {"keysign_policy": "enforce"}
            r = shell_runner.ShellRunner()
            self.assertEqual(r.chain_sign_policy, "enforce")
            self.assertEqual(r.keysign_policy, "enforce")
            shell_runner._cfg = lambda: {"chain_sign_policy": "audit", "keysign_policy": "enforce"}
            self.assertEqual(shell_runner.ShellRunner().chain_sign_policy, "audit")
        finally:
            shell_runner._cfg = orig


if __name__ == "__main__":
    unittest.main()
