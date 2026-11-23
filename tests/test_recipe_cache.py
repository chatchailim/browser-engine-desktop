"""Tests: Recipe Cache (ตัวจำสูตรงาน) — WO-เร่งความเร็ว 4 ต.ค. 2026.

หลักการยืนยัน: cache hit ต้อง replay แผนเดิม (ไม่แคชคำตอบ), ทุกด่านยังทำงาน,
สถานะผิดพลาดทุกกรณีต้อง fail-open กลับไป LLM (fail-closed ต่อความปลอดภัย, fail-open ต่อการทำงาน)
"""
import importlib
import json
import sys
import tempfile
import time
import unittest
import pathlib
from unittest import mock

BASE = pathlib.Path("D:/karaok/ai-platform-kit/apps/browser-engine-desktop")
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

tmp = tempfile.mkdtemp(prefix="recipe_cache_test_")


class _T(unittest.TestCase):
    def setUp(self):
        self.rc = importlib.reload(importlib.import_module("engine.recipe_cache"))
        self.rc.CacheFile = pathlib.Path(tmp) / "recipe_cache.json"
        self.cache = self.rc.RecipeCache(ttl_days=30)

    def test_store_roundtrip_and_schema(self):
        k = self.cache.put("http://x.test/p", "หา A", [{"action": "extract", "target": "[1]", "value": None, "expectation": None}])
        self.assertTrue(len(k) == 24)
        hit = self.cache.get("http://x.test/p", "หา A")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["plan"]["sub_tasks"][0]["action"], "extract")
        self.assertFalse(hit["plan"].get("answer"))  # ห้ามมีคำตอบในแคช

    def test_miss_on_different_goal(self):
        self.cache.put("http://x.test/p", "หา A", [{"action": "extract", "target": "[1]"}])
        self.assertIsNone(self.cache.get("http://x.test/p", "หา B"))

    def test_invalid_plan_rejected(self):
        k = self.cache.put("http://x.test/p", "แผนเสีย", [{"action": "not-allowed", "target": "x"}])
        self.assertEqual(k, "rejected_invalid_plan")
        self.assertIsNone(self.cache.get("http://x.test/p", "แผนเสีย"))

    def test_ttl_expiry(self):
        c1 = self.rc.RecipeCache(ttl_days=0.000001)
        c1.put("http://x.test/p", "หา A", [{"action": "extract", "target": "[1]"}])
        time.sleep(0.1)
        self.assertIsNone(c1.get("http://x.test/p", "หา A"))

    def test_corrupt_file_fail_open(self):
        self.rc.CacheFile.write_text("{broken json", encoding="utf-8")
        self.assertIsNone(self.cache.get("http://x.test/p", "อะไรก็ได้"))

    @mock.patch("engine.planner.TaskPlanner.create_plan")
    def test_planner_wrapper_hit_and_miss(self, mock_create):
        self.cache.put("http://x.test/p", "หา A", [
            {"action": "extract", "target": ".price", "value": None, "expectation": None},
            {"action": "done", "target": "task_completed", "value": None, "expectation": None},
        ])
        w = self.rc.RecipeCachedPlanner(cache=self.cache)
        plan1 = w.create_plan(goal="หา A", current_url="http://x.test/p", page_summary="x")
        self.assertTrue(plan1.context.get("recipe_hit") is True)
        self.assertEqual(plan1.sub_tasks[1]["action"], "done")
        # cache miss → ไปที่ LLM และ "บันทึก" (put) หลังสำเร็จ
        mock_create.side_effect = lambda **kw: type("P", (), {"sub_tasks": [{"action": "click", "target": "[2]"}], "context": {}})()
        plan2 = w.create_plan(goal="งานใหม่", current_url="http://x.test/p")
        self.assertFalse(plan2.context.get("recipe_hit"))
        mock_create.assert_called_once()
        hit2 = self.cache.get("http://x.test/p", "งานใหม่")
        self.assertIsNotNone(hit2, "ต้องบันทึกแผนที่เพิ่งวางไว้ใช้รอบหน้า")

    def test_gate_off_and_on(self):
        with mock.patch.object(self.rc, "enabled_in_config", return_value=False):
            self.assertIsNone(self.rc.make_planner_if_enabled(), "ธงปิด = ต้องได้ planner ปกติ (ไม่มี cache)")
        with mock.patch.object(self.rc, "enabled_in_config", return_value=True):
            w = self.rc.make_planner_if_enabled()
            self.assertIsInstance(w, self.rc.RecipeCachedPlanner)

    def test_key_sensitivity(self):
        k1 = self.rc.cache_key("http://x.test/p?a=1", "g")
        k2 = self.rc.cache_key("http://x.test/p?b=2", "g")
        k3 = self.rc.cache_key("https://x.test/p", "g")
        self.assertEqual(k1, k2)  # query ไม่มีผล
        self.assertNotEqual(k1, k3)  # ข้าม scheme มีผล

    def test_failed_task_forgets_recipe(self):
        """กฎใหม่: งานจบไม่สำเร็จ → แผนที่เพิ่งใช้ต้องถูกลืม (กันแคชแผนพังใช้ซ้ำ)"""
        k = self.cache.put("http://x.test/fail", "งานจะพัง", [{"action": "click", "target": "[9]"}])
        hit = self.cache.get("http://x.test/fail", "งานจะพัง")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["key"], k, "get() ต้องแนบ key กลับมาเสมอ — กฎลืมอ้างทางนี้")
        # เสมือน orchestrator หลังงานล้ม: invalidate_by_key(last_planned_key)
        self.assertTrue(self.cache.invalidate_by_key(hit["key"]))
        self.assertIsNone(self.cache.get("http://x.test/fail", "งานจะพัง"))
        # invalidate เป้าที่ไม่มี = False ไม่ error
        self.assertFalse(self.cache.invalidate_by_key("ไม่มีจริง"))

    def test_evict_old_entries(self):
        for i in range(4):
            self.cache.put(f"http://x.test/p{i}", "g", [{"action": "extract", "target": "[1]"}])
        store = self.rc._load_store()
        self.assertLessEqual(len(store), self.rc.MAX_ENTRIES)


if __name__ == "__main__":
    unittest.main()