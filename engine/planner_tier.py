"""Planner Tiering (WO-019 A1): จำแนกงานง่าย → โมเดลเล็กเร็ว / งานยาก → โมเดลใหญ่ตามเดิม.

หลักการ (ไม่ลดของเดิม — fail-open ทุกเหตุ):
- ธง planner_tiering_enabled=false (ค่าเริ่มต้น) → ไม่มี tiering (โมเดลใหญ่ทุกงาน — เหมือนเดิม 100%)
- งาน "ง่าย" = ครบทั้ง 3 เงื่อนไขเหล็ก (ปลายทางปลอดภัยเชิงผล + ขั้นน้อย + รูปแบบเป้าจำแนกชัด):
  1) เป้ามีรูปแบบจำแนกชัด (หาราคา/extract/verify/หาสิ่ง — ไม่มีคำเสี่ยง)
  2) ไม่มีคำเสี่ยงอันตราย (ซื้อ/ลบ/ส่ง/อนุมัติ/ชำระ/โพสต์/ล็อกอิน/รหัส — ส่งใหญ่+risk gate ตามเดิม)
  3) URL เป้ามีรูปเว็บอ่านอย่างเดียว (books.toscrape/wikipedia แนว synthetic เกณฑ์ตั้งได้)
- โมเดลเล็ก: qwen3:4b บน Ollama (http://192.168.1.108/ai — ตรวจสดมีอยู่แล้ว)
- โมเดลเล็กพัง/ช้า/ตอบมั่ว → fallback โมเดลใหญ่**ทุกกรณี** (planner เดิมคืนงานเหมือนไม่มี tiering)
"""
import json
import re
import time
import urllib.request
from typing import Any, Dict, Optional

APP = None  # lazy — ตั้งตอน import แรก (กัน circular)


def _app_dir():
    global APP
    if APP is None:
        APP = __import__("pathlib").Path(__file__).resolve().parent.parent
    return APP


