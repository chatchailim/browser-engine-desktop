"""Tests: WO-019 A1 Planner Tiering — จำแนกงานง่าย/เสี่ยง + ธงปิดเดิม 100% + fail-open กลับโมเดลใหญ่."""
import json
import pathlib
import sys
import unittest
from unittest import mock

BASE = pathlib.Path("D:/karaok/ai-platform-kit/apps/browser-engine-desktop")
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from engine.planner_tier import is_easy_task, TieredPlanner, OllamaSmallPlannerClient, make_tiered_planner_if_enabled  # noqa: E402


class ClassifierT(unittest.TestCase):
    """R1: ตัวจำแนก — งานง่าย/เสี่ยง/ไม่ชัด (เกณฑ์อนุรักษ์นิยม — ไม่ชัด = ยาก เสมอ)."""

    def test_easy_extract_price(self):
        self.assertTrue(is_easy_task("หาราคาเล่มแรกในหมวด Travel", "https://books.toscrape.com/catalogue/page-1.html"))

    def test_easy_english_price(self):
        self.assertTrue(is_easy_task("what is the price of the first book", "https://books.toscrape.com/"))

    def test_risky_buy_denied(self):
        self.assertFalse(is_easy_task("หาราคาแล้วซื้อเล่มแรก", "https://books.toscrape.com/"))
        self.assertFalse(is_easy_task("extract price then checkout the cart", "https://books.toscrape.com/"))

    def test_risky_login_delete_denied(self):
        for g in ("ล็อกอิน Facebook", "ลบโพสต์เก่า", "เข้าสู่ระบบแล้วดึงข้อมูล", "approv the request", "send an email with price"):
            self.assertFalse(is_easy_task(g, "https://books.toscrape.com/"), g)

    def test_unclear_goal_hard(self):
        # ไม่มีรูปแบบเป้าชัด = ยาก (อนุรักษ์นิยม — ส่งใหญ่)
        self.assertFalse(is_easy_task("ทำอะไรก็ได้ที่ดี", "https://books.toscrape.com/"))

    def test_url_not_in_allowlist_hard(self):
        # เว็บอื่นแม้ goal ชัด = ยาก (เกณฑ์ allowlist ผูก)
        self.assertFalse(is_easy_task("หาราคาเล่มแรก", "https://example-shop-real.co.th/p/1"))

    def test_empty_goal_hard(self):
        self.assertFalse(is_easy_task("", "https://books.toscrape.com/"))


class TieredPlannerT(unittest.TestCase):
    """Routing: งานง่าย→เล็ก · งานยาก→ใหญ่ตรง · เล็กพัง→ใหญ่ (fail-open) · เล็กสำเร็จมี tier_model กำกับ."""

    def _fake_plan(self, model_note):
        from models.schemas import PlanTask
        return PlanTask(task_id="p1", goal="g", sub_tasks=[{"action": "extract", "target": "[1]"}],
                        context={"planning_ms": 12, **model_note})

    def test_easy_routes_to_small(self):
        tp = TieredPlanner.__new__(TieredPlanner)  # ไม่ __init__ — mock เอง
        tp.big = mock.Mock()
        tp.big.create_plan.return_value = self._fake_plan({})
        tp.small_client = mock.Mock(spec=OllamaSmallPlannerClient)
        tp.small_client.model = "qwen3:4b"
        tp._small_planner = mock.Mock()
        tp._small_planner.create_plan.return_value = self._fake_plan({"from_small": True})
        plan = tp.create_plan("หาราคาเล่มแรกในหมวด Travel", "", "https://books.toscrape.com/")
        self.assertTrue(tp._small_planner.create_plan.called)
        self.assertFalse(tp.big.create_plan.called)
        self.assertEqual(plan.context.get("tier_model"), "qwen3:4b")

    def test_hard_routes_to_big_direct(self):
        tp = TieredPlanner.__new__(TieredPlanner)
        tp.big = mock.Mock()
        tp.big.create_plan.return_value = self._fake_plan({})
        tp.small_client = mock.Mock(spec=OllamaSmallPlannerClient)
        tp._small_planner = None
        plan = tp.create_plan("ซื้อเล่มแรก (checkout)", "", "https://books.toscrape.com/")
        self.assertTrue(tp.big.create_plan.called)
        self.assertFalse(plan.context.get("tier_model"))

    def test_small_fail_opens_to_big(self):
        tp = TieredPlanner.__new__(TieredPlanner)
        tp.big = mock.Mock()
        tp.big.create_plan.return_value = self._fake_plan({})
        tp.small_client = mock.Mock(spec=OllamaSmallPlannerClient)
        tp.small_client.model = "qwen3:4b"
        tp._small_planner = mock.Mock()
        tp._small_planner.create_plan.side_effect = RuntimeError("ollama_down")
        plan = tp.create_plan("หาราคาเล่มแรกในหมวด Travel", "", "https://books.toscrape.com/")
        self.assertTrue(tp._small_planner.create_plan.called, "ยิงเล็กก่อนเสมอ")
        self.assertTrue(tp.big.create_plan.called, "เล็กพังต้องใหญ่วางแผนแทนเสมอ")
        self.assertEqual(plan.context.get("tier_fallback_reason"), "small_planner_failed")

    def test_small_warm_called_before_plan(self):
        tp = TieredPlanner.__new__(TieredPlanner)
        tp.big = mock.Mock()
        tp.small_client = mock.Mock(spec=OllamaSmallPlannerClient)
        tp.small_client.model = "qwen3:4b"
        tp._small_planner = mock.Mock()
        tp._small_planner.create_plan.return_value = self._fake_plan({})
        tp.create_plan("หาราคาเล่มแรก", "", "https://books.toscrape.com/")
        self.assertTrue(tp.small_client.ensure_warm.called, "ensure_warm ต้องถูกเรียกก่อนทุก plan ของเล็ก")

    def test_flag_off_returns_none(self, *_):
        with mock.patch("engine.planner_tier._cfg", return_value={"planner_tiering_enabled": False}):
            self.assertIsNone(make_tiered_planner_if_enabled())

    def test_flag_on_returns_planner(self):
        with mock.patch("engine.planner_tier._cfg", return_value={"planner_tiering_enabled": True}):
            p = make_tiered_planner_if_enabled()
            self.assertIsInstance(p, TieredPlanner)


class SmallClientT(unittest.TestCase):
    def test_contract_methods_exist(self):
        c = OllamaSmallPlannerClient(base_url="http://127.0.0.1:1")
        self.assertTrue(hasattr(c, "chat_completion"))
        self.assertTrue(hasattr(c, "extract_text_or_json"))
        self.assertEqual(c.extract_text_or_json({"choices": [{"message": {"content": "hi"}}]}), "hi")


if __name__ == "__main__":
    unittest.main()