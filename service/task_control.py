"""Task control (WO-018 step-2): ปุ่มหยุดงานกลางวิ่ง — shared registry ระหว่าง server/orchestrator."""
import threading

_LOCK = threading.Lock()
_CANCELLED: set = set()


def request_cancel(task_id: str) -> None:
    with _LOCK:
        _CANCELLED.add(str(task_id))


def is_cancelled(task_id: str) -> bool:
    return str(task_id) in _CANCELLED


def clear(task_id: str) -> None:
    with _LOCK:
        _CANCELLED.discard(str(task_id))