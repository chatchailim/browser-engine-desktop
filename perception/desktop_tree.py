"""Desktop UI Automation Tree Perception (Level 5 Strategy) for browser-engine-desktop (WO-016).

Traverses Windows UI Automation tree of permitted application windows and returns
a privacy-masked UI representation conforming to GR5 and GR6:
- GR5: Elements flagged as password fields NEVER expose their value or length (is_password=True, value=None).
- GR6: Plaintext text from window elements is NEVER exposed or stored in traces/Facts/logs.
       Only SHA-256 hashes of text, element role, bounds [x, y, w, h], and automation_id are returned.
- GR7: Data remains strictly local.

Library & Installation:
- Uses `comtypes` (pip install comtypes) to interface directly with Windows standard `UIAutomationCore.dll`.
"""
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import pathlib
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import rfc8785
except ImportError:
    rfc8785 = None

from policy.uia_policy import UIAPolicyEngine, get_uia_policy

# Windows UIA Control Type IDs to Semantic Role Names
UIA_CONTROL_TYPE_NAMES: Dict[int, str] = {
    50000: "Button",
    50001: "Calendar",
    50002: "CheckBox",
    50003: "ComboBox",
    50004: "Edit",
    50005: "Hyperlink",
    50006: "Image",
    50007: "ListItem",
    50008: "List",
    50009: "Menu",
    50010: "MenuBar",
    50011: "MenuItem",
    50012: "ProgressBar",
    50013: "RadioButton",
    50014: "ScrollBar",
    50015: "Slider",
    50016: "Spinner",
    50017: "StatusBar",
    50018: "Tab",
    50019: "TabItem",
    50020: "Text",
    50021: "ToolBar",
    50022: "ToolTip",
    50023: "Tree",
    50024: "TreeItem",
    50025: "Custom",
    50026: "Group",
    50027: "Thumb",
    50028: "DataGrid",
    50029: "DataItem",
    50030: "Document",
    50031: "SplitButton",
    50032: "Window",
    50033: "Pane",
    50034: "Header",
    50035: "HeaderItem",
    50036: "Table",
    50037: "TitleBar",
    50038: "Separator"
}


