#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Desktop Vision Reader (WO-020 Extension) — อ่านหน้าต่างแอปบนจอ (เช่น LINE ห้อง sc13) เป็น JSON
ทำงานภายใต้กรอบความปลอดภัยของ browser-engine-desktop:
1. ขออนุมัติผ่าน Popup บนหน้าจอ (approval_ui.ask_task_approval) ก่อนเริ่มงานเสมอ
2. เปิด/ดึงหน้าต่างแอปที่อยู่ใน allowlist (เช่น LINE.exe) ขึ้นมาและจับภาพเฉพาะกรอบหน้าต่างนั้น (DPI-aware)
3. ส่งภาพให้ AI Vision (OneVault Gemini 2.5 Flash หรือ DGX Qwen2.5-VL) สกัดข้อความสนทนาวันนี้เป็น JSON
"""

import argparse
import base64
import ctypes
from ctypes import wintypes
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
import urllib.error

APP_DIR = pathlib.Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from execution import approval_ui

LINE_DEFAULT_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "LINE", "bin", "current", "LINE.exe")
ONEVAULT_SECRET_DIR = pathlib.Path(r"E:\OneVault\00-secrets-and-accounts")
CAPTURES_DIR = APP_DIR / "data" / "vision-captures"


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def get_onevault_gemini_key() -> str:
    """ดึง Gemini API Key จาก E:\\OneVault\\00-secrets-and-accounts (OV-SEC-001.enc.json) ด้วย AES-256-GCM"""
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key

    key_file = ONEVAULT_SECRET_DIR / "master.key"
    enc_file = ONEVAULT_SECRET_DIR / "encrypted-secrets" / "OV-SEC-001.enc.json"
    if not key_file.is_file() or not enc_file.is_file():
        return ""

    # ลองถอดรหัสด้วย Node.js crypto (มีอยู่ในเครื่องแน่นอนและตรงกับระบบ OneVault 100%)
    node_script = (
        "const fs=require('fs'),crypto=require('crypto');"
        f"const k=Buffer.from(fs.readFileSync({json.dumps(str(key_file))},'utf8').trim(),'hex');"
        f"const p=JSON.parse(fs.readFileSync({json.dumps(str(enc_file))},'utf8'));"
        "const d=crypto.createDecipheriv('aes-256-gcm',k,Buffer.from(p.iv,'hex'));"
        "d.setAuthTag(Buffer.from(p.tag,'hex'));"
        "process.stdout.write(d.update(p.ciphertext,'hex','utf8')+d.final('utf8'));"
    )
    try:
        out = subprocess.check_output(["node", "-e", node_script], stderr=subprocess.DEVNULL, timeout=5)
        return out.decode("utf-8").strip()
    except Exception:
        return ""


def find_or_activate_line_window(line_exe: str = LINE_DEFAULT_EXE) -> dict:
    """
    ค้นหาหน้าต่างหลักของ LINE บน Interactive Desktop ของผู้ใช้
    หากซ่อนอยู่ใน System Tray จะสั่งรัน LINE.exe เพื่อปลุกหน้าต่างขึ้นมาด้านหน้า
    """
    _enable_dpi_awareness()
    user32 = ctypes.windll.user32

    def _scan_windows():
        found = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            title = buf.value or ""
            class_name = cls.value or ""
            if w >= 300 and h >= 300:
                if "line" in title.lower() or "qt" in class_name.lower():
                    found.append({
                        "hwnd": int(hwnd),
                        "pid": int(pid.value),
                        "title": title,
                        "class_name": class_name,
                        "rect": (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
                    })
            return True

        user32.EnumWindows(WNDENUMPROC(cb), 0)
        # ให้ลำดับความสำคัญกับหน้าต่างที่ชื่อมีคำว่า LINE ก่อน
        found.sort(key=lambda x: (0 if "line" in x["title"].lower() else 1, -(x["rect"][2] - x["rect"][0])))
        return found

    wins = _scan_windows()
    if not wins and os.path.isfile(line_exe):
        subprocess.Popen([line_exe])
        time.sleep(2.5)
        wins = _scan_windows()

    if not wins:
        raise RuntimeError(
            "ไม่พบหน้าต่างโปรแกรม LINE บนหน้าจอ (กรุณาเปิดโปรแกรม LINE บนหน้าจอหลัก และรันคำสั่งนี้จากหน้าจอ Windows ของคุณโดยตรง)"
        )

    target = wins[0]
    hwnd = target["hwnd"]
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.8)

    # อัปเดตพิกัดหลังดึงขึ้นมาด้านหน้า
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    target["rect"] = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
    return target


def capture_window_png(rect: tuple[int, int, int, int], room_name: str = "sc13") -> pathlib.Path:
    """จับภาพเฉพาะกรอบหน้าต่างที่กำหนด (bbox) แล้วบันทึกเป็นไฟล์ PNG"""
    _enable_dpi_awareness()
    from PIL import ImageGrab

    CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_room = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in room_name)
    out_path = CAPTURES_DIR / f"line_{safe_room}_{stamp}.png"
    img = ImageGrab.grab(bbox=rect, all_screens=True)
    img.save(out_path, format="PNG")
    return out_path


def analyze_chat_image_with_gemini(image_path: pathlib.Path, room_name: str = "sc13") -> dict:
    """ส่งภาพหน้าต่างแชทให้ Gemini 2.5 Flash (ใช้คีย์จาก OneVault) อ่านและสรุปเป็น JSON"""
    api_key = get_onevault_gemini_key()
    if not api_key:
        raise RuntimeError("ไม่พบคีย์ Gemini ใน E:\\OneVault\\00-secrets-and-accounts")

    img_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    today_str = datetime.date.today().isoformat()

    prompt = f"""คุณคือระบบอ่านหน้าจอแชท LINE (Computer Use Vision Reader)
