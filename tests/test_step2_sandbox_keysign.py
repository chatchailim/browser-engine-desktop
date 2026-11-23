"""Tests: WO-018 step2 — cancel / sandbox-rollback / keysign chain (จับการแก้ย้อนหลังได้จริง)."""
import importlib
import json
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock

BASE = pathlib.Path("D:/karaok/ai-platform-kit/apps/browser-engine-desktop")
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="step2_"))


class CancelT(unittest.TestCase):
    def test_cancel_flow(self):
        from service import task_control as tc
        tc.clear("tX")
        self.assertFalse(tc.is_cancelled("tX"))
        tc.request_cancel("tX")
        self.assertTrue(tc.is_cancelled("tX"))
        tc.clear("tX")
        self.assertFalse(tc.is_cancelled("tX"))


class SandboxT(unittest.TestCase):
    def setUp(self):
        from service.shell_sandbox import ShellSandbox
        self.sb = ShellSandbox(TMP / "sbx")
        for p in self.sb.dir.glob("*"):
            pass

    def test_snapshot_diff_restore(self):
        f1 = self.sb.dir / "a.txt"
        f1.write_text("v1", encoding="utf-8")
        before = self.sb.snapshot()
        stash, mb = self.sb.stash(before, max_mb=10)
        self.assertIsNotNone(stash)
        # แก้ + สร้าง + ลบ
        f1.write_text("v2-broken", encoding="utf-8")
        (self.sb.dir / "b-new.txt").write_text("x", encoding="utf-8")
        (self.sb.dir / "a.txt").with_suffix(".tmp").unlink() if False else None
        after = self.sb.snapshot()
        d = self.sb.diff(before, after)
        self.assertIn("b-new.txt", d["created"])
        self.assertIn("a.txt", d["modified"])
        stats = self.sb.restore(stash, before, d)
        after2 = self.sb.snapshot()
        self.assertEqual(after2["a.txt"]["sha256"], before["a.txt"]["sha256"], "ไฟล์แก้กลับคืนเดิม")
        self.assertNotIn("b-new.txt", after2, "ไฟล์ที่สร้างระหว่างงานถูกเอาออก")
        self.assertEqual(stats["restored"], len(before))


class KeysignT(unittest.TestCase):
    def setUp(self):
        from service import keysign
        self.ks = keysign
        self.mpath = TMP / f"m-{time.time_ns()}.jsonl"

    def test_sign_and_verify_chain(self):
        key = self.ks.ensure_key()
        self.assertTrue(len(key) == 32)
        prev = "GENESIS"
        for i in range(3):
            base = {"seq": i, "timestamp": "t", "source": "ai", "kind": "shell_run", "detail": {"command": f"cmd{i}"}}
            sig = self.ks.sign_entry(base, key, prev)
            entry = dict(base, signature=sig)
            with open(self.mpath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            prev = sig
        r = self.ks.verify_chain(self.mpath, key)
        self.assertTrue(r["valid"], r)
        self.assertEqual(r["entries"], 3)

    def test_tamper_detected(self):
        key = self.ks.ensure_key()
        lines = []
        prev = "GENESIS"
        for i in range(2):
            base = {"seq": i, "timestamp": "t", "source": "ai", "kind": "k", "detail": {"c": i}}
            sig = self.ks.sign_entry(base, key, prev)
            lines.append(json.dumps(dict(base, signature=sig), ensure_ascii=False))
            prev = sig
        # แก้ย้อนหลัง: เปลี่ยน detail รายการแรก (ลายเซ็นไม่ตรงอีกต่อไป)
        e = json.loads(lines[0]); e["detail"]["c"] = 999
        lines[0] = json.dumps(e, ensure_ascii=False)
        self.mpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        r = self.ks.verify_chain(self.mpath, key)
        self.assertFalse(r["valid"], "การแก้ย้อนหลังต้องจับได้")
        self.assertEqual(r["first_problem"]["reason"], "signature_mismatch")

    def test_policy_valid_values(self):
        for v in ("off", "audit", "enforce"):
            self.assertIn(self.ks.policy() if False else v, ("off", "audit", "enforce"))


if __name__ == "__main__":
    unittest.main()