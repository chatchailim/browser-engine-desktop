"""Opt-in, local human-supervised desktop input. No native calls on import.

Kept separate from WindowsUIAAdapter's read-only contract and AI/HTTP routing.
The backend is trusted application code; approval is collected only by the console.
"""
import json
import pathlib
from dataclasses import dataclass
import secrets
import threading
import time

APP_DIR = pathlib.Path(__file__).resolve().parent.parent


def _unrestricted():
    """ธง unrestricted_desktop — อ่าน config ตัวเดียวกับ shell_runner/win32_input (WO-018)."""
    from execution.shell_runner import _cfg as _sh_cfg
    return (_sh_cfg().get("unrestricted_desktop", {}) or {})


def _bound_bypassed():
    return _unrestricted().get("bound_bypass") is True


def _extended_bounds(target):
    """ทั้งจอเมื่อ bound_bypass — ใช้ขนาดหลักของ monitor ที่ bind อยู่ (ประมาณจาก rect เดิม ± margin ใหญ่)."""
    # จำกัดให้เป็นกรอบใหญ่รอบ rect เดิม (กันพิกัดลอยจนไปจอคนละเครื่อง/RDP) — ไม่ใช้วงจร EnumDisplay ตอน import
    l, t, r, b = target.rect
    margin = 8000
    return (max(l - margin, -margin), max(t - margin, -margin), r + margin, b + margin)


class InputDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class Target:
    hwnd: int
    pid: int
    created: int
    image: str
    rect: tuple


@dataclass(frozen=True)
class Action:
    kind: str
    args: tuple


KEYS = {"LEFT", "RIGHT", "UP", "DOWN", "HOME", "END", "BACKSPACE",
        "DELETE", "TAB", "ENTER", "ESC"}


def action_from_json(data):
    """Strict schema. No hotkeys, scripts, clipboard, shell, or control characters."""
    if not isinstance(data, dict) or set(data) != {"kind", "args"}:
        raise InputDenied("invalid_action_schema")
    kind, args = data["kind"], data["args"]
    if not isinstance(kind, str) or not isinstance(args, list):
        raise InputDenied("invalid_action_types")
    if kind in {"click", "drag"}:
        count = 2 if kind == "click" else 4
        if len(args) != count or any(type(v) is not int or abs(v) > 100000 for v in args):
            raise InputDenied("invalid_coordinates")
    elif kind == "type":
        if (len(args) != 1 or not isinstance(args[0], str) or not 1 <= len(args[0]) <= 500
                or any(ord(c) < 32 or 0x7f <= ord(c) <= 0x9f or 0xd800 <= ord(c) <= 0xdfff for c in args[0])):
            raise InputDenied("invalid_text_use_key_for_enter_or_tab")
    elif kind == "key":
        if len(args) != 1 or not isinstance(args[0], str) or args[0] not in KEYS:
            raise InputDenied("key_not_allowed")
    else:
        raise InputDenied("action_not_allowed")
    return Action(kind, tuple(args))


class GuardedDesktopSession:
    def __init__(self, backend, *, enabled=False, allowed_images=(), stop_requested=lambda: True,
                 audit, clock=time.monotonic):
        self.backend, self.audit, self.clock = backend, audit, clock
        self.stop_requested = stop_requested
        self.enabled = enabled is True
        self.allowed_images = frozenset(s.casefold() for s in allowed_images if isinstance(s, str) and s)
        self.target = None
        self.pending = None
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.deadline = 0
        self.count = 0

    def bind(self):
        if not self.enabled or not self.allowed_images:
            raise InputDenied("desktop_input_disabled")
        if self.target is not None or self.stopped.is_set():
            raise InputDenied("new_session_required")
        target = self.backend.capture()
        if not isinstance(target, Target) or target.image.casefold() not in self.allowed_images:
            raise InputDenied("executable_not_allowed")
        if not target.hwnd or not target.pid or not target.created:
            raise InputDenied("unknown_target_identity")
        self.target = target
        self.deadline = self.clock() + 300
        self._check_stop()
        self.audit({"event": "bound", "pid": target.pid, "hwnd": target.hwnd})
        return target

    def stop(self):
        # No execution lock: another thread can revoke a running drag/type operation.
        self.stopped.set()

    def _check_stop(self):
        if (self.stopped.is_set() or self.stop_requested() is not False
                or self.clock() >= self.deadline or self.count >= 30):
            self.stop()
            raise InputDenied("session_stopped_expired_or_budget_exhausted")

    def prepare(self, data):
        with self.lock:
            if self.target is None:
                raise InputDenied("not_bound")
            self._check_stop()
            action = action_from_json(data)
            # All points must remain in the original client rectangle.
            # WO-018 R3 (bound_bypass): ธงจาก config ยืดกรอบเป็นทั้งจอเมื่อผู้ใช้เปิด (งานข้ามหน้าต่าง)
            if action.kind in {"click", "drag"}:
                left, top, right, bottom = self.target.rect
                if _bound_bypassed():
                    left, top, right, bottom = _extended_bounds(self.target)
                for x, y in zip(action.args[::2], action.args[1::2]):
                    if not left <= x < right or not top <= y < bottom:
                        raise InputDenied("outside_target_client_area")
            token = secrets.token_hex(12)
            self.pending = (token, action, self.clock() + 30)
            return token, action

    def execute(self, token):
        if not self.lock.acquire(blocking=False):
            raise InputDenied("desktop_busy")
        try:
            pending, self.pending = self.pending, None  # one shot, including failures
            if (pending is None or not isinstance(token, str)
                    or not secrets.compare_digest(token, pending[0]) or self.clock() >= pending[2]):
                raise InputDenied("approval_missing_expired_or_replayed")
            action = pending[1]

            def guard(point=None):
                self._check_stop()
                self.backend.validate(self.target, action, point)

            with self.backend.input_scope():
                guard()
                # Intent must be durable before input; never log text, coordinates, or hashes.
                self.audit({"event": "input_intent", "kind": action.kind, "sequence": self.count + 1})
                self.backend.perform(action, guard)
                self.count += 1
                self.audit({"event": "input_submitted", "kind": action.kind, "sequence": self.count})
            return {"status": "input_submitted", "verified": False, "sequence": self.count}
        except BaseException as exc:
            self.stop()  # partial input must never be automatically retried
            try:
                self.audit({"event": "input_aborted", "error_type": type(exc).__name__})
            except Exception:
                pass
            raise
        finally:
            self.lock.release()

    def close(self):
        self.stop()
        with self.lock:
            self.pending = None
            self.backend.close()
