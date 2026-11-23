"""Reliability layer (WO-018 ต่อยอด + ปิดจุดอ่อน ASSESSMENT ข้อ 4) — ธุรกิจจริง.

ปิดสามจุดอ่อนที่ผู้ตรวจเดิมระบุ:
1. Atomic task reservation — ACTIVE_TASK_ID ตรวจแล้วตั้งทีหลัง = race (สองงานซ้อนได้)
   → ใช้ไฟล์ จองสิทธิ์แบบ O_EXCL (exclusive create) — อะตอมมิกบน Windows (os.O_CREAT|O_EXCL)
2. TASKS in-memory = งานจบหายทั้งหมดเมื่อ restart → เขียนลง data/task-log/app-YYYYMMDD.jsonl
   + load กลับเมื่อ server ขึ้น (งานเก่าจบแล้ว status=ended_at_restart)
3. Malformed body/ชนิดผิด → ตอบ HTTP 400 แน่นอน (ไม่โยน 500) — โลกธุรกิจต้องตำหนิต่อได้
"""
import json
import os
import pathlib
import threading
import time
import uuid

APP = pathlib.Path(__file__).resolve().parent.parent
TASKLOG_DIR = APP / "data" / "task-log"
RESERVE_FILE = APP / "data" / "task-reservation.lock"


def strict_body(raw_bytes, *, required=(), ints=(), max_len=200000, allow_empty=False):
    """อ่าน+ตรวจ body: JSON จริงเท่านั้น + type/ระยะ — ผิดทางใดคืน (None, "<เหตุผลไทย>")."""
    if not raw_bytes or raw_bytes == b"{}":
        if allow_empty:
            return ({}, None) if not raw_bytes else ({}, None)
        if not raw_bytes:
            return {}, None  # body ว่าง = {} (endpoints ที่ไม่ต้องการฟิลด์ก็ใช้ได้)
    if len(raw_bytes) > max_len:
        return None, "payload ใหญ่เกิน " + str(max_len) + " bytes"
    try:
        body = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        return None, "payload ไม่ใช่ JSON ถูกต้อง"
    if not isinstance(body, dict):
        return None, "payload ต้องเป็น JSON object"
    for k in required:
        v = body.get(k)
        if v is None or (isinstance(v, str) and not v.strip()):
            return None, f"ขาดฟิลด์ {k}"
    for k, hi in ints:
        v = body.get(k)
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int):
            try:
                v = int(v)
                body[k] = v
            except Exception:
                return None, f"ฟิลด์ {k} ต้องเป็นตัวเลข"
        if v < 1 or v > hi:
            return None, f"ฟิลด์ {k} ต้องเป็น 1..{hi}"
    return body, None


class TaskReservation:
    """จองสิทธิ์ทำงานเดียวต่อเครื่อง (อะตอมมิกด้วย O_CREAT|O_EXCL) — ปิดช่อง race."""

    def __init__(self, path=RESERVE_FILE):
        self.path = pathlib.Path(path)

    def acquire(self, task_id: str) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"task_id": task_id, "at": time.time()}, f)
            return True
        except FileExistsError:
            try:
                stale = json.loads(self.path.read_text(encoding="utf-8"))
                # กันค้าง: ถ้าจองเกิน 30 นาทีและโปรเซสยังใช้งานจริงไม่ชัด → ปล่อยให้ override ด้วยการเขียนทับได้
                if time.time() - float(stale.get("at", 0)) > 1800:
                    return self._force(task_id)
            except Exception:
                pass
            return False

    def _force(self, task_id: str) -> bool:
        try:
            self.path.unlink()
        except OSError:
            pass
        return self.acquire(task_id)

    def release(self):
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass
        RETURNED = True


