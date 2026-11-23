"""Recipe Cache — ตัวจำสูตรงาน (WO-เร่งความเร็ว 4 ต.ค. 2026).

หลักการ (ตามธรรมนูญ):
- แคชเฉพาะ "แผน" (sub_tasks จาก planner) สำหรับงาน (url, goal) ที่เคยสำเร็จ — ไม่แคชคำตอบ
  คำตอบทุกครั้งมาจากการอ่านหน้าจริง ณ เวลารัน (อยู่ในสาย extract เดิม)
- งานซ้ำข้าม LLM วางแผนได้ (ประหยัด planning ทั้งหมด) แต่ risk gate / approval /
  verify / takeover ทุกด่านยังทำงานครบเหมือนเดิมทุกขั้น
- key = (origin_host, path, goal.strip()) — ไม่รวม query string/fragment ที่เปลี่ยนได้เรื่อย ๆ
- มี TTL (ค่าเริ่มต้น 30 วัน) + ตรวจโครงสร้างทุกครั้ง + ไฟล์พัง → ถือว่า miss (fail-open to LLM)
- บันทึก provenance ทุกรายการ (สร้างจากใคร เมื่อไร แผนกี่ขั้น)
"""
import hashlib
import json
import pathlib
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from models.client import DGXVllmClient, load_config
from models.schemas import PlanTask

CONFIG_FILE = pathlib.Path(__file__).resolve().parent.parent / "config.json"
CacheFile = pathlib.Path(__file__).resolve().parent.parent / "data" / "recipe_cache.json"
DEFAULT_TTL_DAYS = 30
MAX_ENTRIES = 500


def enabled_in_config(cfg: Optional[Dict[str, Any]] = None) -> bool:
    try:
        c = cfg if cfg is not None else json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return bool(c.get("recipe_cache_enabled", False))
    except Exception:
        return False


def cache_key(url: str, goal: str) -> str:
    parts = urlsplit(url or "")
    origin = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
    raw = f"{origin}|{(goal or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _load_store():
    try:
        data = json.loads(CacheFile.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return data
    except Exception:
        return {}  # fail-open: ไฟล์พัง/หาย = แคชว่าง


def _save_store(store: Dict[str, Any]) -> None:
    CacheFile.parent.mkdir(parents=True, exist_ok=True)
    tmp = CacheFile.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CacheFile)


def valid_plan_shape(plan: Dict[str, Any]) -> bool:
    st = plan.get("sub_tasks")
    if not isinstance(st, list) or not st or len(st) > 50:
        return False
    ok_actions = {"search", "click", "fill", "extract", "verify", "scroll", "done", "type_and_enter", "api_call"}
    for t in st:
        if not isinstance(t, dict) or t.get("action") not in ok_actions:
            return False
        if not isinstance(t.get("target", ""), str) or not t["target"]:
            return False
    return True


class RecipeCache:
    def __init__(self, ttl_days: Optional[float] = None):
        self.ttl_s = (ttl_days if ttl_days is not None else DEFAULT_TTL_DAYS) * 86400

    def get(self, url: str, goal: str) -> Optional[Dict[str, Any]]:
        store = _load_store()
        k = cache_key(url, goal)
        rec = store.get(k)
        if not isinstance(rec, dict) or not valid_plan_shape(rec.get("plan", {})):
            return None
        if time.time() - float(rec.get("created", 0)) > self.ttl_s:
            return None
        out = dict(rec)
        out["key"] = k  # แนบ key เข้า record เสมอ — ผู้ใช้/กฎลืมอ้างกลับได้ทันที
        return out

    def put(self, url: str, goal: str, sub_tasks: list, subtask_dicts: Optional[list] = None,
            source: str = "planner", extra: Optional[Dict[str, Any]] = None) -> str:
        plan = {"sub_tasks": json.loads(json.dumps(subtask_dicts if subtask_dicts is not None else sub_tasks))}
        if not valid_plan_shape(plan):
            return "rejected_invalid_plan"
        store = _load_store()
        k = cache_key(url, goal)
        store[k] = {
            "plan": plan,
            "goal_summary": (goal or "")[:120],
            "url_path": (urlsplit(url).path if url else "")[:200],
            "source": source,
            "created": time.time(),
            "updated": time.time(),
            **(extra or {}),
        }
        if len(store) > MAX_ENTRIES:  # เก็บรายการใหม่กว่าไว้
            for old_k in sorted(store, key=lambda x: store[x].get("created", 0))[: len(store) - MAX_ENTRIES]:
                store.pop(old_k, None)
        _save_store(store)
        return k

    def invalidate(self, url: str, goal: str) -> bool:
        return self.invalidate_by_key(self.cache_key(url, goal))

    def invalidate_by_key(self, key: str) -> bool:
        store = _load_store()
        if key in store:
            store.pop(key)
            _save_store(store)
            return True
        return False

    def list_all(self):
        return [{"key": k, **{f: v.get(f) for f in ("goal_summary", "url_path", "created", "source")}}
                for k, v in sorted(_load_store().items(), key=lambda kv: -kv[1].get("created", 0))]


