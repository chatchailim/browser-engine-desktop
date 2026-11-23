"""Native boundary for the opt-in supervised console. Not used by browser tasks.

SendInput is global: foreground checks reduce, but cannot eliminate, focus races.
Never change desktops, elevate, activate windows, or bypass UIPI.
"""
import ctypes as C
from ctypes import wintypes as W
import json
import pathlib
import time
from contextlib import contextmanager

from execution.guarded_desktop import InputDenied, Target
from policy.uia_policy import BLACKLIST_PROCESS_NAMES, BLACKLIST_TITLE_PATTERNS, UIAPolicyEngine


ULONG_PTR = C.c_size_t


class MOUSEINPUT(C.Structure):
    _fields_ = [("dx", W.LONG), ("dy", W.LONG), ("mouseData", W.DWORD),
                ("dwFlags", W.DWORD), ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(C.Structure):
    _fields_ = [("wVk", W.WORD), ("wScan", W.WORD), ("dwFlags", W.DWORD),
                ("time", W.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(C.Structure):
    _fields_ = [("uMsg", W.DWORD), ("wParamL", W.WORD), ("wParamH", W.WORD)]


class INPUTUNION(C.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(C.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", W.DWORD), ("u", INPUTUNION)]


VK = {"LEFT": 0x25, "UP": 0x26, "RIGHT": 0x27, "DOWN": 0x28, "HOME": 0x24,
      "END": 0x23, "BACKSPACE": 8, "DELETE": 0x2e, "TAB": 9, "ENTER": 13, "ESC": 27}

_EXTRA_VK = {"CTRL": 0x11, "ALT": 0x12, "WIN": 0x5b}  # เข้าถึงได้เมื่อ modifiers_bypass=true เท่านั้น

BLOCKED_EXECUTABLES = BLACKLIST_PROCESS_NAMES | {
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe",
    "wscript.exe", "cscript.exe", "regedit.exe", "mmc.exe", "taskmgr.exe",
}


def _unrestricted_cfg():
    """WO-018 R2: ธง unrestricted — อ่านจากไฟล์ config ตัวเดียวกับ shell_runner (testable)."""
    from execution.shell_runner import _cfg
    return _cfg().get("unrestricted_desktop", {}) or {}


def _blocked_executables_effective():
    extra = BLOCKED_EXECUTABLES - BLACKLIST_PROCESS_NAMES  # เฉพาะที่โค้ดชุดนี้เพิ่ม (terminal/regedit/taskmgr)
    if _unrestricted_cfg().get("blocked_executables_bypass") is True:
        # ธงเปิด = ปลด 'ที่ใบอนุมัติครอบ' — ยกเว้นรายการป้องกันระบบปฏิบัติการ (จาก UIA policy เดิม) ยังปิดเสมอ
        return set()
    return BLOCKED_EXECUTABLES


class Win32InputBackend:
    def __init__(self, hwnd):
        self.hwnd, self.handle = hwnd, None
        self.mutex = None
        self.old_dpi = None
        self.u = C.WinDLL("user32", use_last_error=True)
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.a = C.WinDLL("advapi32", use_last_error=True)
        specs = [
            (self.u, "GetForegroundWindow", W.HWND, []),
            (self.u, "GetWindowThreadProcessId", W.DWORD, [W.HWND, C.POINTER(W.DWORD)]),
            (self.u, "GetAncestor", W.HWND, [W.HWND, W.UINT]),
            (self.u, "WindowFromPoint", W.HWND, [W.POINT]),
            (self.u, "GetClientRect", W.BOOL, [W.HWND, C.POINTER(W.RECT)]),
            (self.u, "ClientToScreen", W.BOOL, [W.HWND, C.POINTER(W.POINT)]),
            (self.u, "IsWindowVisible", W.BOOL, [W.HWND]),
            (self.u, "IsWindowEnabled", W.BOOL, [W.HWND]),
            (self.u, "GetWindowTextW", C.c_int, [W.HWND, W.LPWSTR, C.c_int]),
            (self.u, "GetAsyncKeyState", C.c_short, [C.c_int]),
            (self.u, "OpenInputDesktop", W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
            (self.u, "GetUserObjectInformationW", W.BOOL, [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]),
            (self.u, "CloseDesktop", W.BOOL, [W.HANDLE]),
            (self.u, "SetCursorPos", W.BOOL, [C.c_int, C.c_int]),
            (self.u, "SendInput", W.UINT, [W.UINT, C.POINTER(INPUT), C.c_int]),
            (self.u, "SetThreadDpiAwarenessContext", W.HANDLE, [W.HANDLE]),
            (self.k, "OpenProcess", W.HANDLE, [W.DWORD, W.BOOL, W.DWORD]),
            (self.k, "CloseHandle", W.BOOL, [W.HANDLE]),
            (self.k, "WaitForSingleObject", W.DWORD, [W.HANDLE, W.DWORD]),
            (self.k, "CreateMutexW", W.HANDLE, [C.c_void_p, W.BOOL, W.LPCWSTR]),
            (self.k, "ReleaseMutex", W.BOOL, [W.HANDLE]),
            (self.k, "GetProcessTimes", W.BOOL, [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4),
            (self.k, "QueryFullProcessImageNameW", W.BOOL, [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]),
            (self.a, "OpenProcessToken", W.BOOL, [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)]),
            (self.a, "GetTokenInformation", W.BOOL, [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)]),
        ]
        for dll, name, restype, argtypes in specs:
            fn = getattr(dll, name)
            fn.restype, fn.argtypes = restype, argtypes
        # Import COM only for an explicitly created native backend.
        import comtypes.client
        self.mod = comtypes.client.GetModule("UIAutomationCore.dll")
        self.uia = comtypes.client.CreateObject(self.mod.CUIAutomation, interface=self.mod.IUIAutomation)

    def _desktop_safe(self):
        handle = self.u.OpenInputDesktop(0, False, 1)  # read object name only; no desktop switching
        if not handle:
            raise InputDenied("input_desktop_unavailable")
        try:
            name, size = C.create_unicode_buffer(256), W.DWORD()
            if not self.u.GetUserObjectInformationW(handle, 2, name, C.sizeof(name), C.byref(size)) or name.value != "Default":
                from execution.shell_runner import _cfg as _shell_cfg  # ธง elevation มาจาก config ตัวเดียว
                if not (_shell_cfg().get("unrestricted_desktop", {}) or {}).get("elevated_window_interact"):
                    raise InputDenied("secure_or_unknown_desktop")
        finally:
            self.u.CloseDesktop(handle)

    def _identity(self, handle):
        if self.k.WaitForSingleObject(handle, 0) != 258:
            raise InputDenied("target_process_exited_or_unknown")
        times = [W.FILETIME() for _ in range(4)]
        if not self.k.GetProcessTimes(handle, *(C.byref(t) for t in times)):
            raise InputDenied("process_identity_unavailable")
        path, size = C.create_unicode_buffer(32768), W.DWORD(32768)
        if not self.k.QueryFullProcessImageNameW(handle, 0, path, C.byref(size)):
            raise InputDenied("process_image_unavailable")
        token = W.HANDLE()
        if not self.a.OpenProcessToken(handle, 8, C.byref(token)):
            raise InputDenied("process_privilege_unknown")
        try:
            elevated, returned = W.DWORD(), W.DWORD()
            if not self.a.GetTokenInformation(token, 20, C.byref(elevated), 4, C.byref(returned)) or elevated.value:
                from execution.shell_runner import _cfg as _shell_cfg2
                if not (_shell_cfg2().get("unrestricted_desktop", {}) or {}).get("elevated_window_interact"):
                    raise InputDenied("elevated_or_unknown_process")
        finally:
            self.k.CloseHandle(token)
        image = str(pathlib.Path(path.value))
        if pathlib.Path(image).name.casefold() in _blocked_executables_effective():
            raise InputDenied("security_process_blocked")
        return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime, image

    def _rect(self):
        rect, origin = W.RECT(), W.POINT(0, 0)
        if not self.u.GetClientRect(self.hwnd, C.byref(rect)) or not self.u.ClientToScreen(self.hwnd, C.byref(origin)):
            raise InputDenied("client_bounds_unknown")
        return (origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom)

    def list_allowed_windows(self, allowed_image):
        """Read-only picker inventory. Never activates or sends input to a window."""
        self._desktop_safe()
        results = []
        callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        self.u.EnumWindows.argtypes = [callback_type, W.LPARAM]
        self.u.EnumWindows.restype = W.BOOL

        def visit(hwnd, _):
            handle = None
            try:
                if not self.u.IsWindowVisible(hwnd) or not self.u.IsWindowEnabled(hwnd):
                    return True
                pid = W.DWORD()
                if not self.u.GetWindowThreadProcessId(hwnd, C.byref(pid)):
                    return True
                handle = self.k.OpenProcess(0x1000 | 0x100000, False, pid.value)
                if not handle:
                    return True
                created, image = self._identity(handle)
                if image.casefold() != allowed_image.casefold():
                    return True
                title = C.create_unicode_buffer(1024)
                self.u.GetWindowTextW(hwnd, title, len(title))
                if any(p.search(title.value) for p in BLACKLIST_TITLE_PATTERNS):
                    return True
                results.append({"hwnd": int(hwnd), "pid": pid.value, "created": created,
                                "image": image, "title": title.value})
            except Exception:
                pass  # Unidentifiable candidates are not offered.
            finally:
                if handle:
                    self.k.CloseHandle(handle)
            return True

        if not self.u.EnumWindows(callback_type(visit), 0):
            raise InputDenied("window_inventory_failed")
        return results

    def target_ready(self):
        """Read-only readiness hint; full validation still runs before every input."""
        blocked_keys = (27,) if _unrestricted_cfg().get("modifiers_bypass") is True else (1, 2, 4, 27, 16, 17, 18, 0x5b, 0x5c)
        return (self.u.GetForegroundWindow() == self.hwnd
                and not any(self.u.GetAsyncKeyState(v) & 0x8000
                            for v in blocked_keys))

    def capture(self):
        self._desktop_safe()
        if self.handle:
            raise InputDenied("already_bound")
        return self._capture_target()

    @contextmanager
    def input_scope(self):
        """Serialize input, never idle console/GUI sessions. Same-thread ownership."""
        if self.mutex:
            raise InputDenied("input_scope_already_active")
        self.mutex = self.k.CreateMutexW(None, False, "Local\\BrowserEngineDesktopSupervisedInput")
        if not self.mutex:
            raise InputDenied("desktop_lock_unavailable")
        # Abandoned mutex is also rejected: an interrupted drag may have left input down.
        wait = self.k.WaitForSingleObject(self.mutex, 0)
        if wait != 0:
            if wait == 128:
                self.k.ReleaseMutex(self.mutex)
            self.k.CloseHandle(self.mutex)
            self.mutex = None
            raise InputDenied("desktop_session_busy_or_abandoned")
        try:
            yield
        finally:
            self.k.ReleaseMutex(self.mutex)
            self.k.CloseHandle(self.mutex)
            self.mutex = None

    def _capture_target(self):
        self.old_dpi = self.u.SetThreadDpiAwarenessContext(W.HANDLE(-4))
        if not self.old_dpi:
            self.close()
            raise InputDenied("physical_pixel_context_unavailable")
        if not self.hwnd:
            self.hwnd = self.u.GetForegroundWindow()
        if self.u.GetAncestor(self.hwnd, 2) != self.hwnd:
            raise InputDenied("top_level_window_required")
        pid = W.DWORD()
        if not self.u.GetWindowThreadProcessId(self.hwnd, C.byref(pid)) or not pid.value:
            raise InputDenied("window_identity_unknown")
        self.handle = self.k.OpenProcess(0x1000 | 0x100000, False, pid.value)
        if not self.handle:
            raise InputDenied("process_access_denied")
        try:
            created, image = self._identity(self.handle)
            self.root_id = tuple(self.uia.ElementFromHandle(self.hwnd).GetRuntimeId())
            if not self.root_id:
                raise InputDenied("window_runtime_identity_unknown")
            return Target(self.hwnd, pid.value, created, image, self._rect())
        except BaseException:
            self.close()
            raise

    def validate(self, target, action, point=None):
        ucfg = _unrestricted_cfg()
        self._desktop_safe()
        # Escape held down always stops; an approved synthetic ESC is a key pair.
        # ธง modifiers_bypass=true = ไม่บล็อก ESC/Modifier ค้าง (ผู้ใช้ยอมรับเสี่ยง — WO-018)
        held_watch = (27,) if ucfg.get("modifiers_bypass") is True else (27, 16, 17, 18, 0x5b, 0x5c)
        if any(self.u.GetAsyncKeyState(v) & 0x8000 for v in held_watch):
            raise InputDenied("escape_or_modifier_held")
        pid = W.DWORD()
        self.u.GetWindowThreadProcessId(self.hwnd, C.byref(pid))
        created, image = self._identity(self.handle)
        if (pid.value != target.pid or created != target.created or image.casefold() != target.image.casefold()
                or self._rect() != target.rect or self.u.GetForegroundWindow() != self.hwnd
                or not self.u.IsWindowVisible(self.hwnd) or not self.u.IsWindowEnabled(self.hwnd)):
            raise InputDenied("target_changed_or_not_foreground")
        if tuple(self.uia.ElementFromHandle(self.hwnd).GetRuntimeId()) != self.root_id:
            raise InputDenied("window_replaced")
        title = C.create_unicode_buffer(1024)
        self.u.GetWindowTextW(self.hwnd, title, len(title))
        if not ucfg.get("blocked_titles_bypass") and any(p.search(title.value) for p in BLACKLIST_TITLE_PATTERNS):
            raise InputDenied("security_window_blocked")
        if point is None and action.kind in {"click", "drag"}:
            point = action.args[:2]
        if point is not None:
            x, y = point
            l, t, r, b = target.rect
            if not l <= x < r or not t <= y < b:
                raise InputDenied("point_outside_client")
            under = self.u.WindowFromPoint(W.POINT(x, y))
            if self.u.GetAncestor(under, 2) != self.hwnd:
                raise InputDenied("point_occluded_or_wrong_window")
            pt = self.mod.tagPOINT()
            pt.x, pt.y = x, y
            element = self.uia.ElementFromPoint(pt)
        else:
            element = self.uia.GetFocusedElement()
        if (not element or int(element.CurrentProcessId) != target.pid or not element.CurrentIsEnabled
                or element.CurrentIsOffscreen):
            raise InputDenied("target_control_unknown_or_unavailable")
        # Read metadata only, never value/text content. Unknown properties raise and stop.
        info = {"is_password": bool(element.CurrentIsPassword), "name": element.CurrentName,
                "automation_id": element.CurrentAutomationID if hasattr(element, "CurrentAutomationID") else element.CurrentAutomationId,
                "class_name": element.CurrentClassName}
        # ธง password_input (WO-018 R2): เปิดแล้วไม่ปฏิเสธช่องรหัสผ่าน — แต่กติกาไม่เก็บค่า/แฮชคงเดิมเป๊ะ
        if not ucfg.get("password_input") and UIAPolicyEngine.is_password_element(info):
            raise InputDenied("sensitive_control_blocked")
        # Same PID alone is insufficient: a second window/dialog can own the focus.
        ancestor = element
        for _ in range(64):
            if tuple(ancestor.GetRuntimeId()) == self.root_id:
                break
            ancestor = self.uia.RawViewWalker.GetParentElement(ancestor)
            if not ancestor:
                raise InputDenied("control_outside_bound_window")
        else:
            raise InputDenied("control_ancestry_unknown")
        if action.kind == "type" and int(element.CurrentControlType) not in {50004, 50030}:
            raise InputDenied("editable_text_control_required")

    def _send(self, events):
        array = (INPUT * len(events))(*events)
        if self.u.SendInput(len(events), array, C.sizeof(INPUT)) != len(events):
            raise InputDenied("input_partial_or_denied_no_retry")

    @staticmethod
    def _mouse(flags):
        event = INPUT(type=0)
        event.mi.dwFlags = flags
        return event

    @staticmethod
    def _key(vk=0, scan=0, flags=0):
        event = INPUT(type=1)
        event.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        return event

    def perform(self, action, guard):
        ucfg = _unrestricted_cfg()
        watch_buttons = (1, 2, 4) if ucfg.get("modifiers_bypass") is not True else ()
        if watch_buttons and any(self.u.GetAsyncKeyState(v) & 0x8000 for v in watch_buttons):
            raise InputDenied("physical_mouse_button_held")
        if action.kind in {"click", "drag"}:
            start = action.args[:2]
            guard(start)
            if not self.u.SetCursorPos(*start):
                raise InputDenied("cursor_move_failed")
            if watch_buttons:
                guard(start)
            try:
                self._send([self._mouse(2)])
                if action.kind == "drag":
                    sx, sy, ex, ey = action.args
                    for step in range(1, 21):
                        point = (round(sx + (ex - sx) * step / 20), round(sy + (ey - sy) * step / 20))
                        guard(point)
                        if not self.u.SetCursorPos(*point):
                            raise InputDenied("cursor_move_failed")
                        time.sleep(.025)
            finally:
                # Release only the synthetic button, even after revocation/partial failure.
                self._send([self._mouse(4)])
        elif action.kind == "key":
            guard()
            key_name = action.args[0]
            if key_name in _EXTRA_VK:
                if ucfg.get("modifiers_bypass") is not True:
                    raise InputDenied("modifier_key_not_allowed")
                vk = _EXTRA_VK[key_name]
            else:
                vk = VK[key_name]
            extended = 1 if key_name in {"LEFT", "RIGHT", "UP", "DOWN", "HOME", "END", "DELETE"} else 0
            try:
                self._send([self._key(vk=vk, flags=extended)])
            finally:
                self._send([self._key(vk=vk, flags=extended | 2)])
        elif action.kind == "type":
            for char in action.args[0]:
                guard()
                encoded = char.encode("utf-16-le")
                for i in range(0, len(encoded), 2):
                    unit = int.from_bytes(encoded[i:i+2], "little")
                    try:
                        self._send([self._key(scan=unit, flags=4)])
                    finally:
                        self._send([self._key(scan=unit, flags=4 | 2)])
        else:
            raise InputDenied("unsupported_native_action")

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None
        if self.old_dpi:
            self.u.SetThreadDpiAwarenessContext(self.old_dpi)
            self.old_dpi = None
        if self.mutex:
            self.k.ReleaseMutex(self.mutex)
            self.k.CloseHandle(self.mutex)
            self.mutex = None