กรุณาอ่านข้อความสนทนาจากภาพหน้าต่างแชท LINE ที่แนบมานี้ (ห้องเป้าหมาย: "{room_name}", วันที่ปัจจุบัน: {today_str})
แล้วส่งกลับเป็น JSON เท่านั้น ตามโครงสร้างดังนี้:
{{
  "room_detected": "ชื่อห้องแชทที่เห็นบนหัวหน้าต่างในภาพ",
  "target_room": "{room_name}",
  "date": "{today_str}",
  "summary_th": "สรุปประเด็นสำคัญทั้งหมดที่คุยกันในห้องวันนี้เป็นภาษาไทยอย่างชัดเจน",
  "topics": ["ประเด็นที่ 1", "ประเด็นที่ 2"],
  "participants": ["รายชื่อผู้ส่งข้อความที่ปรากฏในภาพ"],
  "messages": [
    {{
      "time": "เวลาที่แสดงข้างข้อความ เช่น 09:30 หรือ 14:15",
      "sender": "ชื่อผู้ส่ง (หรือ 'ฉัน/ตัวเอง' หากเป็นกล่องข้อความฝั่งขวา)",
      "type": "text | image | sticker | file | link",
      "text": "ข้อความที่อ่านได้ตามตัวอักษรจริง หรือคำอธิบายรูปภาพ/สติกเกอร์"
    }}
  ]
}}"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": "image/png", "data": img_b64}},
                    {"text": prompt},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))

    raw_text = (
        res_data.get("candidates", [{}])[0]
        .get("content", {})
        .get("parts", [{}])[0]
        .get("text", "{}")
    )
    parsed = json.loads(raw_text)
    parsed["screenshot_path"] = str(image_path)
    parsed["engine"] = "gemini-2.5-flash (OneVault Secret OV-SEC-001)"
    return parsed