def canonical_sha256(data: Any) -> str:
    """Compute deterministic SHA-256 hash using RFC 8785 canonical JSON if available."""
    if rfc8785 is not None:
        try:
            canonical_bytes = rfc8785.dumps(data)
            return "sha256:" + hashlib.sha256(canonical_bytes).hexdigest()
        except Exception:
            pass
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class DesktopTreeParser:
    def __init__(self, policy: Optional[UIAPolicyEngine] = None):
        self.policy = policy or get_uia_policy()
        self._uia = None
        self._uia_module = None
        self._uia_init_error = None
        self._user32 = None
        self._kernel32 = None
        self._init_windows_apis()

    def _init_windows_apis(self) -> None:
        """Initialize Win32 and UIAutomation COM interfaces with pointer-safe ctypes prototypes."""
        try:
            self._user32 = ctypes.windll.user32
            self._kernel32 = ctypes.windll.kernel32
            self._setup_win32_prototypes()
        except Exception:
            self._user32 = None
            self._kernel32 = None

        try:
            import comtypes.client
            from comtypes.client import CreateObject, GetModule
            self._uia_module = GetModule("UIAutomationCore.dll")
            self._uia = CreateObject(self._uia_module.CUIAutomation, interface=self._uia_module.IUIAutomation)
        except Exception as e:
            self._uia = None
            self._uia_module = None
            self._uia_init_error = type(e).__name__

    def _setup_win32_prototypes(self) -> None:
        """Declare pointer-safe ctypes restype and argtypes for 64-bit Windows."""
        if self._kernel32:
            try:
                # Process & Snapshot APIs
                self._kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
                self._kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]

                self._kernel32.Process32FirstW.restype = wintypes.BOOL
                self._kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]

                self._kernel32.Process32NextW.restype = wintypes.BOOL
                self._kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]

                self._kernel32.OpenProcess.restype = wintypes.HANDLE
                self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

                self._kernel32.CloseHandle.restype = wintypes.BOOL
                self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

                self._kernel32.GetProcessTimes.restype = wintypes.BOOL
                self._kernel32.GetProcessTimes.argtypes = [
                    wintypes.HANDLE,
                    ctypes.POINTER(FILETIME),
                    ctypes.POINTER(FILETIME),
                    ctypes.POINTER(FILETIME),
                    ctypes.POINTER(FILETIME)
                ]

                self._kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
                self._kernel32.QueryFullProcessImageNameW.argtypes = [
                    wintypes.HANDLE,
                    wintypes.DWORD,
                    wintypes.LPWSTR,
                    ctypes.POINTER(wintypes.DWORD)
                ]

                self._kernel32.GetExitCodeProcess.restype = wintypes.BOOL
                self._kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

                self._kernel32.TerminateProcess.restype = wintypes.BOOL
                self._kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            except Exception:
                pass

        if self._user32:
            try:
                self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
                self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

                self._user32.GetWindowTextW.restype = ctypes.c_int
                self._user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

                self._user32.GetWindowTextLengthW.restype = ctypes.c_int
                self._user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]

                self._user32.IsWindowVisible.restype = wintypes.BOOL
                self._user32.IsWindowVisible.argtypes = [wintypes.HWND]
            except Exception:
                pass

    def _ensure_desktop_access(self) -> None:
        """Ensure current thread is attached to interactive desktop (Default) if running in service/sandbox."""
        if not self._user32:
            return
        try:
            h_default = self._user32.OpenDesktopW("Default", 0, False, 0x01FF)
            if h_default:
                self._user32.SetThreadDesktop(h_default)
        except Exception:
            pass

    def get_window_info(self, hwnd: int) -> Dict[str, Any]:
        """Extract process name, window title, and elevation info for an HWND."""
        if not self._user32 or not self._kernel32 or not hwnd:
            return {"hwnd": hwnd, "process_name": "", "title": ""}

        self._ensure_desktop_access()

        # Window title
        length = self._user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            self._user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

        # Process ID & Name
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_name = ""
        is_elevated_higher = False

        if pid.value:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if h_proc:
                path_buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(len(path_buf))
                if self._kernel32.QueryFullProcessImageNameW(h_proc, 0, path_buf, ctypes.byref(size)):
                    process_name = os.path.basename(path_buf.value)
                self._kernel32.CloseHandle(h_proc)
            else:
                # If cannot open process at all due to ERROR_ACCESS_DENIED (5), it is running elevated/system
                err = self._kernel32.GetLastError()
                if err == 5:
                    is_elevated_higher = True

        return {
            "hwnd": hwnd,
            "pid": pid.value,
            "process_name": process_name,
            "title": title,
            "is_elevated_higher": is_elevated_higher
        }

    @staticmethod
    def _is_valid_handle(h: Any) -> bool:
        """Check if Win32 handle is non-null and not INVALID_HANDLE_VALUE."""
        if not h:
            return False
        try:
            val = int(h)
            if val == -1 or val == 0xFFFFFFFFFFFFFFFF or val == 0xFFFFFFFF:
                return False
        except (ValueError, TypeError):
            return False
        return True

    def _get_process_creation_time(self, pid: int) -> Optional[int]:
        """Query 64-bit creation timestamp of process via GetProcessTimes."""
        if not self._kernel32 or not pid:
            return None
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = None
        try:
            h_proc = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
            if not self._is_valid_handle(h_proc):
                return None
            creation = FILETIME()
            exit_time = FILETIME()
            kernel_time = FILETIME()
            user_time = FILETIME()
            if self._kernel32.GetProcessTimes(
                h_proc,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time)
            ):
                return (int(creation.dwHighDateTime) << 32) + int(creation.dwLowDateTime)
        except Exception:
            pass
        finally:
            if self._is_valid_handle(h_proc):
                try:
                    self._kernel32.CloseHandle(h_proc)
                except Exception:
                    pass
        return None

    def _is_process_alive(self, pid: int) -> bool:
        """Check if process is still running without sending signals or terminating."""
        if not self._kernel32 or not pid:
            return False
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = None
        try:
            h_proc = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
            if not self._is_valid_handle(h_proc):
                return False
            exit_code = wintypes.DWORD()
            if self._kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
                STILL_ACTIVE = 259
                return exit_code.value == STILL_ACTIVE
        except Exception:
            pass
        finally:
            if self._is_valid_handle(h_proc):
                try:
                    self._kernel32.CloseHandle(h_proc)
                except Exception:
                    pass
        return False

    def _get_process_image_name(self, pid: int) -> str:
        """Query executable image basename for a PID via QueryFullProcessImageNameW."""
        if not self._kernel32 or not pid:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h_proc = None
        try:
            h_proc = self._kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
            if not self._is_valid_handle(h_proc):
                return ""
            pbuf = ctypes.create_unicode_buffer(1024)
            sz = wintypes.DWORD(len(pbuf))
            if self._kernel32.QueryFullProcessImageNameW(h_proc, 0, pbuf, ctypes.byref(sz)):
                return os.path.basename(pbuf.value)
        except Exception:
            pass
        finally:
            if self._is_valid_handle(h_proc):
                try:
                    self._kernel32.CloseHandle(h_proc)
                except Exception:
                    pass
        return ""

    def _get_child_pids(self, parent_pid: int, parent_creation_time: Optional[int] = None) -> Set[int]:
        """Find direct child process IDs spawned by parent_pid with lineage & creation time verification."""
        if not self._kernel32 or not parent_pid:
            return set()
        child_pids: Set[int] = set()
        h_snap = None
        try:
            TH32CS_SNAPPROCESS = 0x00000002
            h_snap = self._kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if not self._is_valid_handle(h_snap):
                return set()
            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ('dwSize', wintypes.DWORD),
                    ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD),
                    ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD),
                    ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD),
                    ('pcPriClassBase', ctypes.c_long),
                    ('dwFlags', wintypes.DWORD),
                    ('szExeFile', ctypes.c_wchar * 260)
                ]
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if self._kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
                while True:
                    if int(pe.th32ParentProcessID) == int(parent_pid):
                        c_pid = int(pe.th32ProcessID)
                        # Verify child creation time >= parent creation time if parent_creation_time known
                        if parent_creation_time is not None:
                            c_ctime = self._get_process_creation_time(c_pid)
                            if c_ctime is not None and c_ctime >= parent_creation_time:
                                child_pids.add(c_pid)
                        else:
                            child_pids.add(c_pid)
                    if not self._kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                        break
        except Exception:
            pass
        finally:
            if self._is_valid_handle(h_snap):
                try:
                    self._kernel32.CloseHandle(h_snap)
                except Exception:
                    pass
        return child_pids

    def _get_uia_element_name(self, hwnd: int) -> str:
        """Query UIA Element Name for window element if UIA COM is available."""
        if not self._uia:
            return ""
        try:
            elem = self._uia.ElementFromHandle(hwnd)
            if elem:
                return str(getattr(elem, "CurrentName", "") or "")
        except Exception:
            pass
        return ""

    def find_window_with_ownership(
        self,
        expected_pid: Optional[int] = None,
        expected_process_name: Optional[str] = None,
        title_keyword: Optional[str] = None,
        timeout_sec: float = 0.0,
        poll_interval: float = 0.2,
        expected_creation_time: Optional[int] = None
    ) -> Tuple[Optional[int], Dict[str, Any]]:
        """Find a topmost visible HWND matching ownership constraints with bounded polling.
        
        Strict ownership principles (WO-017.13):
        1. If both expected_pid and title_keyword are None, fails closed (no unconstrained first-match).
        2. If expected_pid is provided, windows belonging to other PIDs (e.g. user processes) are ignored.
        3. Exact image basename comparison is required (no substring matching).
        4. Reused PIDs with same name are rejected via creation timestamp verification.
        5. Child PIDs must have verifiable lineage from expected_pid.
        6. If multiple ambiguous candidates match, fails closed.
        7. If timeout expires without finding an owned window, fails closed.
        """
        if not self._user32:
            return None, {"error": "user32_not_available", "verified": False}

        # Rule 1: Fail closed on unconstrained / process-name-only first match
        if expected_pid is None and not title_keyword:
            return None, {
                "error": "unconstrained_search_prohibited",
                "reason": "Process-name-only first match without PID or title_keyword is prohibited",
                "verified": False
            }

        self._ensure_desktop_access()

        t_start = time.monotonic()
        poll_interval = max(0.05, float(poll_interval))
        timeout_sec = max(0.0, float(timeout_sec))

        # Query and lock expected creation time if expected_pid is provided and not already given
        if expected_pid is not None:
            if expected_creation_time is None:
                expected_creation_time = self._get_process_creation_time(expected_pid)
            if expected_creation_time is None:
                return None, {
                    "error": "expected_process_identity_unverified",
                    "reason": f"Unable to verify creation timestamp for expected_pid {expected_pid}",
                    "verified": False,
                    "elapsed_sec": round(time.monotonic() - t_start, 3)
                }

        while True:
            # Build current owned PIDs set with verified child lineage
            owned_pids = set()
            child_pids = set()
            if expected_pid is not None:
                owned_pids.add(expected_pid)
                child_pids = set()  # Handoff needs retained launch handles; do not infer ownership from PPID.
                owned_pids.update(child_pids)

            candidate_windows: List[Dict[str, Any]] = []

            def enum_proc(hwnd: int, lparam: int) -> bool:
                if not self._user32.IsWindowVisible(hwnd):
                    return True

                info = self.get_window_info(hwnd)
                w_pid = info.get("pid")
                w_proc = info.get("process_name", "")
                w_title = info.get("title", "")

                # 1. PID ownership check
                if expected_pid is not None:
                    if w_pid not in owned_pids:
                        return True
                    cand_ctime = self._get_process_creation_time(w_pid)
                    if cand_ctime is None:
                        # Cannot verify candidate identity - fail closed / ignore candidate
                        return True
                    # Same-name PID reuse check for expected_pid
                    if w_pid == expected_pid:
                        if cand_ctime != expected_creation_time:
                            # Reused PID detected - ignore candidate!
                            return True
                    elif w_pid in child_pids:
                        if cand_ctime < expected_creation_time:
                            # Child spawned before parent or invalid lineage
                            return True

                # 2. Exact image basename comparison (NO substring!)
                if expected_process_name:
                    if not w_proc:
                        # Cannot verify image name - fail closed / ignore candidate
                        return True
                    exp_base = os.path.basename(expected_process_name).lower()
                    cand_base = os.path.basename(w_proc).lower()
                    if cand_base != exp_base:
                        return True

                # 3. Title verification (Win32 title + UIA Name fallback)
                title_match = False
                matched_source = "none"
                if title_keyword:
                    kw_lower = title_keyword.lower()
                    if kw_lower in w_title.lower():
                        title_match = True
                        matched_source = "win32_title"
                    else:
                        uia_name = self._get_uia_element_name(hwnd)
                        if kw_lower in uia_name.lower():
                            title_match = True
                            matched_source = "uia_name"

                candidate_windows.append({
                    "hwnd": hwnd,
                    "pid": w_pid,
                    "process_name": w_proc,
                    "title": w_title,
                    "title_match": title_match,
                    "matched_source": matched_source
                })
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            self._user32.EnumWindows(WNDENUMPROC(enum_proc), 0)

            # Filter candidates based on criteria
            if title_keyword:
                verified = [c for c in candidate_windows if c["title_match"]]
            else:
                verified = candidate_windows

            if len(verified) == 1:
                sel = verified[0]
                proof = {
                    "verified": True,
                    "selected_hwnd": sel["hwnd"],
                    "pid": sel["pid"],
                    "process_name": sel["process_name"],
                    "ownership_type": "pid_match" if expected_pid is not None else "title_keyword_match",
                    "creation_time_verified": (expected_creation_time is not None and sel["pid"] == expected_pid),
                    "child_pids": list(child_pids),
                    "title_matched": sel["title_match"],
                    "title_source": sel["matched_source"],
                    "title_hash": "sha256:" + hashlib.sha256(sel["title"].encode("utf-8")).hexdigest() if sel["title"] else "",
                    "elapsed_sec": round(time.monotonic() - t_start, 3)
                }
                return sel["hwnd"], proof

            if len(verified) > 1:
                # Ambiguous candidate windows detected - fail closed!
                return None, {
                    "error": "ambiguous_candidates",
                    "candidate_count": len(verified),
                    "candidate_hwnds": [c["hwnd"] for c in verified],
                    "verified": False,
                    "elapsed_sec": round(time.monotonic() - t_start, 3)
                }

            # 0 verified candidates found
            elapsed = time.monotonic() - t_start
            if elapsed >= timeout_sec:
                break

            time.sleep(poll_interval)

        return None, {
            "error": "timeout_no_owned_window",
            "timeout_sec": timeout_sec,
            "elapsed_sec": round(time.monotonic() - t_start, 3),
            "expected_pid": expected_pid,
            "verified": False
        }

    def find_window_by_process_or_title(
        self,
        process_name: Optional[str] = None,
        title_keyword: Optional[str] = None,
        expected_pid: Optional[int] = None,
        timeout_sec: float = 0.0
    ) -> Optional[int]:
        """Find window respecting ownership contracts. Fails closed if ownership is unproven."""
        hwnd, _ = self.find_window_with_ownership(
            expected_pid=expected_pid,
            expected_process_name=process_name,
            title_keyword=title_keyword,
            timeout_sec=timeout_sec
        )
        return hwnd

    def _extract_masked_element_node(self, elem: Any) -> Optional[Dict[str, Any]]:
        """Extract a single UIA element node and immediately apply GR5/GR6 privacy masking."""
        if not elem:
            return None

        try:
            control_type_id = int(getattr(elem, "CurrentControlType", 0) or getattr(elem, "control_type", 0) or 0)
            role = UIA_CONTROL_TYPE_NAMES.get(control_type_id, f"Custom_{control_type_id}")
            automation_id = str(getattr(elem, "CurrentAutomationId", "") or getattr(elem, "automation_id", "") or "")
            class_name = str(getattr(elem, "CurrentClassName", "") or getattr(elem, "class_name", "") or "")
            is_password = bool(getattr(elem, "CurrentIsPassword", 0) or getattr(elem, "is_password", False))

            raw_name = ""
            try:
                raw_name = str(getattr(elem, "CurrentName", "") or getattr(elem, "name", "") or "")
            except Exception:
                pass

            label_text = ""
            try:
                lbl = getattr(elem, "CurrentLabeledBy", None)
                if lbl:
                    label_text = str(getattr(lbl, "CurrentName", "") or "")
                if not label_text:
                    label_text = str(getattr(elem, "CurrentHelpText", "") or getattr(elem, "label", "") or "")
            except Exception:
                pass

            # Bounding rectangle: [x, y, w, h]
            bounds = [0, 0, 0, 0]
            try:
                rect = elem.CurrentBoundingRectangle
                bounds = [
                    int(rect.left),
                    int(rect.top),
                    int(rect.right - rect.left),
                    int(rect.bottom - rect.top)
                ]
            except Exception:
                pass

            # Enforce GR5 & WO-016.1 R1: Password elements detection (including unannounced password fields)
            element_meta = {
                "name": raw_name,
                "label": label_text,
                "role": role,
                "control_type": role,
                "automation_id": automation_id,
                "class_name": class_name,
                "is_password": is_password
            }
            if self.policy.is_password_element(element_meta):
                return {
                    "role": role,
                    "automation_id": automation_id,
                    "bounds": bounds,
                    "is_password": True,
                    "text_hash": None,
                    "children": []
                }

            # Enforce GR6: Non-password elements: Extract text in-memory ONLY to compute SHA-256 hash
            raw_text = ""
            try:
                raw_text = elem.CurrentName or ""
            except Exception:
                pass

            # Document/Edit names can be labels rather than document contents.
            # Read content patterns only after the password policy gate above.
            if control_type_id in (50004, 50030) and self._uia_module:
                raw_text = ""
                try:
                    text_unk = elem.GetCurrentPattern(self._uia_module.UIA_TextPatternId)
                    if text_unk:
                        text_pat = text_unk.QueryInterface(self._uia_module.IUIAutomationTextPattern)
                        raw_text = text_pat.DocumentRange.GetText(65536) or ""
                except Exception:
                    pass
            if not raw_text and control_type_id == 50004 and self._uia_module:
                try:
                    val_unk = elem.GetCurrentPattern(self._uia_module.UIA_ValuePatternId)
                    if val_unk:
                        val_pat = val_unk.QueryInterface(self._uia_module.IUIAutomationValuePattern)
                        raw_text = val_pat.CurrentValue or ""
                except Exception:
                    pass

            text_hash = None
            if raw_text:
                text_hash = "sha256:" + hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

            # The raw text is discarded here. It NEVER enters the returned node.
            return {
                "role": role,
                "automation_id": automation_id,
                "bounds": bounds,
                "is_password": False,
                "text_hash": text_hash,
                "children": []
            }
        except Exception:
            return None

    def build_masked_tree(
        self,
        hwnd_or_elem: Union[int, Any],
        max_depth: int = 8,
        max_elements: int = 500
    ) -> Dict[str, Any]:
        """Traverse the UI Automation tree of an allowed window and return a masked structure.
        
        Zero plaintext user window text, zero screenshots.
        """
        self._ensure_desktop_access()

        if self._uia is None or self._uia_module is None:
            return {
                "ok": False,
                "error": "uia_not_available",
                "initialization_error_type": self._uia_init_error,
                "element_count": 0,
                "tree": None,
                "structure_hash": ""
            }

        root_elem = None
        window_info = {}
        if isinstance(hwnd_or_elem, int):
            hwnd = hwnd_or_elem
            window_info = self.get_window_info(hwnd)
            # Enforce Policy Check (R2)
            allowed, reason = self.policy.can_inspect_window(window_info)
            if not allowed:
                return {
                    "ok": False,
                    "error": reason,
                    "window_info": {
                        "process_name": window_info.get("process_name", ""),
                        "title_hash": "sha256:" + hashlib.sha256(window_info.get("title", "").encode()).hexdigest()
                    },
                    "element_count": 0,
                    "tree": None,
                    "structure_hash": ""
                }
            try:
                root_elem = self._uia.ElementFromHandle(hwnd)
            except Exception as e:
                return {
                    "ok": False,
                    "error": f"ElementFromHandle_failed: {e}",
                    "element_count": 0,
                    "tree": None,
                    "structure_hash": ""
                }
        else:
            root_elem = hwnd_or_elem

        if not root_elem:
            return {
                "ok": False,
                "error": "null_root_element",
                "element_count": 0,
                "tree": None,
                "structure_hash": ""
            }

        # Build root masked node
        root_node = self._extract_masked_element_node(root_elem)
        if not root_node:
            root_node = {
                "role": "Window",
                "automation_id": "",
                "bounds": [0, 0, 0, 0],
                "is_password": False,
                "text_hash": None,
                "children": []
            }

        element_count = 1
        walker = self._uia.CreateTreeWalker(self._uia.ControlViewCondition)

        def traverse(node: Dict[str, Any], elem: Any, depth: int) -> None:
            nonlocal element_count
            if depth >= max_depth or element_count >= max_elements:
                return

            try:
                child = walker.GetFirstChildElement(elem)
            except Exception:
                child = None

            while child and element_count < max_elements:
                child_node = self._extract_masked_element_node(child)
                if child_node:
                    element_count += 1
                    node["children"].append(child_node)
                    traverse(child_node, child, depth + 1)
                try:
                    child = walker.GetNextSiblingElement(child)
                except Exception:
                    break

        traverse(root_node, root_elem, 1)

        struct_hash = canonical_sha256(root_node)
        raw_title = window_info.get("title", "")
        title_hash = "sha256:" + hashlib.sha256(raw_title.encode("utf-8")).hexdigest() if raw_title else ""

        return {
            "ok": True,
            "process_name": window_info.get("process_name", ""),
            "window_title_hash": title_hash,
            "element_count": element_count,
            "structure_hash": struct_hash,
            "tree": root_node
        }