class TaskSlotPool:
    """หลายสล็อต + งานเดสก์ท็อปเกรงกันเครื่อง (WO-019 P1 คู่ขนานขั้น 1) — ใช้เมื่อธง task_slots > 1 เท่านั้น.

    - ล็อกรายสล็อตเป็นไฟล์ O_CREAT|O_EXCL (อะตอมมิกเหมือนเดิม) — slot-N.lock
    - kind="desktop" = งานกรรมสิทธิ์เครื่อง (จอ/เมาส์แด่วเดียว): จองได้**เมื่อทุกสล็อตว่าง** และขณะถือ
      ปฏิเสธการจองใหม่ทุกชนิด (กติกากายภาพ — desktop task คู่ขนานกับอะไรไม่ได้)
    - stale เกิน 30 นาที = ปล่อยอัตโนมัติ (กติกากันล็อกหน้าตายเดิม)
    - release อ่านไฟล์จริงเสมอ (คืนได้จากทุก worker — สถานะไม่พึ่งหน่วยความจำ)
    """

    STALE_AFTER = 1800  # 30 นาที — ต้องกับกติกา TaskReservation เดิม

    def __init__(self, slots=1, base=None):
        self.slots = max(1, int(slots))
        self.base = pathlib.Path(base) if base else (APP / "data" / "task-slots")
        try:
            self.base.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    def _slot_path(self, n: int) -> pathlib.Path:
        return self.base / f"slot-{n}.lock"

    def _read(self, p: pathlib.Path) -> dict:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _stale(self, info: dict) -> bool:
        try:
            return time.time() - float(info.get("at", 0)) > self.STALE_AFTER
        except Exception:
            return False

    def _claim(self, n: int, task_id: str, kind: str):
        try:
            fd = os.open(str(self._slot_path(n)), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        except OSError:
            return None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"task_id": task_id, "kind": kind, "at": time.time()}, f)
        return n

    def acquire(self, task_id: str, kind: str = "browser"):
        """คืนเลขสล็อต (1..slots) เมื่อได้ · None เมื่อเต็ม/เครื่องถูกงาน desktop ยึด."""
        # งาน desktop = หลังบ้านทั้งเครื่อง: ต้องได้ทั้งชุดสล็อตว่างจริง
        if kind == "desktop":
            return self._acquire_desktop(task_id)
        for _pass in range(3):  # แข่งขันระหว่าง thread — ลองซ้ำสั้น ๆ เมื่อโดนชน
            for n in range(1, self.slots + 1):
                p = self._slot_path(n)
                info = self._read(p)
                if info and not self._stale(info):
                    if info.get("kind") == "desktop":
                        return None  # เครื่องยึดโดยงานเดสก์ท็อป — ปฏิเสธทุกจองใหม่
                    continue  # สล็อตนี้มีคนถือ
                if info and self._stale(info):
                    self._clear_stale(p)  # ยึดคืน: ลบไฟล์ stale ก่อน claim มิฉะนั้น O_EXCL ชนตลอด
                got = self._claim(n, task_id, kind)
                if got:
                    return got
        return None

    def _clear_stale(self, p: pathlib.Path):
        try:
            info = self._read(p)
            if info and self._stale(info):
                p.unlink()
        except OSError:
            pass

    def _acquire_desktop(self, task_id: str):
        # เช็คว่าว่างจริงทั้งเครื่อง (ล็อก stale ไม่นับเป็นคนถือ) แล้วล้างซากก่อน claim
        for n in range(1, self.slots + 1):
            info = self._read(self._slot_path(n))
            if info and not self._stale(info):
                return None
        for n in range(1, self.slots + 1):
            self._clear_stale(self._slot_path(n))
        got = self._claim(1, task_id, "desktop")
        if got is None:
            return None  # โดนแย่งระหว่างเช็คกับจอง — รอบคิวถัดไปจะได้
        # ยืนยันยังว่างชุดเดิมหลังจอง (ปิดหน้าต่างแข่ง) — ถ้ามีคนแทรก = คืนสล็อตตัวเองทันที
        for n in range(2, self.slots + 1):
            info = self._read(self._slot_path(n))
            if info and not self._stale(info):
                self.release(task_id)
                return None
        return got

    def release(self, task_id: str) -> bool:
        for n in range(1, self.slots + 1):
            p = self._slot_path(n)
            info = self._read(p)
            if info and info.get("task_id") == task_id:
                # Windows: unlink ชน handle อ่านของ thread อื่นได้ (PermissionError) — ลองซ้ำสั้น ๆ กันสล็อตค้างเงียบ
                for _ in range(5):
                    try:
                        p.unlink()
                        return True
                    except OSError:
                        time.sleep(0.02)
                return False
        return False

    def release_all(self):
        """ที่ boot เท่านั้น — server เจ้าของเครื่อง: ล็อกซากจากรอบตายไม่มีสิทธิ์ถือต่อ."""
        for n in range(1, self.slots + 1):
            try:
                self._slot_path(n).unlink()
            except OSError:
                pass

    def snapshot(self) -> list:
        out = []
        for n in range(1, self.slots + 1):
            info = self._read(self._slot_path(n))
            if info and self._stale(info):
                info = None
            out.append({"slot": n, "task_id": (info or {}).get("task_id"),
                        "kind": (info or {}).get("kind") or ""})
        return out


