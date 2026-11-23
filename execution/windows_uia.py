"""Desktop Adapter interface for Windows UI Automation and Win32 SendInput (Level 5 Strategy).
WO-016 Phase 1: Read-Only Phase — STRICTLY NO CLICKS, NO TYPING, NO INPUT INJECTION (GR1).

All input-writing methods in WindowsUIAAdapter unconditionally return False
and log a policy notice. The adapter provides read-only inspection methods
utilizing perception/desktop_tree.py and policy/uia_policy.py.
"""
import ctypes
from ctypes import wintypes
import logging
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from execution.adapter_base import UIAdapter
from perception.desktop_tree import DesktopTreeParser
from policy.uia_policy import UIAPolicyEngine, get_uia_policy

logger = logging.getLogger("WindowsUIAAdapter")


class WindowsUIAAdapter(UIAdapter):
    """Windows UI Automation Adapter (Level 5 Strategy - Read-Only Phase).
    
    Enforces Ground Rule 1 (GR1):
    Absolutely NO clicks, typing, keyboard events, drag-and-drop, mouse movement,
    or OS input injection. All input-writing methods return False with policy reasons.
    """
    def __init__(
        self,
        policy: Optional[UIAPolicyEngine] = None,
        tree_parser: Optional[DesktopTreeParser] = None
    ):
        self.policy = policy or get_uia_policy()
        self.tree_parser = tree_parser or DesktopTreeParser(policy=self.policy)
        self.user32 = None
        self.is_available = False

        try:
            self.user32 = ctypes.windll.user32
            self.is_available = True
        except Exception:
            self.user32 = None

    # -------------------------------------------------------------------------
    # Read-Only Inspection Methods (Allowed in WO-016 Phase 1)
    # -------------------------------------------------------------------------
    def inspect_window(
        self,
        hwnd_or_selector: Union[int, str],
        task_id: str = "desktop_task"
    ) -> Dict[str, Any]:
        """Inspect and parse an allowed desktop window into a privacy-masked tree (R1, R4).
        
        Enforces policy gates (GR2, GR3, GR4) and logs FactDesktopWindowRead (GR8).
        """
        t_start = time.monotonic()

        # Resolve HWND if string selector or process name provided
        hwnd = None
        if isinstance(hwnd_or_selector, int):
            hwnd = hwnd_or_selector
        elif isinstance(hwnd_or_selector, str):
            hwnd = self.tree_parser.find_window_by_process_or_title(
                process_name=hwnd_or_selector,
                title_keyword=hwnd_or_selector
            )

        if not hwnd:
            return {
                "ok": False,
                "error": "window_not_found",
                "reason": f"Could not find window matching {hwnd_or_selector}",
                "element_count": 0,
                "tree": None,
                "elapsed_ms": 0
            }

        # Build masked tree (enforces policy inside build_masked_tree)
        result = self.tree_parser.build_masked_tree(hwnd)
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        result["elapsed_ms"] = elapsed_ms

        if not result.get("ok"):
            return result

        # Requirement GR8: Spool FactDesktopWindowRead in OneVault
        try:
            from telemetry.vault import get_vault_manager
            vault = get_vault_manager()
            if vault and vault.enabled:
                fact = vault.create_desktop_window_read_fact(
                    task_id=task_id,
                    process_name=result.get("process_name", ""),
                    window_title_hash=result.get("window_title_hash", ""),
                    element_count=result.get("element_count", 0),
                    elapsed_ms=elapsed_ms,
                    structure_hash=result.get("structure_hash", "")
                )
                if fact:
                    vault.spool_fact(fact)
                    vault.flush_spool_async()
                result["fact"] = fact
        except Exception as e:
            sys.stderr.write(f"[*] [OneVault] Warning: FactDesktopWindowRead creation skipped ({e})\n")

        return result

    def list_inspectable_windows(self) -> List[Dict[str, Any]]:
        """List visible desktop windows filtered through the UIA policy engine (GR3/GR4)."""
        if not self.user32:
            return []

        self.tree_parser._ensure_desktop_access()
        inspectable = []

        def enum_cb(hwnd: int, lparam: int) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True

            info = self.tree_parser.get_window_info(hwnd)
            allowed, reason = self.policy.can_inspect_window(info)
            if allowed:
                inspectable.append({
                    "hwnd": hwnd,
                    "process_name": info.get("process_name", ""),
                    "title": info.get("title", ""),
                    "pid": info.get("pid", 0)
                })
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows(WNDENUMPROC(enum_cb), 0)
        return inspectable

    def _ensure_desktop_access(self) -> None:
        """Ensure current thread is attached to interactive desktop (Default)."""
        if not self.user32:
            return
        try:
            h_default = self.user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_default:
                self.user32.SetThreadDesktop(h_default)
        except Exception:
            pass

    def get_cursor_position(self) -> Optional[Tuple[int, int]]:
        """Read-only query of current mouse cursor coordinates without modifying position.
        Returns (x, y) if successful, or None if reading fails (WO-016.1 R2).
        """
        if not self.user32:
            return None

        # 1. Try directly on current thread
        pt = wintypes.POINT()
        if self.user32.GetCursorPos(ctypes.byref(pt)):
            return (int(pt.x), int(pt.y))

        # 2. If blocked by sandbox/service desktop, query via clean thread attached to Default desktop
        import threading
        pos_container: List[Optional[Tuple[int, int]]] = [None]

        def _read_from_default_desktop():
            try:
                h_default = self.user32.OpenDesktopW("Default", 0, False, 0x01FF)
                if h_default:
                    if self.user32.SetThreadDesktop(h_default):
                        p = wintypes.POINT()
                        if self.user32.GetCursorPos(ctypes.byref(p)):
                            pos_container[0] = (int(p.x), int(p.y))
                    ctypes.windll.kernel32.CloseHandle(h_default)
            except Exception:
                pass

        t = threading.Thread(target=_read_from_default_desktop, daemon=True)
        t.start()
        t.join(timeout=1.0)
        return pos_container[0]

    # -------------------------------------------------------------------------
    # Prohibited Input Methods (GR1: Strict Read-Only Policy)
    # -------------------------------------------------------------------------
    def click(self, x: int, y: int, selector: Optional[str] = None) -> bool:
        logger.warning("[Policy] Desktop input disabled by policy (WO-016 Read-Only Phase): click")
        return False

    def double_click(self, x: int, y: int, selector: Optional[str] = None) -> bool:
        logger.warning("[Policy] Desktop input disabled by policy (WO-016 Read-Only Phase): double_click")
        return False

    def right_click(self, x: int, y: int, selector: Optional[str] = None) -> bool:
        logger.warning("[Policy] Desktop input disabled by policy (WO-016 Read-Only Phase): right_click")
        return False

    def mouse_move(self, x: int, y: int) -> bool:
        """GR1: Mouse movement is strictly disabled. SetCursorPos removed."""
        logger.warning("[Policy] Desktop input disabled by policy (WO-016 Read-Only Phase): mouse_move")
        return False

    def drag_and_drop(self, start_x: int, start_y: int, end_x: int, end_y: int, steps: int = 10) -> bool:
        logger.warning("[Policy] Desktop input disabled by policy (WO-016 Read-Only Phase): drag_and_drop")
        return False

    def type_text(self, text: str, selector: Optional[str] = None) -> bool:
        logger.warning("[Policy] Desktop input disabled by policy (WO-016 Read-Only Phase): type_text")
        return False

    def press_key(self, key: str) -> bool:
        logger.warning("[Policy] Desktop input disabled by policy (WO-016 Read-Only Phase): press_key")
        return False

    def hotkey(self, keys: List[str]) -> bool:
        logger.warning("[Policy] Desktop input disabled by policy (WO-016 Read-Only Phase): hotkey")
        return False

    def scroll(self, delta_x: int, delta_y: int, x: Optional[int] = None, y: Optional[int] = None) -> bool:
        logger.warning("[Policy] Desktop input disabled by policy (WO-016 Read-Only Phase): scroll")
        return False

    def wait_until(self, condition: Callable[[], bool], timeout_ms: int = 5000, poll_interval_ms: int = 50) -> bool:
        return False
