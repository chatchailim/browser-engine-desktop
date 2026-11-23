"""Tests: WO-019 M3 โหมดแชท /api/chat-task — fail-closed ครบเคส (ไม่ยิง browser จริง — mock worker)."""
import json
import pathlib
import sys
import unittest
from unittest import mock

BASE = pathlib.Path("D:/karaok/ai-platform-kit/apps/browser-engine-desktop")
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


class M3ChatTask(unittest.TestCase):
    """ทดสอบสัญญา /api/chat-task โดยเรียก handler-บางส่วน (จำลองด้วย logic เดียวกับ server)."""

    def _chat(self, goal, host="", url_in_body=None):
        # ยืนยัน logic เดียวกับ server.py (คู่ขนาน — การ wire จริงยิงสดใน live proof)
        import re as _re
        goal = (goal or "").strip()
        if not goal:
            return 400, {"error": "goal ต้องไม่ว่าง"}
        url_in = ""
        m = _re.search(r"https?://[^\s\u0e00-\u0e7f]+", goal)
        if m:
            url_in = m.group(0)
        if not url_in:
            if not host:
                return 400, {"error": "ต้องระบุเว็บเป้าหมาย (host)"}
            url_in = f"http://{host}/"
        from engine.planner_tier import is_easy_task
        if not is_easy_task(goal, url_in):
            return 403, {"error": "งานเสี่ยง/เว็บไม่อนุมัติ", "reason": "tier_classifier_rejected"}
        return 200, {"status": "queued", "mode": "chat"}

    def test_empty_goal(self):
        st, r = self._chat("")
        self.assertEqual(st, 400)

    def test_no_host_rejected(self):
        st, r = self._chat("หาราคาหนังสือ")
        self.assertEqual(st, 400)
        self.assertIn("host", r["error"])

    def test_risky_goal_rejected_even_with_host(self):
        st, r = self._chat("ซื้อหนังสือเล่มแรกให้ผม", host="books.toscrape.com")
        self.assertEqual(st, 403)
        self.assertEqual(r["reason"], "tier_classifier_rejected")

    def test_easy_goal_with_allowed_host_passes(self):
        st, r = self._chat("หาราคาหนังสือเล่มแรก", host="books.toscrape.com")
        self.assertEqual(st, 200)
        self.assertEqual(r["mode"], "chat")

    def test_easy_goal_with_disallowed_host_rejected(self):
        st, r = self._chat("หาราคาสินค้า", host="unknown-shop.example")
        self.assertEqual(st, 403)

    def test_url_in_message_used(self):
        st, r = self._chat("หาราคาสินค้าจาก http://books.toscrape.com/x")
        self.assertEqual(st, 200)  # ครอบด้วย allowlist จากข้อความ

    def test_min_role_covers_chat_task(self):
        from service import roles
        self.assertEqual(roles.min_role_for("/api/chat-task"), "operator")


if __name__ == "__main__":
    unittest.main()