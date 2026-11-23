"""Approval popup รายงาน (one-click per task) — สำหรับโหมดอิสระภายในกรอบ.

หลักการ:
- popup ขึ้น FOREGROUND พร้อมปุ่ม อนุมัติ/ปฏิเสธ + รายละเอียดงาน (เป้า/จำนวนคำสั่งสูงสุด/กรอบหน้าต่าง)
- ค่าเริ่มต้น = ปฏิเสธ (timeout 20 วิ = ไม่อนุมัติ) — ผู้ใช้ไม่กด งานไม่เกิด
- topmost ชั่วคราว: ผู้ใช้กด Alt+Tab/หนาวเสือกได้เสมอ — โปรแกรมไม่ยัดหน้า
- คืนค่า approved=True/False — ใครก็อ่านได้ ไม่มี token ลับ (เชื่อว่าคนที่นั่งหน้าจอเป็นเจ้าของ)
"""
import sys
import threading
import time

REASON_TIMEOUT = "approval_timeout_default_deny"


def ask_task_approval(title, details, timeout_s=20.0):
    """บล็อกจนกว่าผู้ใช้กดหรือ timeout — คืน (approved: bool, reason: str)."""
    try:
        import tkinter as tk
        from tkinter import font as tkfont
    except Exception:
        return False, "tkinter_unavailable_default_deny"

    result = {"approved": None}

    def on_approve():
        result["approved"] = True
        root.quit()

    def on_deny():
        result["approved"] = False
        root.quit()

    root = tk.Tk()
    root.title("อนุมัติงาน — Computer Use")
    root.attributes("-topmost", True)
    root.configure(bg="#0f172a")
    w, h = 460, 340
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//3}")
    pad = {"padx": 18, "pady": 8, "sticky": "w"}
    tk.Label(root, text="⬢ Computer Use — ขออนุมัติทำงาน", font=("Tahoma", 13, "bold"),
             fg="#38bdf8", bg="#0f172a").grid(row=0, column=0, columnspan=2, **pad)
    rows = list(details.items())[:9]
    for i, (k, v) in enumerate(rows, start=1):
        tk.Label(root, text=str(k), font=("Tahoma", 10), fg="#94a3b8", bg="#0f172a").grid(row=i, column=0, **pad)
        tk.Label(root, text=str(v)[:70], font=("Tahoma", 10), fg="#f8fafc", bg="#0f172a", wraplength=290, justify="left").grid(row=i, column=1, **pad)
    brow = len(rows) + 2
    btn_font = ("Tahoma", 12, "bold")
    tk.Button(root, text="✔ อนุมัติ", command=on_approve, font=btn_font, bg="#22c55e", fg="#04120a",
              activebackground="#16a34a", relief="flat", padx=22, pady=8).grid(row=brow, column=0, pady=14, padx=18)
    tk.Button(root, text="✘ ปฏิเสธ", command=on_deny, font=btn_font, bg="#ef4444", fg="#1f0808",
              activebackground="#dc2626", relief="flat", padx=22, pady=8).grid(row=brow, column=1, pady=14, padx=18, sticky="e")
    cd = tk.Label(root, text=f"ไม่ตอบ = ปฏิเสธอัตโนมัติใน {timeout_s:.0f} วิ", font=("Tahoma", 9), fg="#facc15", bg="#0f172a")
    cd.grid(row=brow + 1, column=0, columnspan=2)

    t0 = time.monotonic()

    def countdown():
        # ทุกการแตะ tkinter อยู่ในเธรดเดียวกัน (apartment เดียว) ผ่าน root.after
        remain = timeout_s - (time.monotonic() - t0)
        if result["approved"] is not None:
            return
        if remain <= 0:
            result["approved"] = False
            root.quit()
            return
        cd.config(text=f"ไม่ตอบ = ปฏิเสธอัตโนมัติใน {remain:.0f} วิ")
        root.after(500, countdown)

    root.after(200, countdown)
    root.mainloop()  # บล็อกเธรดผู้เรียกจนกดปุ่ม/timeout — single-threaded Tcl ถูกต้อง
    try:
        root.destroy()
    except Exception:
        pass
    approved = result["approved"] is True
    return approved, ("approved" if approved else (REASON_TIMEOUT if result["approved"] is None else "user_denied"))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(ask_task_approval("ทดสอบ", {"เป้าหมาย": "ไม่พบ", "จำนวนคำสั่ง": 5}, timeout_s=20))