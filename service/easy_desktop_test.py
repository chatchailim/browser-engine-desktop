"""Large-print, human-clicked typing rehearsal. Native calls only after a button click.

No AI/HTTP entrypoint, no commands to copy, no token displayed to the operator.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import subprocess
import time
import hashlib
import tkinter as tk

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from execution.guarded_desktop import GuardedDesktopSession, InputDenied
from service.desktop_console import stop_requested

ALLOWED_IMAGE = r"C:\Windows\System32\notepad.exe"
TEST_ACTION = {"kind": "type", "args": [" TEST-456"]}


def test_documents(windows):
    # This rehearsal never offers named working files such as song lyrics.
    return [w for w in windows if w["title"].lstrip("*").strip() in
            {"Untitled - Notepad", "ไม่มีชื่อ - Notepad"}]


def same_target(target, selected):
    return all(getattr(target, key) == selected[key] for key in ("hwnd", "pid", "created")) and target.image.casefold() == selected["image"].casefold()


def verify_test_text(backend, target):
    """Read only the bound, validated test editor; never claim SendInput == success."""
    from execution.guarded_desktop import Action
    backend.validate(target, Action("type", (" TEST-456",)))
    element = backend.uia.GetFocusedElement()
    try:
        unknown = element.GetCurrentPattern(backend.mod.UIA_TextPatternId)
        pattern = unknown.QueryInterface(backend.mod.IUIAutomationTextPattern)
        value = pattern.DocumentRange.GetText(4096) or ""
    except Exception:
        # Classic Windows Notepad's Edit control may expose only ValuePattern.
        unknown = element.GetCurrentPattern(backend.mod.UIA_ValuePatternId)
        pattern = unknown.QueryInterface(backend.mod.IUIAutomationValuePattern)
        value = pattern.CurrentValue or ""
        if len(value) > 4096:
            raise InputDenied("test_document_too_large")
    return {"event": "test_readback", "expected_suffix_matches": value.endswith(" TEST-456"),
            "character_count": len(value), "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}


class EasyTest:
    def __init__(self, root):
        self.root = root
        self.backend = self.session = None
        self.candidates = []
        self.token = None
        self.after_id = None
        self.audit_path = APP / "data/desktop-input" / ("easy-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{os.getpid()}.jsonl")
        root.title("ทดสอบพิมพ์ — ปุ่มใหญ่")
        root.geometry("850x690")
        root.option_add("*Font", ("Tahoma", 20))
        root.configure(bg="white")
        tk.Label(root, text="ทดสอบพิมพ์ใน Notepad", font=("Tahoma", 26, "bold"), bg="white").pack(pady=15)
        tk.Label(root, text="ระบบจะเติมข้อความ:  TEST-456\nใช้เฉพาะเอกสาร Untitled สำหรับทดสอบ", bg="white").pack(pady=6)
        self.find_button = tk.Button(root, text="1. ค้นหา Notepad ทดสอบ", command=self.find, height=2)
        self.find_button.pack(fill="x", padx=24, pady=8)
        self.listbox = tk.Listbox(root, height=2, exportselection=False)
        self.listbox.pack(fill="x", padx=24)
        self.run_button = tk.Button(root, text="2. อนุมัติให้พิมพ์ TEST-456", command=self.start, state="disabled", height=2)
        self.run_button.pack(fill="x", padx=24, pady=10)
        self.status = tk.StringVar(value="เปิด Notepad เอกสารใหม่ไว้ แล้วกดปุ่ม 1\nไม่ต้องพิมพ์รหัสหรือคำสั่งใด ๆ")
        tk.Label(root, textvariable=self.status, wraplength=790, bg="white", fg="#12386c", justify="left").pack(padx=24, pady=10)
        tk.Button(root, text="หยุด / ยกเลิก", command=self.stop, fg="#a00000", height=2).pack(fill="x", padx=24, pady=8)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<F5>", lambda _: self.find() if self.session is None else None)
        root.bind("<F6>", lambda _: self.start() if self.backend is not None and self.session is None else None)
        root.bind("<Escape>", lambda _: self.stop())

    def audit(self, record):
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(record, at=datetime.now(timezone.utc).isoformat())) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def release(self):
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if self.session:
            self.session.close()
            self.session = None
        elif self.backend:
            self.backend.close()
        self.backend = None
        self.token = None

    def failure(self, exc):
        reason = str(exc) if isinstance(exc, InputDenied) else type(exc).__name__
        try:
            self.audit({"event": "easy_test_error", "reason": reason})
        except OSError:
            pass
        self.release()
        self.status.set("หยุดแล้ว ยังไม่ผ่านการทดสอบ\nสาเหตุ: " + reason + "\nส่งภาพหน้านี้ให้ Codex ตรวจได้เลย")
        self.root.title("ทดสอบพิมพ์ — หยุด: " + reason)
        self.find_button.configure(state="normal")
        self.run_button.configure(state="disabled")

    def find(self):
        try:
            self.release()
            if stop_requested():
                raise InputDenied("paused_or_stop_file_present")
            from execution.win32_input import Win32InputBackend
            self.backend = Win32InputBackend(0)
            self.candidates = test_documents(self.backend.list_allowed_windows(ALLOWED_IMAGE))
            if not self.candidates:
                self.run_button.configure(state="disabled")
                self.find_button.configure(state="disabled")
                self.status.set("กำลังเปิด Notepad ทดสอบให้ใหม่\nไม่ต้องเปิดหน้าต่างเอง รอสักครู่ครับ")
                # Only the fixed, permitted executable; no shell and no file arguments.
                self.created_notepad = subprocess.Popen([ALLOWED_IMAGE])
                self.open_deadline = time.monotonic() + 10
                self.after_id = self.root.after(250, self.refresh_created_notepad)
                return
            self.show_candidates()
        except Exception as exc:
            self.failure(exc)

    def show_candidates(self):
        self.listbox.delete(0, "end")
        for item in self.candidates:
            self.listbox.insert("end", f"{item['title']}  (PID {item['pid']})")
        self.listbox.selection_set(0)
        self.find_button.configure(state="normal")
        self.run_button.configure(state="normal")
        self.audit({"event": "test_candidates_ready", "count": len(self.candidates)})
        self.root.title("ทดสอบพิมพ์ — พร้อมทดสอบ (F6)")
        self.status.set("พบหน้าต่างทดสอบแล้ว\nกดปุ่ม 2 แล้วคลิกช่องข้อความใน Notepad\nอย่าขยับหรือปรับขนาดหน้าต่างระหว่างทดสอบ")

    def refresh_created_notepad(self):
        self.after_id = None
        try:
            if stop_requested():
                raise InputDenied("paused_while_opening_test_document")
            self.candidates = [w for w in test_documents(self.backend.list_allowed_windows(ALLOWED_IMAGE))
                               if w["pid"] == self.created_notepad.pid]
            if self.candidates:
                self.show_candidates()
            elif time.monotonic() >= self.open_deadline:
                raise InputDenied("new_Notepad_window_unavailable")
            else:
                self.after_id = self.root.after(250, self.refresh_created_notepad)
        except Exception as exc:
            self.failure(exc)

    def start(self):
        try:
            selection = self.listbox.curselection()
            if not selection or self.backend is None:
                raise InputDenied("select_test_window_first")
            selected = self.candidates[selection[0]]
            self.backend.hwnd = selected["hwnd"]
            self.session = GuardedDesktopSession(self.backend, enabled=True, allowed_images=[ALLOWED_IMAGE],
                                                  stop_requested=stop_requested, audit=self.audit)
            target = self.session.bind()
            if not same_target(target, selected):
                raise InputDenied("selected_window_changed")
            # The clearly labelled button is the per-action human approval.
            # A fresh, private one-use token is created only now, after reading time.
            self.token, _ = self.session.prepare(TEST_ACTION)
            self.audit({"event": "operator_approved", "kind": "type"})
            self.find_button.configure(state="disabled")
            self.run_button.configure(state="disabled")
            self.status.set("คลิกช่องข้อความใน Notepad ตอนนี้\nแล้วปล่อยเมาส์ ระบบจะเติม TEST-456 ให้\nไม่ต้องพิมพ์เอง (รอได้ 30 วินาที)")
            self.root.title("ทดสอบพิมพ์ — รอ Notepad เป้าหมาย")
            self.after_id = self.root.after(100, self.poll)
        except Exception as exc:
            self.failure(exc)

    def poll(self):
        self.after_id = None
        try:
            self.session._check_stop()
            if self.session.clock() >= self.session.pending[2]:
                raise InputDenied("approval_expired_no_input_sent")
            if not self.backend.target_ready():
                self.after_id = self.root.after(100, self.poll)
                return
            self.session.execute(self.token)
            # Allow the target app to process its input queue; never repeat input.
            time.sleep(.2)
            verification = verify_test_text(self.backend, self.session.target)
            self.audit(verification)
            if not verification["expected_suffix_matches"]:
                raise InputDenied("test_readback_mismatch")
            self.release()
            self.status.set("ผ่าน: อ่านข้อความ TEST-456 กลับได้จริง\nบันทึกผลตรวจแล้ว ไม่ต้องส่งภาพเพิ่มเติม")
            self.root.title("ทดสอบพิมพ์ — ผ่านการอ่านผลจริง")
            self.find_button.configure(state="normal")
        except Exception as exc:
            self.failure(exc)

    def stop(self):
        self.audit({"event": "test_cancelled", "pending_action": self.token is not None})
        self.release()
        self.run_button.configure(state="disabled")
        self.find_button.configure(state="normal")
        self.status.set("หยุดแล้ว ไม่มีการส่งคำสั่งเพิ่ม")

    def close(self):
        self.release()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    EasyTest(root)
    root.mainloop()