class TaskQueue:
    """คิวงานรอสล็อต (in-process + cap) — ธง task_slots>1 เท่านั้น · งานอยู่ครบใน TASKS/STORE แยกต่างหาก."""

    def __init__(self, cap=20):
        self.items = []
        self.cap = int(cap)
        self._lock = threading.Lock()

    def enqueue(self, item: dict):
        """คืนตำแหน่งคิว (1-based) · None เมื่อคิวเต็ม (caller ตอบ 409 queue_full)."""
        with self._lock:
            if len(self.items) >= self.cap:
                return None
            self.items.append(item)
            return len(self.items)

    def remove(self, task_id: str) -> bool:
        with self._lock:
            before = len(self.items)
            self.items = [q for q in self.items if q.get("task_id") != task_id]
            return len(self.items) < before

    def peek(self):
        with self._lock:
            return self.items[0] if self.items else None

    def pop(self):
        with self._lock:
            return self.items.pop(0) if self.items else None

    def snapshot(self) -> list:
        with self._lock:
            return [dict(q, position=i + 1) for i, q in enumerate(self.items)]


__all__ = ["TaskReservation", "TaskStore", "strict_body", "TaskSlotPool", "TaskQueue"]


class TaskStore:
    """งานไม่หาย — เขียนลงไฟล์ ณ จุดจบ+จุดเปลี่ยนสถานะ, โหลดกลับตอนเริ่ม server."""

    def __init__(self, base_dir=TASKLOG_DIR, tasks=None, memory_limit=200):
        TASKLOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = TASKLOG_DIR / ("app-" + time.strftime("%Y%m%d") + ".jsonl")
        self.memory = tasks if tasks is not None else {}
        self.memory_limit = memory_limit
        self._loaded = False

    def bootstrap(self):
        """คืนค่างานจาก log รอบก่อน (สำหรับโชว์ผลประวัติ) — งานค้าง running = ทำเครื่องหมายจบ."""
        if self._loaded:
            return
        self._loaded = True
        recent = []
        try:
            for p in sorted(self.path.parent.glob("app-*.jsonl"))[-2:]:
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        recent.append(json.loads(line))
                    except Exception:
                        pass
        except OSError:
            return
        for rec in recent[-50:]:
            tid = rec.get("id")
            if not tid or tid in self.memory:
                continue
            if rec.get("status") in ("running", "queued", "waiting_approval"):
                rec["status"] = "ended_at_restart"
                rec["telemetry"] = dict(rec.get("telemetry") or {}, error="server restarted before completion")
            # snapshot เขียน steps เป็นจำนวนนับ (int) — โหลดกลับต้องแปลงเป็น list + เก็บจำนวนแยก (กัน len() พังที่ endpoint)
            steps_field = rec.get("steps")
            if isinstance(steps_field, int) or steps_field is None:
                rec["steps_count"] = steps_field or 0
                rec["steps"] = []
            self.memory[tid] = rec

    def snapshot(self, task: dict):
        """บันทึกสถานะสำคัญลงไฟล์ (append jsonl — ย้อนดูได้ทุกเวลา)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({k: task.get(k) for k in
                                    ("id", "url", "goal", "profile", "status", "kind",
                                     "created_at", "telemetry")} | {"steps": len(task.get("steps", [])),
                                                                    "at": time.time()},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass
        # keep memory bounded
        if len(self.memory) > self.memory_limit:
            for old in sorted(self.memory, key=lambda k: self.memory[k].get("created_at", 0))[: len(self.memory) - self.memory_limit]:
                self.memory.pop(old, None)


__all__ = ["TaskReservation", "TaskStore", "strict_body"]