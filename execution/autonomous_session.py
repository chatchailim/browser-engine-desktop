"""AutonomousFrameSession — โหมดอิสระภายในกรอบ (ระดับ A) ทำงานเหนือ GuardedDesktopSession.

หลักการ (คำสั่งอนุมัติลายลักษณ์อักษรของผู้ใช้ 4 ต.ค. 2026 — ระเบียน WO-017-CLOSEOUT):
- อนุมัติ **รายงาน** (popup one-click ก่อนเริ่มงาน) ไม่ใช่รายคำสั่ง — เพื่อความเร็วระดับแทนคน
- คงด่าน validate ต่อ action ของ GuardedDesktopSession/Win32InputBackend **ทุกชั้นเดิม**:
  identity/creation-time/rect/runtime-id/foreground/occlusion/password/blocked-exe/escape/modifier
- ขอบเขตกรอบ: bind หน้าต่างเดียวที่ allowlist (เริ่มต้น Notepad) — ทุกคำสั่งต้องอยู่ใน client rect เดิม
  (เหมือนเดิม — prepare() ปฏิเสธพิกัดนอกกรอบ / validate ปฏิเสธหน้าต่างย้าย/เปลี่ยน)
- ปุ่มหยุดเดิมทำงานทุกตัว: Escape ค้าง (validate), STOP-DESKTOP.cmd, control.json paused,
  Ctrl+C — พร้อมเพิ่ม: ไฟล์ AUTONOMY-STOP (รายงานละงานทันที)
- Budget ต่องาน: max_actions (ค่าเริ่มต้น 60) — เกิน = หยุด (ตามธรรมนูญ งานสำเร็จแล้วห้ามขยายเอง)
- audit ทุกเหตุการณ์แต่งในไฟล์รายงาน (ไม่บันทึกข้อความพิมพ์/พิกัด)
- งานจบ (สำเร็จ/ล้ม) ปิดเซสชัน ไม่ reuse ข้ามงาน
"""
import json
import pathlib
import time
import threading

from execution.guarded_desktop import GuardedDesktopSession, InputDenied
from execution import approval_ui

APP = pathlib.Path(__file__).resolve().parent.parent
AUTONOMY_STOP = APP / "service" / "AUTONOMY-STOP"

def _control_path() -> pathlib.Path:
    """control.json ของ coordination — env BED_COORDINATION_DIR ก่อน แล้วไล่หาโฟลเดอร์ coordination ขึ้นไป.

    รองรับทั้ง layout เดิม (ai-platform-kit/apps/browser-engine-desktop) และ repo แยก (browser-engine-desktop)
    — ของเดิม APP.parents[2] ใช้ได้เฉพาะ layout เดิม และพังด้วย IndexError เมื่อแยก repo
    """
    import os
    env = os.environ.get("BED_COORDINATION_DIR")
    if env:
        return pathlib.Path(env) / "codex-anti/control.json"
    for parent in APP.parents:
        if (parent / "coordination").is_dir():
            return parent / "coordination/codex-anti/control.json"
    return APP.parent / "coordination/codex-anti/control.json"


CONTROL = _control_path()
DEFAULT_ALLOWED_IMAGES = ["C:\\Windows\\System32\\notepad.exe"]
DEFAULT_MAX_ACTIONS = 60
APPROVAL_TIMEOUT_S = 20.0


def _autonomy_stop_requested():
    try:
        if AUTONOMY_STOP.exists():
            return True
        return json.loads(CONTROL.read_text("utf-8")).get("paused") is not False
    except (OSError, ValueError):
        return True


class AutonomousFrameSession:
    """One approved task window: prepare+execute actions without per-action approval codes."""

    def __init__(self, backend, *, allowed_images=None, max_actions=DEFAULT_MAX_ACTIONS,
                 audit, approval_timeout_s=APPROVAL_TIMEOUT_S, task_details=None,
                 require_task_approval=True):
        self.inner = GuardedDesktopSession(
            backend, enabled=True,
            allowed_images=allowed_images or DEFAULT_ALLOWED_IMAGES,
            stop_requested=lambda: _autonomy_stop_requested() or self._task_stopped.is_set(),
            audit=audit)
        self.max_actions = max(1, int(max_actions))
        self.audit = audit
        self.approval_timeout_s = approval_timeout_s
        self.task_details = dict(task_details or {})
        self.require_task_approval = require_task_approval
        self._task_stopped = threading.Event()
        self.actions_done = 0

    # ---- lifecycle ----
    def start(self):
        """อนุมัติรายงาน (popup) → bind → กลับ Target ที่ผูกแล้ว"""
        self.audit({"event": "autonomy_task_requested",
                    "max_actions": self.max_actions, **{("d_" + k): v for k, v in self.task_details.items()}})
        if self.require_task_approval:
            approved, reason = approval_ui.ask_task_approval(
                "Computer Use — ขออนุมัติทำงาน",
                {**self.task_details, "ขั้นสูงสุด": self.max_actions,
                 "หยุดทันที": "กด Escape ค้าง หรือไฟล์ AUTONOMY-STOP",
                 "ค่าเริ่มต้น": "ไม่ตอบ = ปฏิเสธ"},
                timeout_s=self.approval_timeout_s)
            self.audit({"event": "autonomy_task_approval", "approved": approved, "reason": reason})
            if not approved:
                raise InputDenied("task_approval_" + reason)
        self.inner.bind()
        self.audit({"event": "autonomy_session_started", "hwnd": self.inner.target.hwnd, "pid": self.inner.target.pid})

    def stop(self):
        self._task_stopped.set()
        self.inner.stop()

    def close(self):
        self.inner.close()
        self.audit({"event": "autonomy_session_closed", "actions_done": self.actions_done})

    # ---- actions ----
    def do(self, data):
        """วางแผนแล้วกระทำทันที — ไม่มีรหัสรายคำสั่ง; แต่ด่าน guard ทุกชั้นยังทำงานก่อนส่ง input."""
        if self._task_stopped.is_set():
            raise InputDenied("task_stopped")
        if self.actions_done >= self.max_actions:
            raise InputDenied("task_action_budget_exhausted")
        token, action = self.inner.prepare(data)      # schema+bounds+stop checks (เดิมทุกขั้น)
        self.inner.execute(token)                     # validate+perform (เดิมทุกชั้น)
        self.actions_done += 1
        return {"status": "input_submitted", "sequence": self.actions_done,
                "kind": action.kind, "verified": False}