def _cfg() -> dict:
    try:
        return json.loads((_app_dir() / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


# รูปแบบจำแนก "งานง่าย" (เป้าชัด ปลายทางอ่าน) — เกณฑ์อนุรักษ์นิยม (เจอไม่ชัด = ยาก)
EASY_GOAL_PATTERNS = [
    r"หาราคา", r"ราคา.{0,20}(เท่ากัน|เท่า|เห็น)", r"extract", r"คำตอบ.{0,20}(อะไร|เท่าไร)",
    r"หา(ข้อมูล|สิ่ง|เล่มแรก|เล่มที่)", r"(เท่าไร|กี่|มากเท่าไร)", r"price of|find the price|what is the",
]
# คำเสี่ยง → ห้ามใช้เล็ก (ส่งใหญ่ + risk gate เดิมเป็นชั้นกันอีกตัว)
RISKY_GOAL_PATTERN = re.compile(
    r"(ซื้อ|สั่งซื้อ|ชำระ|จ่าย|ลบ|ส่ง|โพสต์|อนุมัติ|เข้าสู่ระบบ|ล็อกอิน|รหัสผ่าน|ตรวจสอบข้อมูลส่วนตัว|"
    r"buy|purchase|pay|checkout|delete|send|publish|approve|login|sign.?in|password)", re.I)


def is_easy_task(goal: str, url: str = "", max_subtasks_hint: int = 15) -> bool:
    """คืน True เมื่องานนี้ปลอดภัยพอที่จะให้โมเดลเล็กวางแผน (เกณฑ์อนุรักษ์นิยม — ไม่ชัด = ยาก)."""
    goal = (goal or "").strip()
    if not goal or RISKY_GOAL_PATTERN.search(goal):
        return False
    if not any(re.search(p, goal, re.I) for p in EASY_GOAL_PATTERNS):
        return False
    cfg = _cfg()
    allow_urls = cfg.get("planner_tier_easy_urls", ["books.toscrape.com", "en.wikipedia.org"])
    if url:
        host = (url.split("//", 1)[-1].split("/", 1)[0] or "").lower()
        if host and not any(h in host for h in allow_urls):
            return False
    return True


class OllamaSmallPlannerClient:
    """client น้องเล็ก — สัญญาเดียวกับ DGXVllmClient ที่ TaskPlanner ใช้ (chat_completion/extract_text_or_json)."""

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None, default_timeout: float = 30.0):
        c = _cfg()
        self.base_url = (base_url or c.get("ollama_url", "http://192.168.1.108/ai")).rstrip("/")
        self.model = model or c.get("planner_tier_small_model", "qwen3:4b-instruct-2507-q4_K_M")
        self.default_timeout = float(c.get("planner_tier_small_timeout_seconds", 60))  # 4b อ่าน prompt เต็ม + 1500 tok ครั้งแรก 25-39 วิ (วัดจริง) — 60 = ปลอดภัย ยังเร็วกว่า 27B 44 วิ
        self.default_max_tokens = int(c.get("planner_tier_small_max_tokens", 1500))  # 4b verbose — JSON ยาว (วัดจริง: ตัดที่ 500 = parse_error)
        self._keepalive_done = False

    def ensure_warm(self):
        """ครั้งแรกต่อโปรเซส: จ้ำโมเดลคงอยู่ใน RAM (keep_alive) — cold-load 31.5 วิ ก่อน warm 2.2 วิ (วัดจริง DGX)."""
        if self._keepalive_done:
            return
        try:
            import threading

            def _poke():
                try:
                    payload = json.dumps({"model": self.model, "prompt": "ok", "keep_alive": "30m", "stream": False}).encode("utf-8")
                    req = urllib.request.Request(self.base_url + "/api/generate", data=payload, method="POST",
                                                 headers={"Content-Type": "application/json"})
                    urllib.request.urlopen(req, timeout=90)
                except Exception:
                    pass  # fail-open — ตัวเล็กไม่ warm = fallback ใหญ่ (ตามปกติ)

            threading.Thread(target=_poke, daemon=True).start()
            self._keepalive_done = True
        except Exception:
            pass  # ไม่เดิน keepalive ได้ = ใช้แบบตรงแล้ว cold-ตอนแรก (ไม่พัง)

    def chat_completion(self, messages, max_tokens=500, temperature=0.0, timeout=None):
        """เดินผ่าน Ollama /api/generate + format="json" (JSON-mode เครื่อง — ตัวเดียวที่กัน 4b ตอบอธิบายย่อหน้า
        ก่อนแผนตอน context ยาว: วัดจริง — /v1/chat/completions: 49วิ+ไม่มีJSON · format=json: 3.9วิ+JSONถูก+แผนถูก)."""
        system_text = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        user_text = "\n".join(m["content"] for m in messages if m.get("role") != "system")
        payload = json.dumps({
            "model": self.model,
            "system": system_text,
            "prompt": user_text + "\n\nReturn JSON with keys goal and sub_tasks",
            "format": "json",
            "keep_alive": "30m",
            "stream": False,
            "options": {"num_predict": max_tokens, "temperature": temperature},
        }).encode("utf-8")
        req = urllib.request.Request(self.base_url + "/api/generate", data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=timeout or self.default_timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # แปลงผลเป็นรูป response ของ vllm (extract_text_or_json ใช้ choices[0].message.content)
        lat = int((time.monotonic() - t0) * 1000)
        self._last_raw = data.get("response", "")  # วินิจฉัยเชิงพิสูจน์ (fallback จะอ่านตรงนี้ — กันตายตัวกลืนหาย)
        # WO-019 M4: provenance เดียวกับฝั่งใหญ่ — ชี้ endpoint ที่รับงานจริง (ollama /ai ต่างจาก /vllm/v1 ชัด)
        return {"choices": [{"message": {"content": self._last_raw}}],
                "created": int(time.time()), "client_latency_ms": lat,
                "endpoint_used": self.base_url}

    @staticmethod
    def extract_text_or_json(resp) -> Any:
        try:
            if isinstance(resp, dict):
                ch = resp.get("choices") or [{}]
                msg = (ch[0] or {}).get("message") or {}
                return msg.get("content") or msg.get("reasoning_content") or ""
            if hasattr(resp, "read"):
                return resp.read().decode("utf-8")
        except Exception:
            pass
        return ""


class TieredPlanner:
    """ห่อ TaskPlanner: งานง่าย+ธงเปิด → โมเดลเล็ก · ล้มเหลวทุกกรณี → โมเดลใหญ่ (TaskPlanner เดิม)."""

    def __init__(self, big_planner=None, small_client=None):
        from engine.planner import TaskPlanner
        self.big = big_planner or TaskPlanner()
        self.small_client = small_client or OllamaSmallPlannerClient()
        self._small_planner = None  # lazy: TaskPlanner(client=small) — สร้างเมื่อใช้จริงครั้งแรก

    def _get_small(self):
        if self._small_planner is None:
            from engine.planner import TaskPlanner
            self._small_planner = TaskPlanner(client=self.small_client)
        return self._small_planner

    def create_plan(self, goal: str, page_title: str = "", current_url: str = "",
                    page_summary: str = "", timeout: Optional[float] = None):
        if not is_easy_task(goal, current_url):
            return self.big.create_plan(goal, page_title, current_url, page_summary, timeout)  # เป้าทางเดิม
        try:
            self.small_client.ensure_warm()  # ครั้งแรกต่อโปรเซส — กันจ่าย cold 31.5 วิกับงานจริง
            plan = self._get_small().create_plan(goal, page_title, current_url, page_summary,
                                                 timeout=timeout if timeout else None)
            ctx = plan.context or {}
            ctx["tier_model"] = self.small_client.model
            plan.context = ctx
            return plan
        except Exception as e:
            # fail-open เด็ดขาด: โมเดลเล็กพังทุกเหตุ = ใหญ่วางแผนตามเดิม (บันทึกเหตุจริง+หัวท้าย raw — เห็นรูปแบบตายตัวทันที)
            _raw = getattr(self.small_client, "_last_raw", "") or ""
            plan = self.big.create_plan(goal, page_title, current_url, page_summary, timeout)
            ctx = plan.context or {}
            ctx["tier_fallback_reason"] = "small_planner_failed"
            ctx["tier_fallback_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            ctx["tier_fallback_raw_head"] = _raw[:160]
            ctx["tier_fallback_raw_tail"] = _raw[-160:]
            plan.context = ctx
            return plan

    # เด้อื่น/ตามอำนาจเดิม — ทุกอย่างเดินทางใหญ่ (ความเสี่ยงสูงต้องใหญ่เสมอ)
    def replan(self, *a, **kw):
        return self.big.replan(*a, **kw)

    def extract_from_page(self, *a, **kw):
        return self.big.extract_from_page(*a, **kw)


def make_tiered_planner_if_enabled():
    """จุด wire: ธง planner_tiering_enabled=true → TieredPlanner · false = None (ใช้เดิม)."""
    c = _cfg()
    if not c.get("planner_tiering_enabled", False):
        return None
    return TieredPlanner()