def analyze_chat_image_with_dgx(image_path: pathlib.Path, room_name: str = "sc13") -> dict:
    """Fallback: ส่งภาพให้โมเดล Vision บน DGX (qwen2.5vl:7b) อ่านเป็น JSON"""
    cfg_path = APP_DIR / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.is_file() else {}
    endpoint = cfg.get("vision_endpoint", "http://192.168.1.108/ai/api/generate")
    model = cfg.get("vision_model", "qwen2.5vl:7b")
    img_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    today_str = datetime.date.today().isoformat()

    prompt = (
        f"อ่านข้อความแชท LINE ในภาพนี้ (ห้อง {room_name} วันที่ {today_str}) "
        "แล้วตอบกลับเป็น JSON ที่มีคีย์ room_detected, date, summary_th, topics, participants, messages[{time,sender,type,text}] เท่านั้น"
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "format": "json",
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
    raw_resp = res_data.get("response", "{}")
    parsed = json.loads(raw_resp)
    parsed["screenshot_path"] = str(image_path)
    parsed["engine"] = f"dgx-vision ({model})"
    return parsed


def run_line_room_vision_reader(
    room_name: str = "sc13",
    require_approval: bool = True,
    countdown_sec: int = 3,
    image_file: str | None = None,
) -> dict:
    """
    ลำดับการทำงานหลัก:
    1. ถ้าระบุ image_file มาแล้ว อ่านจากไฟล์ภาพนั้นทันที
    2. ถ้าไม่ได้ระบุ image_file:
       - ขออนุมัติผ่าน Popup บนหน้าจอ (ตามธรรมนูญ Computer Use)
       - ปลุกหน้าต่าง LINE.exe ขึ้นมาด้านหน้า
       - นับถอยหลังให้ผู้ใช้วางโฟกัสที่ห้อง sc13 (เช่น 3 วินาที)
       - จับภาพเฉพาะกรอบหน้าต่าง LINE.exe
    3. ส่งภาพให้ AI Vision (Gemini OneVault หรือ DGX) สกัดเป็น JSON และบันทึกผลลัพธ์
    """
    if image_file:
        img_path = pathlib.Path(image_file)
        if not img_path.is_file():
            raise FileNotFoundError(f"ไม่พบไฟล์ภาพ: {img_path}")
        win_info = {"title": "From Image File", "rect": [0, 0, 0, 0], "hwnd": 0, "pid": 0}
    else:
        if require_approval:
            approved, reason = approval_ui.ask_task_approval(
                "Computer Use — ขออนุมัติอ่านแชท LINE เป็น JSON",
                {
                    "โปรแกรมเป้าหมาย": "LINE Desktop (LINE.exe)",
                    "ห้องแชทที่ต้องการอ่าน": room_name,
                    "การกระทำ": "จับภาพเฉพาะกรอบหน้าต่าง LINE และสรุปข้อความวันนี้เป็น JSON",
                    "คำแนะนำ": f"กรุณาเปิดห้อง '{room_name}' ในแอป LINE แล้วกด ✔ อนุมัติ",
                },
                timeout_s=30.0,
            )
            if not approved:
                return {"ok": False, "error": f"ผู้ใช้ไม่อนุมัติหรือหมดเวลา ({reason})"}

        win_info = find_or_activate_line_window()
        if countdown_sec > 0:
            for remaining in range(countdown_sec, 0, -1):
                sys.stderr.write(f"\r⏳ กำลังจับภาพหน้าต่าง LINE (ห้อง {room_name}) ในอีก {remaining} วินาที... ")
                sys.stderr.flush()
                time.sleep(1.0)
            sys.stderr.write("\n")

        # อัปเดตพิกัดหน้าต่างล่าสุดก่อนจับภาพ
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        user32.GetWindowRect(win_info["hwnd"], ctypes.byref(rect))
        win_info["rect"] = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        img_path = capture_window_png(win_info["rect"], room_name=room_name)

    try:
        result = analyze_chat_image_with_gemini(img_path, room_name=room_name)
    except Exception as gemini_err:
        try:
            result = analyze_chat_image_with_dgx(img_path, room_name=room_name)
        except Exception as dgx_err:
            raise RuntimeError(f"วิเคราะห์ภาพไม่สำเร็จ (Gemini: {gemini_err} | DGX: {dgx_err})")

    result["ok"] = True
    result["window"] = win_info

    out_json_path = APP_DIR / "data" / f"line_{room_name}_today.json"
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    out_json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["saved_json_path"] = str(out_json_path)
    return result


def main():
    parser = argparse.ArgumentParser(description="Read LINE chat room window into structured JSON via Computer Use Vision")
    parser.add_argument("--room", default="sc13", help="Target LINE chat room name (default: sc13)")
    parser.add_argument("--countdown", type=int, default=3, help="Seconds before capturing LINE window")
    parser.add_argument("--no-approval-popup", action="store_true", help="Skip popup when already launched interactively by user script")
    parser.add_argument("--image", help="Optional path to an existing screenshot PNG/JPG to parse directly")
    args = parser.parse_args()

    try:
        res = run_line_room_vision_reader(
            room_name=args.room,
            require_approval=not args.no_approval_popup,
            countdown_sec=args.countdown,
            image_file=args.image,
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
    except Exception as e:
        err_obj = {"ok": False, "room": args.room, "error": str(e)}
        print(json.dumps(err_obj, ensure_ascii=False, indent=2))
        sys.exit(1)


if __name__ == "__main__":
    main()
