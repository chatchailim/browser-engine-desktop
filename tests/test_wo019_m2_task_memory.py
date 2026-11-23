"""Tests: WO-019 M2 Task Memory — add/update/remove/บล็อกบริบทเพดานอักษร/fail-closed."""
import json
import pathlib
import sys
import tempfile
import unittest

BASE = pathlib.Path("D:/karaok/ai-platform-kit/apps/browser-engine-desktop")
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


class M2Memory(unittest.TestCase):
    def setUp(self):
        import importlib
        from service import task_memory as tm
        self.tm = importlib.reload(tm)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="m2_"))
        self.tm.MEM_FILE = self.tmp / "task-memory.json"
        self.tm._memory_cache = None  # รีเซ็ตแคชต่อเทสต์

    def test_add_and_list(self):
        r = self.tm.add("ซัพพลายเออร์หลัก", "A, B, C จำกัด 3 ราย")
        self.assertTrue(r["ok"])
        self.assertEqual(len(self.tm.list_all()), 1)

    def test_update_same_key_keeps_history(self):
        self.tm.add("ราคากลาง", "100")
        self.tm.add("ราคากลาง", "120")
        entries = self.tm.list_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["value"], "120")

    def test_remove_soft(self):
        self.tm.add("x", "1")
        self.assertTrue(self.tm.remove("x")["ok"])
        self.assertEqual(self.tm.list_all(), [])

    def test_empty_rejected(self):
        self.assertFalse(self.tm.add("", "v")["ok"])
        self.assertFalse(self.tm.add("k", "")["ok"])

    def test_context_block_capped(self):
        for i in range(20):
            self.tm.add(f"k{i}", "v" * 40)
        block = self.tm.build_context_block()
        self.assertTrue(block.startswith("บริบทของธุรกิจนี้"))
        self.assertLessEqual(len(block), self.tm.MAX_CONTEXT_CHARS)

    def test_context_empty_when_no_entries(self):
        self.assertEqual(self.tm.build_context_block(), "")

    def test_corrupt_file_fail_closed(self):
        self.tm.MEM_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.tm.MEM_FILE.write_text("{{{not json", encoding="utf-8")
        self.tm._memory_cache = None
        self.assertEqual(self.tm.build_context_block(), "")  # ระบบทำงานต่อไม่มีบริบท
        self.assertTrue(self.tm.add("new", "v")["ok"])  # เขียนทับไฟล์เสียใหม่ได้


if __name__ == "__main__":
    unittest.main()