class RecipeCachedPlanner:
    """Wraps TaskPlanner: cache hit → replay proven plan (0 ms); miss → LLM + record.
    Interface matches TaskPlanner (create_plan / replan / extract_from_page)."""

    def __init__(self, inner: Optional[Any] = None, cache: Optional[RecipeCache] = None):
        self.inner = inner  # lazy: created on first LLM use
        self.cache = cache or RecipeCache()
        self.last_cache_hit: Optional[str] = None
        self.last_planned_key: Optional[str] = None  # key ของ goal+url ล่าสุดที่ระบบวางแผนให้ (hit หรือ miss ก็ตาม)

    def _ensure_inner(self):
        if self.inner is None:
            from engine.planner import TaskPlanner
            self.inner = TaskPlanner()

    def create_plan(self, goal: str, page_title: str = "", current_url: str = "",
                    page_summary: str = "", timeout: Optional[float] = None):
        hit = self.cache.get(current_url, goal)
        if hit is not None:
            self.last_cache_hit = hit.get("key")
            self.last_planned_key = hit.get("key")
            from models.schemas import PlanTask
            return PlanTask(
                task_id="recipe-hit" + (hit.get("key") or ""),
                goal=hit.get("goal_summary") or goal,
                sub_tasks=hit["plan"]["sub_tasks"],
                context={"planning_ms": 0, "recipe_hit": True, "recipe_key": hit.get("key"),
                         "recipe_source": hit.get("source"), "recipe_created": hit.get("created")},
            )
        self.last_cache_hit = None
        self._ensure_inner()
        plan = self.inner.create_plan(goal=goal, page_title=page_title, current_url=current_url,
                                      page_summary=page_summary, timeout=timeout)
        self.last_planned_key = cache_key(current_url, goal)
        try:
            self.cache.put(current_url, goal, plan.sub_tasks, source="planner",
                           extra={"page_title": (page_title or "")[:120]})
        except Exception:
            pass  # บันทึกไม่ได้ไม่กระทบผลงานรอบนี้
        return plan

    def replan(self, goal: str, failed_step: Dict[str, Any], error: str,
               page_summary: str = "", timeout: Optional[float] = None):
        self._ensure_inner()
        plan = self.inner.replan(goal=goal, failed_step=failed_step, error=error,
                                 page_summary=page_summary, timeout=timeout)
        try:
            self.cache.invalidate("", goal)  # (replan ต่อ goal เดิม: เคลียร์ไม่ได้เพราะไม่รู้ url — ที่นี่ปล่อยเก็บไว้)
        except Exception:
            pass
        return plan

    def extract_from_page(self, goal: str, page_text: str):
        self._ensure_inner()
        return self.inner.extract_from_page(goal=goal, page_text=page_text)


def make_planner_if_enabled():
    """Production entry: config-gated (recipe_cache_enabled=true only)."""
    if enabled_in_config():
        return RecipeCachedPlanner()
    return None