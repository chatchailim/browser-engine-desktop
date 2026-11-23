"""Tests: Reliability (WO-018 ต่อยอดปิดจุดอ่อน ASSESSMENT ข้อ 4) — atomic reservation / task store / strict body."""
import importlib
import json
import pathlib
import sys
import tempfile
import threading
import time
import unittest

BASE = pathlib.Path(__file__).resolve().parent.parent  # repo แยกแล้ว — ห้ามชี้ path เดิมใน ai-platform-kit
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

TMP = pathlib.Path(tempfile.mkdtemp(prefix="reliab_"))
R = importlib.import_module("service.reliability")


class _T(unittest.TestCase):
    def setUp(self):
        self.res = R.TaskReservation(TMP / f"lock-{time.time_ns()}.lock")
        self.store = R.TaskStore(base_dir=TMP / "tasklog", tasks={})

    def test_reservation_atomic_exclusive(self):
        self.assertTrue(self.res.acquire("t1"))
        self.assertFalse(self.res.acquire("t2"), "สองงานชน = อันหลังต้องพังก่อนเริ่ม")
        self.res.release()
        self.assertTrue(self.res.acquire("t2"), "ปล่อยแล้วจองใหม่ได้")

    def test_reservation_stale_override(self):
        p = self.res.path
        p.write_text(json.dumps({"task_id": "old", "at": time.time() - 1900}), encoding="utf-8")
        self.assertTrue(self.res.acquire("new"), "ค้าง >30 นาที = ยืดได้ (กันหน้าตายถาวร)")

    def test_strict_body_malformed(self):
        body, err = R.strict_body(b"{broken")
        self.assertIsNone(body)
        self.assertIn("JSON", err)

    def test_strict_body_not_object(self):
        body, err = R.strict_body(b"[1,2]")
        self.assertIsNone(body)
        self.assertIn("object", err)

    def test_strict_body_int_coerce_and_range(self):
        body, err = R.strict_body(json.dumps({"max_steps": "3"}).encode(), ints=(("max_steps", 50),))
        self.assertIsNone(err)
        self.assertEqual(body["max_steps"], 3)
        body, err = R.strict_body(json.dumps({"max_steps": "abc"}).encode(), ints=(("max_steps", 50),))
        self.assertIsNone(body)
        body, err = R.strict_body(json.dumps({"max_steps": 99}).encode(), ints=(("max_steps", 50),))
        self.assertIsNone(body, "เกินเพดาน = 400")

    def test_strict_body_empty_ok(self):
        body, err = R.strict_body(b"")
        self.assertEqual(body, {})
        self.assertIsNone(err)

    def test_store_snapshot_and_bootstrap(self):
        t = {"id": "x1", "url": "u", "goal": "g", "status": "running", "created_at": time.time(), "steps": [1, 2]}
        self.store.memory["x1"] = t
        self.store.snapshot(t)
        self.assertTrue(self.store.path.exists())
        # bootstrap ใน store ใหม่: งาน running จากไฟล์ = ended_at_restart (ไม่หาย)
        s2 = R.TaskStore(base_dir=TMP / "tasklog", tasks={})
        s2.bootstrap()
        self.assertIn("x1", s2.memory)
        self.assertEqual(s2.memory["x1"]["status"], "ended_at_restart")
        self.assertIn("server restarted", (s2.memory["x1"].get("telemetry") or {}).get("error", ""))


if __name__ == "__main__":
    unittest.main()