def cleanup_owned_process_tree(
    parent_pid, child_pids=None, expected_process_name="notepad.exe",
    expected_creation_time=None, timeout=2.0, *, retained_handle=None, kernel32=None
):
    """Only terminate the launcher's retained handle, never reopen a PID to kill it.

    The handle is borrowed from Popen and remains owned by Popen. Unproven
    packaged-app handoffs are deliberately unsupported; no child PID is killed.
    """
    audit = {"parent_pid": parent_pid, "child_pids": sorted(child_pids or []),
             "terminated_pids": [], "all_cleaned": False, "verified": False, "errors": []}
    if child_pids:
        audit["errors"].append("Unproven child process handles; cleanup refused")
        return audit
    if not retained_handle or expected_creation_time is None or not expected_process_name:
        audit["errors"].append("Retained launch handle and identity are required")
        return audit
    try:
        api = kernel32
        if api is None:
            api = ctypes.windll.kernel32
        api.GetProcessId.argtypes = [wintypes.HANDLE]
        api.GetProcessId.restype = wintypes.DWORD
        api.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
        api.GetProcessTimes.restype = wintypes.BOOL
        api.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        api.QueryFullProcessImageNameW.restype = wintypes.BOOL
        api.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateProcess.restype = wintypes.BOOL
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        if api.GetProcessId(retained_handle) != parent_pid:
            raise ValueError("Launch handle PID mismatch")
        times = [FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(retained_handle, *[ctypes.byref(t) for t in times]):
            raise ValueError("Unknown process creation time")
        created = (int(times[0].dwHighDateTime) << 32) | int(times[0].dwLowDateTime)
        if created != expected_creation_time:
            raise ValueError("Launch identity mismatch")
        state = api.WaitForSingleObject(retained_handle, 0)
        if state == 0:  # confirmed exit of the retained process object
            audit.update(all_cleaned=True, verified=True)
            return audit
        if state != 258:
            raise ValueError("Process wait failed; liveness unknown")
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not api.QueryFullProcessImageNameW(retained_handle, 0, buf, ctypes.byref(size)):
            raise ValueError("Unknown process image")
        if os.path.basename(buf.value).casefold() != os.path.basename(expected_process_name).casefold():
            raise ValueError("Process image mismatch")
        if not api.TerminateProcess(retained_handle, 1):
            raise ValueError("Termination failed")
        audit["terminated_pids"].append(parent_pid)
        if api.WaitForSingleObject(retained_handle, max(0, min(60000, int(timeout * 1000)))) != 0:
            raise ValueError("Exit not confirmed before timeout")
        audit.update(all_cleaned=True, verified=True)
    except Exception as exc:
        audit["errors"].append(str(exc))
    return audit


def verify_synthetic_content_presence(
    tree_node: Optional[Dict[str, Any]],
    expected_hashes: Set[str],
    disallowed_hashes: Optional[Set[str]] = None
) -> bool:
    """Check whether any document body element in the masked tree matches expected synthetic content hashes.
    
    Strict rules (WO-017.14):
    1. Identification-only elements (Window, TitleBar, TabItem, Header, MenuBar, etc.) or nodes without
       explicit document body roles are excluded from document content proof.
    2. Any node matching disallowed_hashes (e.g. filename, tab title, or balance alone) is rejected.
    3. Only document body/content roles (Document, Edit, Text) are accepted.
    """
    if not tree_node or not isinstance(tree_node, dict) or not expected_hashes:
        return False

    BODY_ROLES = {"Document", "Edit", "Text"}

    role = str(tree_node.get("role", "") or "")
    node_hash = tree_node.get("text_hash")

    if node_hash and role in BODY_ROLES:
        if disallowed_hashes and node_hash in disallowed_hashes:
            pass  # Explicitly disallowed (e.g. filename/title/balance-only)
        elif node_hash in expected_hashes:
            return True

    # Check children recursively
    children = tree_node.get("children", [])
    if isinstance(children, list):
        for child in children:
            if verify_synthetic_content_presence(child, expected_hashes, disallowed_hashes=disallowed_hashes):
                return True
    return False

