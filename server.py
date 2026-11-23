"""Local Server & Web UI for OneVault Interactive Agent Engine.
Hosts the real-time control dashboard at http://127.0.0.1:8205 with telemetry & Human Takeover.
Enforces local-only binding (127.0.0.1) and token-based authentication (X-Desktop-Token).
"""
import hmac
import json
import os
import pathlib
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = pathlib.Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from typing import Any
    from desktop_engine import run_interactive_task, takeover_manager, CFG
    from models.client import get_vllm_key
    from policy.approval import approval_manager
    from playwright.sync_api import sync_playwright
except (ImportError, ModuleNotFoundError) as _err:
    pkg = getattr(_err, "name", str(_err))
    msg = (
        "====================================================================\n"
        f"[ข้อผิดพลาด] ไม่พบแพ็กเกจที่จำเป็นสำหรับการทำงาน ({pkg})\n"
        "กรุณาเปิดโปรแกรมผ่าน run.cmd หรือ run.ps1 เพื่อให้ระบบเตรียมและใช้สภาพแวดล้อม (.venv) อัตโนมัติ\n"
        "หรือเปิดใช้งานผ่านคำสั่ง: .\\.venv\\Scripts\\python.exe server.py\n"
        "====================================================================\n"
    )
    try:
        sys.stderr.write(msg)
        sys.stderr.flush()
    except Exception:
        try:
            sys.stderr.buffer.write(msg.encode("utf-8"))
            sys.stderr.buffer.flush()
        except Exception:
            pass
    sys.exit(1)

STATIC_DIR = BASE_DIR / "static"
PORT = int(CFG.get("port", 8205))

# Requirement A1: Token directory and storage (%USERPROFILE%\.browser-engine-desktop\token)
TOKEN_DIR = pathlib.Path.home() / ".browser-engine-desktop"
TOKEN_FILE = TOKEN_DIR / "token"


def _enforce_token_permissions():
    try:
        if sys.platform == "win32":
            user = os.environ.get("USERNAME")
            if user and TOKEN_FILE.is_file():
                subprocess.run(["icacls", str(TOKEN_FILE), "/inheritance:r", "/grant:r", f"{user}:(R,W)"],
                               check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            if TOKEN_FILE.is_file():
                os.chmod(TOKEN_FILE, 0o600)
    except Exception:
        pass


def get_or_create_token() -> str:
    """Read existing token or generate a cryptographically strong 32-byte hex token."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.is_file():
        try:
            tok = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if tok:
                _enforce_token_permissions()
                return tok
        except Exception:
            pass

    tok = secrets.token_hex(32)
    TOKEN_FILE.write_text(tok, encoding="utf-8")
    _enforce_token_permissions()
    return tok


EXPECTED_TOKEN = get_or_create_token()

from service.reliability import TaskReservation, TaskStore, strict_body, TaskSlotPool, TaskQueue

TASKS = {}
ACTIVE_TASK_ID = None
LOGIN_IN_PROGRESS = False
RESERVATION = TaskReservation()
STORE = TaskStore(tasks=TASKS)
# WO-019 P1: คู่ขนานขั้น 1 — ธง task_slots (เริ่มต้น 1 = พฤติกรรมเดิม 100%): >1 = สล็อตคู่ขนาน + คิวรอ
TASK_SLOTS = max(1, int(CFG.get("task_slots", 1)))
SLOTS = TaskSlotPool(slots=TASK_SLOTS) if TASK_SLOTS > 1 else None
QUEUE = TaskQueue(cap=int(CFG.get("task_queue_cap", 20))) if TASK_SLOTS > 1 else None
if SLOTS is not None:
    SLOTS.release_all()  # boot เจ้าของเครื่อง — ล็อกซากรอบตายไม่มีสิทธิ์ถือต่อ


def open_browser_for_login(target_url: str, profile_name: str = "default_user"):
    global LOGIN_IN_PROGRESS
    LOGIN_IN_PROGRESS = True
    safe_profile = "".join(c for c in profile_name if c.isalnum() or c in ("-", "_")).strip() or "default_user"
    user_data_dir = BASE_DIR / "data" / "profiles" / safe_profile
    user_data_dir.mkdir(parents=True, exist_ok=True)
    channel = CFG.get("browser_channel", "chrome")

    print(f"[*] Opening Chrome for login at {target_url} (profile: {safe_profile}) ...")
    with sync_playwright() as p:
        # Requirement A2: Standard clean browser launch without hiding automation flags
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            channel=channel,
            headless=False,
            args=["--no-sandbox"]
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(target_url)
        print("[*] Browser is open. Please log in or complete setup, then close the browser window when finished.")
        page.wait_for_event("close", timeout=0)
        ctx.close()
    LOGIN_IN_PROGRESS = False
    print(f"[*] Chrome closed. Login session saved to local profile: {safe_profile}")


def run_task_worker(task_id: str, url: str, goal: str, max_steps: int, profile: str = None):
    global ACTIVE_TASK_ID
    ACTIVE_TASK_ID = task_id

    def on_notify(evt: str, data: Any):
        if task_id in TASKS:
            TASKS[task_id]["events"].append({"time": time.time(), "event": evt, "data": data})
            if evt == "step":
                TASKS[task_id]["steps"].append(data)
            elif evt == "finished":
                TASKS[task_id]["status"] = data.get("status")
                TASKS[task_id]["telemetry"] = data.get("telemetry", {})
                STORE.snapshot(TASKS[task_id])

    TASKS[task_id] = {"id": task_id, "url": url, "goal": goal, "profile": profile,
                      "status": "running", "steps": [], "events": [],
                      "telemetry": {}, "created_at": time.time()}
    STORE.snapshot(TASKS[task_id])

    try:
        res = run_interactive_task(
            url=url,
            goal=goal,
            max_steps=max_steps,
            on_step=on_notify,
            profile=profile,
            task_id=task_id
        )
        TASKS[task_id].update(res)
    except Exception as e:
        TASKS[task_id]["status"] = "error"
        TASKS[task_id]["error"] = str(e)
    finally:
        STORE.snapshot(TASKS[task_id])
        RESERVATION.release()
        if ACTIVE_TASK_ID == task_id:
            ACTIVE_TASK_ID = None
        # WO-019 P1: คืนสล็อตของงานตัวเอง (อ่านจากไฟล์ — คืนได้แม้ข้าม worker) แล้วปลุกคิว
        if SLOTS is not None:
            SLOTS.release(task_id)
        try:
            _queue_drain_if_ready()
        except Exception:
            pass


_DRAIN_LOCK = threading.Lock()


def _start_worker(task_id: str, url: str, goal: str, max_steps: int, profile, kind: str = "browser"):
    t = threading.Thread(
        target=run_task_worker,
        args=(task_id, url, goal, max_steps, profile),
        daemon=True
    )
    t.start()


def _acquire_or_queue(task_id: str, url: str, goal: str, max_steps: int, profile,
                      kind: str = "browser"):
    """P1: จองสล็อตหรือเข้าคิว — คืน (response_dict, http_code). ธง task_slots=1 ไม่เรียกฟังก์ชันนี้."""
    slot = SLOTS.acquire(task_id, kind)
    if slot is not None:
        TASKS[task_id] = {"id": task_id, "url": url, "goal": goal, "profile": profile, "kind": kind,
                          "status": "running", "steps": [], "events": [],
                          "telemetry": {}, "created_at": time.time()}
        _start_worker(task_id, url, goal, max_steps, profile, kind)
        return {"task_id": task_id, "status": "queued", "slot": slot}, 200
    pos = QUEUE.enqueue({"task_id": task_id, "kind": kind, "url": url, "goal": goal,
                         "max_steps": max_steps, "profile": profile, "queued_at": time.time()})
    if pos is None:
        return {"error": "คิวงานเต็ม (queue_full) — รองานเดิมจบก่อน", "queue_cap": QUEUE.cap}, 409
    TASKS[task_id] = {"id": task_id, "url": url, "goal": goal, "profile": profile, "kind": kind,
                      "status": "queued", "queue_position": pos, "steps": [], "events": [],
                      "telemetry": {}, "created_at": time.time()}
    STORE.snapshot(TASKS[task_id])
    return {"task_id": task_id, "status": "queued", "queue_position": pos}, 200


def _queue_drain_if_ready():
    """ปลุกงานหัวคิวเมื่อสล็อตว่าง — เรียกจาก finally ของ worker ทุกตัว + หลังจองสำเร็จ (กันคิวตายเงียบ)."""
    if not QUEUE:
        return
    with _DRAIN_LOCK:
        head = QUEUE.peek()
        if not head:
            return
        kind = head.get("kind") or "browser"
        slot = SLOTS.acquire(head["task_id"], kind)
        if slot is None:
            return  # เครื่องไม่ว่างพอ (desktop ถือทั้งชุด / สล็อตเต็ม) — ปลุกคราวหน้า
        item = QUEUE.pop()
        tid = item["task_id"]
        TASKS[tid] = {"id": tid, "url": item.get("url"), "goal": item.get("goal"),
                      "profile": item.get("profile"), "kind": kind, "status": "running",
                      "steps": [], "events": [], "telemetry": {},
                      "created_at": item.get("queued_at") or time.time()}
        _start_worker(tid, item.get("url"), item.get("goal"),
                      item.get("max_steps") or 3, item.get("profile"), kind)


class InteractiveHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_authenticated(self) -> bool:
        """Check X-Desktop-Token header using constant-time comparison."""
        token_hdr = self.headers.get("X-Desktop-Token", "").strip()
        if not token_hdr:
            return False
        ok = hmac.compare_digest(token_hdr, EXPECTED_TOKEN)
        # WO-018 step3: รับ token ผู้ใช้ที่ลงทะเบียนด้วย (operator/viewer) เมื่อธงเปิด
        if not ok and token_hdr:
            try:
                from service import roles
                if roles.enabled() and roles.resolve_role(token_hdr):
                    return True
            except Exception:
                pass
        return ok

    # ---------- WO-018 step3: สิทธิ์ 3 ระดับ (roles_enabled) ----------
    def _resolve_role(self) -> str:
        token_hdr = self.headers.get("X-Desktop-Token", "").strip()
        try:
            from service import roles
            if not roles.enabled():
                return "admin"  # ธงปิด = พฤติกรรมเดิม: token เดียวทำได้ทุกอย่าง
            return roles.resolve_role(token_hdr)
        except Exception:
            return "admin"

    def _role_gate_post(self) -> bool:
        """คืน True=ผ่าน · False=ส่ง 403 เองแล้ว. ธงปิด = ผ่านเสมอ (พฤติกรรมเดิม 100%)."""
        try:
            from service import roles as _roles
            if not _roles.enabled():
                return True
        except Exception:
            return True
        role = self._resolve_role()
        need = _roles.min_role_for(self.path.split("?")[0])
        if _roles.role_at_least(role, need):
            self._current_role = role
            return True
        self._send_json({"error": "สิทธิ์ไม่พอ", "role": role or None,
                         "required_any_of": need, "path": self.path}, 403)
        return False

    def do_GET(self):
        # Static dashboard UI (can be loaded directly in local browser)
        if self.path in ("/", "/index.html"):
            p = STATIC_DIR / "index.html"
            if p.is_file():
                content = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_error(404)
            return

        # ห้องทดลองสั่งงาน (lab) — static like index; /api/* still token-gated
        if self.path in ("/lab", "/lab.html"):
            p = STATIC_DIR / "lab.html"
            if p.is_file():
                content = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_error(404)
            return

        # คู่มือใช้งาน (Help) — อ่านได้ไม่ต้อง token (เป็นเอกสาร ไม่มีข้อมูลงาน) — ปุ่ม Help ใน /lab ชี้มาที่นี่
        if self.path in ("/help", "/help.html"):
            p = STATIC_DIR / "help.html"
            if p.is_file():
                content = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_error(404)
            return

        # WO-019 C1: หน้าสิทธิ์/สมาชิก (UI-04) + ตรวจย้อน (UI-05) — ต้องมี token ผ่านเสมอ (ใช้ /api/* ทุกเรียก)
        if self.path in ("/members", "/members.html", "/audit", "/audit.html"):
            fname = "members.html" if self.path.startswith("/members") else "audit.html"
            p = STATIC_DIR / fname
            if p.is_file():
                content = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(content)
                return
            self.send_error(404)
            return

        # Requirement A1: Every /api/* endpoint requires valid X-Desktop-Token
        if self.path.startswith("/api/"):
            if not self._is_authenticated():
                self._send_json({"error": "Unauthorized: Invalid or missing X-Desktop-Token"}, 401)
                return

        if self.path == "/api/status":
            vllm_ok = False
            vllm_msg = "พร้อมใช้งาน (vLLM online)"
            try:
                from models.client import planner_route
                _route = planner_route()  # T3: ตรวจทางเดียวกับที่ Planner ใช้จริง (gateway หรือ vLLM ตรง)
                k = _route["key"]
                url = _route["url"]
                req = urllib.request.Request(f"{url}/models", headers={"Authorization": f"Bearer {k}"})
                with urllib.request.urlopen(req, timeout=2) as r:
                    if r.status == 200:
                        vllm_ok = True
            except Exception as e:
                vllm_msg = f"ไม่สามารถเชื่อมต่อ DGX AI Brain ({CFG.get('vllm_url')}): ระบบทำงานในโหมดออฟไลน์/สำรอง"

            from telemetry.vault import get_vault_manager
            vault_stat = get_vault_manager().get_status()

            self._send_json({
                "status": "online",
                "vllm_ready": vllm_ok,
                "vllm_message": vllm_msg if not vllm_ok else "พร้อมใช้งาน (vLLM online)",
                "vllm_url": CFG.get("vllm_url"),
                "planner_route": "ai-host-gateway" if CFG.get("planner_gateway_url") else "direct",
                "model": CFG.get("model", "Qwen3.8-27B"),
                "channel": CFG.get("browser_channel", "chrome"),
                "active_task_id": ACTIVE_TASK_ID,
                "task_slots": TASK_SLOTS,
                "slots": SLOTS.snapshot() if SLOTS else None,
                "queue": QUEUE.snapshot() if QUEUE else None,
                "login_in_progress": LOGIN_IN_PROGRESS,
                "takeover_active": takeover_manager.takeover_active,
                "takeover_reason": takeover_manager.reason,
                "pending_approvals": len(approval_manager.get_pending()),
                "onevault": vault_stat
            })
            return

        # WO-019 B1a: สถานะกติกา (config guard) — อ่านได้ทุก role (viewer เห็น warn ได้เช่นกัน)
        if self.path == "/api/config-status":
            from service.config_guard import check_integrity
            self._send_json(check_integrity())
            return

        # WO-019 M2: บริบทธุรกิจราย-Node — อ่านได้ทุก role (GET — หลังจากเดิมมีแค่ POST add/remove)
        if self.path == "/api/memory":
            from service import task_memory as _tm
            self._send_json({"entries": _tm.list_all()})
            return

        # WO-019 C1 UI-04: ตารางสมาชิก+สิทธิ์ (ชื่อ+role เท่านั้น — ไม่มี token ในผลเด็ดขาด)
        if self.path == "/api/roles":
            from service import roles as _roles
            self._send_json({
                "roles_enabled": _roles.enabled(),
                "users": _roles.list_users(),
                "matrix": {"admin": "ทุกอย่าง", "operator": "สั่งงาน/อนุมัติ/หยุด/rollback — ห้าม shell",
                            "viewer": "อ่านอย่างเดียว"},
                "note": "ไม่มี token = 401 ทุก /api/* · POST ที่ไม่อยู่ตาราง = admin เท่านั้น (fail-closed)",
            })
            return

        # WO-019 C1 UI-05: รายการไฟล์ manifest ที่ตรวจย้อนได้ (shell + desktop input)
        if self.path.startswith("/api/audit/list"):
            out = {"shell": [], "desktop_input": [], "chain_sign_policy": None, "keysign_policy": None}
            try:
                from service import chain_sign as _ks
                out["chain_sign_policy"] = _ks.policy()
                out["keysign_policy"] = out["chain_sign_policy"]  # ชื่อเดิม (เลิกใช้)
                out["pubkey_head"] = _ks.pubkey_hex()[:16]
            except Exception:
                pass
            for key, sub in (("shell", "action-manifest"), ("desktop_input", "desktop-input")):
                d = BASE_DIR / "data" / sub
                if d.is_dir():
                    out[key] = sorted((f.name, f.stat().st_size) for f in d.glob("*.jsonl"))
            self._send_json(out)
            return

        # WO-019 C1 UI-05: อ่าน manifest + verify_chain 1 คลิก — file=ชื่อไฟล์ (กัน path traversal เด็ดขาด)
        if self.path.startswith("/api/audit/manifest"):
            q = urllib.parse.urlparse(self.path).query
            fname = (urllib.parse.parse_qs(q).get("file") or [""])[0]
            verify = (urllib.parse.parse_qs(q).get("verify") or ["0"])[0] == "1"
            if not fname or "/" in fname or "\\" in fname or ".." in fname or not fname.endswith(".jsonl"):
                self._send_json({"error": "ชื่อไฟล์ไม่ถูกต้อง"}, 400)
                return
            for sub in ("action-manifest", "desktop-input"):
                p = BASE_DIR / "data" / sub / fname
                if p.is_file():
                    break
            else:
                self._send_json({"error": "ไม่พบไฟล์"}, 404)
                return
            lines = p.read_text(encoding="utf-8").splitlines()
            entries = []
            for ln in lines[-200:]:
                if not ln.strip():
                    continue
                try:
                    e = json.loads(ln)
                    detail = e.get("detail") or {}
                    res = e.get("result") or {}
                    entries.append({
                        "seq": e.get("seq"), "timestamp": e.get("timestamp"),
                        "source": e.get("source"), "kind": e.get("kind"),
                        "cmd": str(detail.get("command") or detail.get("intent") or detail.get("action") or "")[:90],
                        "exit": res.get("exit_code"), "sandbox": (res.get("sandbox") or {}).get("changed", None),
                        "sig": str(e.get("signature") or "unsigned")[:24],
                    })
                except Exception:
                    continue
            v = None
            if verify:
                try:
                    from service import chain_sign as _ks
                    v = _ks.verify_chain(str(p))
                except Exception as e:
                    v = {"exists": True, "error": str(e)[:100]}
            self._send_json({"file": fname, "entries_shown": len(entries), "entries": entries, "verify": v})
            return

        if self.path == "/api/vault/status":
            from telemetry.vault import get_vault_manager
            self._send_json(get_vault_manager().get_status())
            return

        if self.path == "/api/approvals":
            self._send_json(approval_manager.get_pending())
            return

        if self.path == "/api/tasks":
            summary = [
                {
                    "id": t["id"],
                    "url": t.get("url", ""),
                    "goal": t.get("goal", ""),
                    "status": t.get("status"),
                    "steps": len(t["steps"]) if isinstance(t.get("steps"), list) else (t.get("steps_count") or 0),
                    "telemetry": t.get("telemetry", {})
                }
                for t in sorted(TASKS.values(), key=lambda x: x.get("created_at", 0), reverse=True)
            ]
            self._send_json(summary)
            return

        if self.path.startswith("/api/task/"):
            tid = self.path[len("/api/task/"):]
            if tid in TASKS:
                self._send_json(TASKS[tid])
                return
            self.send_error(404, "Task not found")
            return

        self.send_error(404)

    def do_POST(self):
        # Requirement A1: Every /api/* endpoint requires valid X-Desktop-Token
        if self.path.startswith("/api/"):
            if not self._is_authenticated():
                self._send_json({"error": "Unauthorized: Invalid or missing X-Desktop-Token"}, 401)
                return

        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            length = int(length)
        except (TypeError, ValueError):
            length = 0
        body_raw = self.rfile.read(length) if 0 < length <= 200000 else b"{}"
        body, body_err = strict_body(body_raw)
        if body_err is not None:
            self._send_json({"error": body_err}, 400)
            return

        # WO-018 step3: สิทธิ์ 3 ระดับ — คุมทุก POST (ธง roles_enabled ปิด = ผ่านเสมอ เหมือนเดิม)
        if not self._role_gate_post():
            return

        # Requirement A3: Action approval endpoint (/api/approve/<task>/<step>)
        if self.path.startswith("/api/approve/"):
            parts = self.path[len("/api/approve/"):].split("/")
            if len(parts) >= 2:
                task_id, step_str = parts[0], parts[1]
                try:
                    step_num = int(step_str)
                    from service import spada_keysign as _sk
                    try:
                        ok = approval_manager.approve(task_id, step_num)
                    except _sk.KeySignError as e:
                        # T2: ต้องมีลายเซ็นจาก SPADA KeySign — ไม่ผ่าน = ยังไม่อนุมัติ (คำขอยังรออยู่)
                        self._send_json({"error": "spada_keysign_required", "code": e.code}, 403)
                        return
                    if ok:
                        self._send_json({"status": "approved", "task_id": task_id, "step": step_num})
                    else:
                        self._send_json({"error": "Pending approval request not found or expired"}, 404)
                    return
                except ValueError:
                    self._send_json({"error": "Invalid step number"}, 400)
                    return

        if self.path.startswith("/api/reject/"):
            parts = self.path[len("/api/reject/"):].split("/")
            if len(parts) >= 2:
                task_id, step_str = parts[0], parts[1]
                try:
                    step_num = int(step_str)
                    ok = approval_manager.reject(task_id, step_num)
                    if ok:
                        self._send_json({"status": "rejected", "task_id": task_id, "step": step_num})
                    else:
                        self._send_json({"error": "Pending approval request not found or expired"}, 404)
                    return
                except ValueError:
                    self._send_json({"error": "Invalid step number"}, 400)
                    return

        if self.path == "/api/takeover":
            reason = body.get("reason", "User requested manual control")
            takeover_manager.trigger_takeover(reason)
            self._send_json({"status": "takeover_activated", "reason": reason})
            return

        if self.path == "/api/resume":
            takeover_manager.resume_agent()
            self._send_json({"status": "agent_resumed"})
            return

        if self.path == "/api/vault/sync":
            from telemetry.vault import get_vault_manager
            res = get_vault_manager().flush_spool(timeout=30.0)
            self._send_json({"status": "sync_complete", **res})
            return

        if self.path == "/api/pdpa/erase":
            task_id = body.get("task_id")
            reason = body.get("reason", "PDPA Right to Erasure request")
            from telemetry.retention import RetentionManager, is_safe_task_id
            if not task_id or not is_safe_task_id(task_id):
                self._send_json({
                    "ok": False,
                    "error": "Invalid or missing task_id. Must match ^[A-Za-z0-9._-]{1,128}$ with no path traversal or wildcards.",
                    "task_id": task_id
                }, 400)
                return
            res = RetentionManager.erase_task_data(task_id, reason=reason)
            if not res.get("ok"):
                self._send_json(res, 400)
                return
            self._send_json(res)
            return

        if self.path == "/api/retention/purge":
            days_raw = body.get("retention_days")
            confirm = body.get("confirm")
            
            if days_raw is None or isinstance(days_raw, bool):
                self._send_json({"error": "retention_days must be an integer between 0 and 3650"}, 400)
                return

            try:
                if isinstance(days_raw, (int, float)):
                    if int(days_raw) != days_raw:
                        raise ValueError()
                    days = int(days_raw)
                elif isinstance(days_raw, str) and (days_raw.isdigit() or (days_raw.startswith("-") and days_raw[1:].isdigit())):
                    days = int(days_raw)
                else:
                    raise ValueError()
            except (ValueError, TypeError):
                self._send_json({"error": "retention_days must be an integer between 0 and 3650", "received": days_raw}, 400)
                return

            if days < 0 or days > 3650:
                self._send_json({"error": "retention_days out of range [0, 3650]", "retention_days": days}, 400)
                return

            if days == 0 and confirm != "PURGE_ALL":
                self._send_json({
                    "error": "Confirmation required to purge all traces (retention_days=0). Must provide {'confirm': 'PURGE_ALL'}"
                }, 400)
                return

            from telemetry.retention import RetentionManager
            try:
                res = RetentionManager.purge_expired_traces(retention_days=days)
                self._send_json(res)
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
            return

        if self.path == "/api/open-browser":
            target_url = body.get("url", "https://shopee.co.th")
            profile_name = body.get("profile", "default_user")
            t = threading.Thread(target=open_browser_for_login, args=(target_url, profile_name), daemon=True)
            t.start()
            self._send_json({"status": "browser_opened", "url": target_url, "profile": profile_name})
            return

        if self.path == "/api/task":
            if TASK_SLOTS == 1:
                # เดิม 100% — ธง task_slots=1
                if ACTIVE_TASK_ID:
                    self._send_json({"error": "Another task is already running", "active_task_id": ACTIVE_TASK_ID}, 409)
                    return
                if LOGIN_IN_PROGRESS:
                    self._send_json({"error": "หน้าต่างเข้าสู่ระบบ Chrome กำลังเปิดอยู่ กรุณาล็อกอินให้เสร็จแล้วปิดหน้าต่างก่อนเริ่มงานครับ"}, 409)
                    return
            url = body.get("url", "").strip()
            goal = body.get("goal", "").strip()
            try:
                max_steps = int(body.get("max_steps", CFG.get("default_max_steps", 15)))
                if max_steps < 1 or max_steps > 50:
                    raise ValueError
            except (ValueError, TypeError):
                self._send_json({"error": "max_steps ต้องเป็นตัวเลข 1..50"}, 400)
                return
            profile = body.get("profile", None)

            if not url or not goal:
                self._send_json({"error": "url and goal are required"}, 400)
                return

            if TASK_SLOTS == 1:
                # Atomic reservation: จองสิทธิ์ก่อน — สองคำสั่งซ้อน = อันหลังได้ 409 แน่นอน (ปิดช่อง race)
                task_id = f"task_{int(time.time())}"
                if not RESERVATION.acquire(task_id):
                    self._send_json({"error": "Another task is already running (reservation)", "active_task_id": ACTIVE_TASK_ID}, 409)
                    return
                t = threading.Thread(
                    target=run_task_worker,
                    args=(task_id, url, goal, max_steps, profile),
                    daemon=True
                )
                t.start()
                self._send_json({"task_id": task_id, "status": "queued"})
                return
            # WO-019 P1: หลายสล็อต — จองหรือคิว (desktop task ยังเดินหลัง /api/desktop-task แยก)
            # task_id ต้องไม่ซ้ำ (ยิง 3 งานในวินาทีเดียว — task_<วินาที> ชนกัน = WORKER ทับกัน)
            payload, code = _acquire_or_queue(
                f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}", url, goal, max_steps, profile, kind="browser")
            self._send_json(payload, code)
            return

        # ---------- Computer Use (ระดับ A — อิสระภายในกรอบ, อนุมัติรายงานผ่าน popup) ----------
        if self.path == "/api/desktop-task":
            if TASK_SLOTS > 1:
                # WO-019 P1: desktop = กรรมสิทธิ์เครื่อง (จอ/เมาส์แด่ว) — ต้องว่างทั้งชุดสล็อต และขณะถือปฏิเสธทุกงานใหม่
                from engine.desktop_planner import plan_desktop_actions as _pda  # probe import เบาก่อนจอง
                slot = SLOTS.acquire(f"desk_{int(time.time())}_{uuid.uuid4().hex[:6]}", kind="desktop")
                if slot is None:
                    self._send_json({"error": "งาน desktop ต้องการทั้งเครื่อง — มีงานอื่นถือสล็อตอยู่ (รอจบก่อน)", "slots": SLOTS.snapshot()}, 409)
                    return
            else:
                if not RESERVATION.acquire(f"desk_{int(time.time())}"):
                    self._send_json({"error": "งานอื่นกำลังรันอยู่ (reservation)", "active_task_id": ACTIVE_TASK_ID}, 409)
                    return
            if TASK_SLOTS == 1 and ACTIVE_TASK_ID:
                self._send_json({"error": "งานอื่นกำลังรันอยู่ (browser task ยึด slot)"}, 409)
                return
            if not CFG.get("desktop_autonomy_enabled", False):
                self._send_json({"error": "desktop autonomy ยังปิดใน config (desktop_autonomy_enabled)"}, 403)
                return
            goal = (body.get("goal") or "").strip()
            allow = body.get("allow_apps") or None
            max_actions = min(int(body.get("max_actions", 60)), 200)
            if not goal:
                self._send_json({"error": "goal is required"}, 400)
                return
            def _worker():
                global ACTIVE_TASK_ID
                tid = f"desk_{int(time.time())}"
                ACTIVE_TASK_ID = tid
                TASKS[tid] = {"id": tid, "kind": "desktop", "goal": goal, "status": "waiting_approval",
                              "steps": [], "events": [], "telemetry": {}, "created_at": time.time()}

                def audit(rec):
                    TASKS[tid]["events"].append(dict(rec, at=time.time()))

                def notify_step(d):
                    TASKS[tid]["steps"].append(d)

                try:
                    from execution.win32_input import Win32InputBackend
                    # 1) เปิดหน้าต่างเป้าเอง (Notepad ดิสโพสาเบิล) เมื่อผู้ใช้เลือกแบบเปิดให้ — มีอนุมัติงานอยู่แล้วใน popup
                    allow_images = allow or ["C:\\Windows\\System32\\notepad.exe"]
                    proc = None
                    if body.get("new_notepad", True):
                        proc = subprocess.Popen(["notepad.exe"], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
                        time.sleep(1.2)
                    backend_obj = Win32InputBackend(0)  # capture foreground ตอน start() → popup จะอยู่ให้ก่อน
                    from execution.autonomous_session import AutonomousFrameSession
                    sess = AutonomousFrameSession(backend_obj, allowed_images=allow_images,
                                                  max_actions=max_actions, audit=audit,
                                                  task_details={"เป้าหมาย": goal[:80],
                                                                "แอปที่อนุญาต": ", ".join(p.rsplit("\\", 1)[-1] for p in allow_images)})
                    try:
                        sess.start()  # popup รายงาน — ผู้ใช้กดอนุมัติ/ปฏิเสธ
                        TASKS[tid]["status"] = "running"
                        STORE.snapshot(TASKS[tid])
                        TASKS[tid]["telemetry"] = {"approved_at": time.time(),
                                                   "rect": list(sess.inner.target.rect)}
                        notify_step({"action": "task_approved", "target": str(sess.inner.target.image), "status": "ok"})
                        from engine.desktop_planner import plan_desktop_actions
                        from engine.planner import TaskPlanner
                        actions, reason = plan_desktop_actions(TaskPlanner(), goal, sess.inner.target.rect, max_actions)
                        if not actions:
                            TASKS[tid]["status"] = "planner_declined"
                            TASKS[tid]["telemetry"]["reason"] = reason or "ไม่มีคำสั่ง"
                            return
                        notify_step({"action": "plan_ready", "target": f"{len(actions)} คำสั่ง", "status": "ok"})
                        t0 = time.monotonic()
                        for i, act in enumerate(actions, 1):
                            sess.do({"kind": act.kind, "args": list(act.args)})
                            notify_step({"action": act.kind, "target": ",".join(map(str, act.args))[:40],
                                         "status": "ok", "elapsed_ms": int((time.monotonic() - t0) * 1000)})
                        TASKS[tid]["status"] = "done"
                        TASKS[tid]["telemetry"]["answer"] = "ทำงานเสร็จตามสูตรที่โมเดลวาง"
                        TASKS[tid]["telemetry"]["total_elapsed_ms"] = int((time.monotonic() - t0) * 1000)
                        TASKS[tid]["telemetry"]["recipe_hit"] = None
                        TASKS[tid]["telemetry"]["planning_ms"] = TASKS[tid]["telemetry"]["total_elapsed_ms"]
                    finally:
                        sess.close()
                        if proc is not None:
                            time.sleep(0.6)
                            try:
                                if proc.poll() is None:
                                    proc.terminate()
                            except Exception:
                                pass
                except Exception as e:
                    TASKS[tid]["status"] = "error"
                    err = getattr(e, "args", [""])
                    reason = str(err[0]) if err and err[0] else type(e).__name__
                    TASKS[tid]["telemetry"] = {"error": str(reason)[:160]}
                    notify_step({"action": "error", "target": "", "status": str(reason)[:60]})
                finally:
                    STORE.snapshot(TASKS[tid])
                    RESERVATION.release()
                    ACTIVE_TASK_ID = None
            threading.Thread(target=_worker, daemon=True).start()
            self._send_json({"status": "queued", "note": "popup จะขึ้นบนจอ — กดอนุมัติเพื่อให้งานเริ่ม"})
            return

        # ---------- WO-020 Extension: Computer Use Vision Reader (อ่านหน้าต่าง LINE / แอปเป็น JSON) ----------
        if self.path == "/api/desktop-vision-read":
            if not CFG.get("desktop_autonomy_enabled", False):
                self._send_json({"error": "desktop autonomy ยังปิดใน config (desktop_autonomy_enabled)"}, 403)
                return
            room_name = str(body.get("room") or "sc13").strip() or "sc13"
            countdown = max(0, min(int(body.get("countdown", 3)), 15))
            image_file = str(body.get("image_file") or "").strip() or None
            tid = f"desk_vis_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            if TASK_SLOTS > 1:
                slot = SLOTS.acquire(tid, kind="desktop")
                if slot is None:
                    self._send_json({"error": "งาน desktop ต้องการทั้งเครื่อง — มีงานอื่นถือสล็อตอยู่", "slots": SLOTS.snapshot()}, 409)
                    return
            else:
                if not RESERVATION.acquire(tid):
                    self._send_json({"error": "งานอื่นกำลังรันอยู่ (reservation)", "active_task_id": ACTIVE_TASK_ID}, 409)
                    return

            def _vision_worker():
                global ACTIVE_TASK_ID
                ACTIVE_TASK_ID = tid
                TASKS[tid] = {
                    "id": tid,
                    "kind": "desktop_vision",
                    "goal": f"อ่านแชท LINE ห้อง {room_name} วันนี้เป็น JSON",
                    "status": "waiting_approval",
                    "steps": [],
                    "events": [],
                    "telemetry": {},
                    "created_at": time.time(),
                }
                t0 = time.monotonic()

                def notify_step(d):
                    TASKS[tid]["steps"].append(d)

                try:
                    from perception.desktop_vision_reader import run_line_room_vision_reader
                    notify_step({"action": "approval_popup", "target": f"LINE ({room_name})", "status": "waiting"})
                    res = run_line_room_vision_reader(
                        room_name=room_name,
                        require_approval=not bool(image_file),
                        countdown_sec=countdown,
                        image_file=image_file,
                    )
                    if not res.get("ok"):
                        TASKS[tid]["status"] = "denied"
                        TASKS[tid]["telemetry"] = {"error": res.get("error", "ไม่ได้รับอนุมัติ")}
                        notify_step({"action": "vision_read", "target": room_name, "status": "denied"})
                        return
                    notify_step({
                        "action": "capture_and_ocr",
                        "target": res.get("window", {}).get("title", room_name),
                        "status": "ok",
                        "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    })
                    TASKS[tid]["status"] = "done"
                    TASKS[tid]["telemetry"] = {
                        "answer": res.get("summary_th") or f"อ่านข้อความห้อง {room_name} สำเร็จ",
                        "evidence": res,
                        "total_elapsed_ms": int((time.monotonic() - t0) * 1000),
                        "planning_ms": int((time.monotonic() - t0) * 1000),
                        "recipe_hit": None,
                    }
                except Exception as e:
                    TASKS[tid]["status"] = "error"
                    TASKS[tid]["telemetry"] = {"error": str(e)[:240]}
                    notify_step({"action": "error", "target": room_name, "status": str(e)[:80]})
                finally:
                    STORE.snapshot(TASKS[tid])
                    RESERVATION.release()
                    ACTIVE_TASK_ID = None

            threading.Thread(target=_vision_worker, daemon=True).start()
            self._send_json({"task_id": tid, "status": "queued", "note": "popup จะขึ้นบนจอ — เปิดห้องแชทที่ต้องการแล้วกดอนุมัติ"})
            return

        # ---------- WO-018 R1: Shell/Command Runner (ธง shell_execution_enabled) ----------
        # WO-019 B1a: จด baseline กติกาใหม่ — admin เท่านั้น (หลังได้ Approval/Version แล้ว — ไม่ใช่ AI ตัดสิน)
        if self.path == "/api/config-anchor":
            role = self._resolve_role()
            if str(role or "").lower() != "admin":
                self._send_json({"error": "สิทธิ์ไม่พอ — admin เท่านั้น (จด baseline ได้หลังผ่าน Approval)", "role": role}, 403)
                return
            from service.config_guard import anchor_version
            note = str(body.get("version_note") or "manual-anchor").strip()[:120]
            res = anchor_version(note)
            self._send_json(res, 200 if res.get("ok") else 400)
            return

        # WO-019 C1 UI-04: สร้างสมาชิกใหม่ — admin เท่านั้น (token แสดงครั้งเดียว — เจ้าของเก็บเอง)
        if self.path == "/api/roles/add":
            role = self._resolve_role()
            if str(role or "").lower() != "admin":
                self._send_json({"error": "สิทธิ์ไม่พอ — admin เท่านั้น", "role": role}, 403)
                return
            from service import roles as _roles
            r = _roles.add_user(str(body.get("name") or "").strip(), str(body.get("role") or "").strip(),
                                str(body.get("expires") or "").strip())
            if r.get("error"):
                self._send_json(r, 400)
                return
            self._send_json({"ok": True, "name": r["name"], "role": r["role"], "expires": r.get("expires") or "",
                             "token": r["token"],  # ครั้งเดียวของการตอบ — หน้าเว็บเตือน "จดสำรองทันที"
                             "warning": "แสดงครั้งเดียวจนกว่าจะปิดหน้า — เจ้าของต้องเก็บเอง (ระบบไม่เก็บรูปแบบเต็ม)"})
            return

        # WO-019 M3: โหมดแชท — แปลงข้อความธรรมชาติ → งาน (เดาเป้าหมายเว็บจาก allowlistเดิม — fail-closed)
        if self.path == "/api/chat-task":
            goal = str(body.get("goal") or "").strip()
            if not goal:
                self._send_json({"error": "goal ต้องไม่ว่าง"}, 400)
                return
            from engine.planner_tier import is_easy_task as _easy
            try:
                max_steps = int(body.get("max_steps") or 3)
                if max_steps < 1 or max_steps > 50:
                    raise ValueError
            except (ValueError, TypeError):
                self._send_json({"error": "max_steps ต้องเป็นตัวเลข 1..50"}, 400)
                return
            # หา URL ที่ผู้ใช้ใบ้ (มีลิงก์ในข้อความ) — ไม่ใบ้ = รับ host จาก body — ไม่มีทั้งคู่ = ปฏิเสธ (ไม่เดาเว็บแทนคุณ)
            import re as _re
            url_in = ""
            m = _re.search(r"https?://[^\s\u0e00-\u0e7f]+", goal)
            if m:
                url_in = m.group(0)
            if not url_in:
                host = str(body.get("host") or "").strip().lower()
                if not host:
                    self._send_json({"error": "โหมดแชทต้องระบุเว็ปเป้าหมาย (host) ที่อนุมัติ เช่น books.toscrape.com — ระบบไม่เดาเว็บแทนคุณเมื่อไม่ได้ใบ้",
                                     "allowed_hosts": ["books.toscrape.com", "en.wikipedia.org"]}, 400)
                    return
                url_in = f"http://{host}/"
            if not _easy(goal, url_in):
                self._send_json({"error": "งานนี้ผ่านโหมดแชทไม่ได้ (เสี่ยง/เว็บไม่อนุมัติ) — ใช้หน้าฟอร์มพร้อม URL แล้วผู้ดูแลอนุมัติตามกระบวนการ",
                                 "reason": "tier_classifier_rejected"}, 403)
                return
            task_id = f"task_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            if TASK_SLOTS > 1:
                # WO-019 P1: หลายสล็อต — จองหรือคิว (kind=browser) แทน reservation เดิม
                payload, code = _acquire_or_queue(task_id, url_in, goal, max_steps, None, kind="browser")
                self._send_json(payload, code)
                return
            if not RESERVATION.acquire(task_id):
                self._send_json({"error": "Another task is already running (reservation)", "active_task_id": ACTIVE_TASK_ID}, 409)
                return
            t = threading.Thread(
                target=run_task_worker,
                args=(task_id, url_in, goal, max_steps, None),
                daemon=True
            )
            t.start()
            self._send_json({"task_id": task_id, "status": "queued", "mode": "chat"})
            return

        # WO-019 M2: บริบทธุรกิจราย-Node — อ่านได้ทุก role · แก้ได้ admin เท่านั้น
        if self.path == "/api/memory":
            from service import task_memory as _tm
            self._send_json({"entries": _tm.list_all()})
            return
        if self.path == "/api/memory/add":
            role = self._resolve_role()
            if str(role or "").lower() != "admin":
                self._send_json({"error": "สิทธิ์ไม่พอ — admin เท่านั้น", "role": role}, 403)
                return
            from service import task_memory as _tm
            r = _tm.add(str(body.get("key") or "").strip(), str(body.get("value") or "").strip(),
                        who=str(role).lower())
            self._send_json(r, 200 if r.get("ok") else 400)
            return
        if self.path == "/api/memory/remove":
            role = self._resolve_role()
            if str(role or "").lower() != "admin":
                self._send_json({"error": "สิทธิ์ไม่พอ — admin เท่านั้น", "role": role}, 403)
                return
            from service import task_memory as _tm
            r = _tm.remove(str(body.get("key") or "").strip())
            self._send_json(r, 200 if r.get("ok") else 400)
            return

        # WO-019 M1: ระงับ token ทันที (revoke) — admin เท่านั้น
        if self.path == "/api/roles/revoke":
            role = self._resolve_role()
            if str(role or "").lower() != "admin":
                self._send_json({"error": "สิทธิ์ไม่พอ — admin เท่านั้น", "role": role}, 403)
                return
            from service import roles as _roles
            r = _roles.revoke_user(str(body.get("name") or "").strip(), str(body.get("reason") or "").strip())
            self._send_json(r, 200 if r.get("ok") else 400)
            return

        if self.path == "/api/shell-run":
            command = (body.get("command") or "").strip()
            cwd = (body.get("cwd") or None)
            elevated = bool(body.get("elevated", False))
            if not command:
                self._send_json({"error": "command is required"}, 400)
                return
            from execution.shell_runner import ShellRunner
            r = ShellRunner().run(command, cwd=cwd, elevated=elevated)
            if r.denided_reason:
                self._send_json({"error": r.denided_reason,
                                 "hint": "เปิดธง shell_execution_enabled (และ shell_elevated_allowed ถ้าต้องการ) ใน config.json โดยผู้ใช้อนุมัติ"}, 403)
                return
            self._send_json({
                "ok": r.ok, "exit_code": r.exit_code,
                "stdout": r.stdout, "stderr": r.stderr,
                "duration_ms": r.duration_ms, "command": r.command,
                "manifest_seq": (r.manifest_entry or {}).get("seq"),
                "manifest_entry": r.manifest_entry,
            })
            return

        # ---------- WO-018 step2: หยุดงานกลางวิ่ง ----------
        if self.path == "/api/task-cancel":
            tid = (body.get("task_id") or "").strip()
            if not tid:
                self._send_json({"error": "task_id is required"}, 400)
                return
            from service import task_control
            task_control.request_cancel(tid)
            if tid in TASKS:
                TASKS[tid]["status"] = "cancelling"
                STORE.snapshot(TASKS[tid])
            # WO-019 P1: งานที่ยืนอยู่ในคิว (ยังไม่ได้สล็อต) = ชักออกจากคิวได้ทันที
            if QUEUE and QUEUE.remove(tid):
                if tid in TASKS:
                    TASKS[tid]["status"] = "cancelled"
                    TASKS[tid]["telemetry"] = dict(TASKS[tid].get("telemetry") or {}, error="cancelled while queued")
                    STORE.snapshot(TASKS[tid])
                self._send_json({"status": "cancelled_while_queued", "task_id": tid})
                return
            self._send_json({"status": "cancel_requested", "task_id": tid})
            return

        # ---------- WO-018 step2: กู้คืนไฟล์จาก sandbox คำสั่งล่าสุด (อ่านสถานะจากไฟล์ — ข้าม request) ----------
        if self.path == "/api/shell-rollback":
            state_file = BASE_DIR / "data" / "shell-sandbox-state.json"
            if not state_file.is_file():
                self._send_json({"error": "ไม่มีสิ่งให้กู้ (เปิด shell_sandbox_mode และรันคำสั่งก่อน)"}, 409)
                return
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:
                self._send_json({"error": "สถานะ sandbox พัง — ลบไฟล์ shell-sandbox-state.json แล้วรันคำสั่งใหม่"}, 500)
                return
            if not state.get("stash"):
                self._send_json({"error": "ไม่มี stash ให้กู้ (สนามว่าง/งานเกินขนาด)"}, 409)
                return
            from service.shell_sandbox import ShellSandbox
            sb = ShellSandbox(BASE_DIR / "data" / "shell-sandbox")
            stats = sb.restore(state["stash"], state["before"],
                               state["diff"] or {"created": [], "modified": [], "deleted": []})
            self._send_json({"status": "rolled_back", **stats})
            return

        # สถานะงานเดสก์ท็อปรายงาน (อ่านผ่าน /api/task/<id> ร่วมกับ browser task) — ไม่เพิ่ม endpoint ใหม่
        self.send_error(404)

    def log_message(self, format, *args):
        pass


def main():
    STORE.bootstrap()  # คืนงานจาก log เก่า (งานค้าง = ended_at_restart — งานไม่หาย)
    # WO-018 step4: กัน bind ซ้ำ (วันนี้พบ server ซาก 5 โปรเซสฟัง 8205 พร้อมกันบน Windows)
    # Windows + SO_REUSEADDR บน ThreadingHTTPServer ยอม bind ซ้ำบนอินเทอร์เฟซเดียวกัน → คำขอไปเข้าตัวเก่าด้วย
    try:
        import urllib.request as _u
        with _u.urlopen(f"http://127.0.0.1:{PORT}/api/status", timeout=2) as _r:
            sys.stderr.write(
                f"\n[!] มี server ตัวอื่นตอบอยู่แล้วที่พอร์ต {PORT} (HTTP {_r.status}) — ห้ามรันซ้ำ\n"
                f"    ปิดตัวเก่าก่อน: PowerShell → Get-NetTCPConnection -LocalPort {PORT} -State Listen | "
                f"ForEach-Object {{ Stop-Process -Id $_.OwningProcess -Force }}\n\n")
            sys.exit(1)
    except urllib.error.HTTPError:
        # ตอบแบบ status error (401/403) = มี server จริงฟังอยู่ — ห้ามรันซ้ำ
        sys.stderr.write(
            f"\n[!] มี server ตัวอื่นฟังพอร์ต {PORT} อยู่แล้ว (ตอบ auth error) — ห้ามรันซ้ำ ปิดตัวเก่าก่อน\n\n")
        sys.exit(1)
    except OSError:
        pass  # เชื่อมไม่ได้ = พอร์ตว่างจริง — เดินหน้า bind
    except Exception:
        pass  # ตรวจไม่ได้ด้วยเหตุอื่น — ปล่อยผ่านไปเจอ bind ตามจริง (fail-pass จุดเดียวที่ยอม)
    # Requirement A1: Bind 127.0.0.1 ONLY (never 0.0.0.0)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), InteractiveHandler)
    except OSError as e:
        win_err = getattr(e, "winerror", None)
        err_no = getattr(e, "errno", None)
        err_str = str(e)
        if win_err in (10013, 10048) or err_no in (48, 98, 13) or "10048" in err_str or "10013" in err_str:
            sys.stderr.write(
                f"\n[!] ข้อผิดพลาด: พอร์ต {PORT} ถูกใช้งานอยู่แล้วโดยโปรแกรมอื่น (Port {PORT} already in use)\n"
                f"    สาเหตุ: มีโปรแกรมอื่นหรือเซิร์ฟเวอร์รอบก่อนหน้ากำลังใช้งานพอร์ตนี้อยู่\n"
                f"    วิธีแก้ไข:\n"
                f"    1. ตรวจสอบว่ามีหน้าต่าง Browser Engine เดิมเปิดค้างอยู่หรือไม่ หากมีให้ปิดหน้าต่างนั้น\n"
                f"    2. ค้นหาและปิดโปรเซสที่ใช้พอร์ตนี้ด้วยคำสั่ง PowerShell/CMD:\n"
                f"       netstat -ano | findstr :{PORT}\n"
                f"       taskkill /PID <PID_ที่พบ> /F\n"
                f"    3. หรือเปลี่ยนหมายเลขพอร์ตในไฟล์ config.json เป็นพอร์ตอื่น เช่น 8206\n\n"
            )
            sys.exit(1)
        raise
    print(f"============================================================")
    print(f"  OneVault Interactive Agent Engine running at http://127.0.0.1:{PORT}")
    # WO-019 A1: tiering ธงเปิด = อุ่นโมเดลเล็กแบบพื้นหลังทันทีตอนเปิดเซิร์ฟเวอร์ (ไม่ให้งานแรกจ่าย cold 45 วิ)
    # พิสูจน์ 5 รอบติด (WO019-TIER-PROOF.json): tier 5/5 · planning 5.0-5.6 วิ (เทียบ 27B 44 วิ = ~8×) · คำตอบจากหน้าจริง · fallback=0
    try:
        from engine.planner_tier import make_tiered_planner_if_enabled
        _tp = make_tiered_planner_if_enabled()
        if _tp is not None:
            _tp.small_client.ensure_warm()
            print(f"  Planner tiering: ON — warming {_tp.small_client.model} (background)")
        else:
            print(f"  Planner tiering: off (โมเดลใหญ่ทุกงานตามเดิม)")
    except Exception:
        pass
    # WO-019 B1a Config Guard: จด baseline config ตอนเปิด — แก้ตรงข้ามกระบวนการ = ตรวจเจอ (แจ้งบน /api/status)
    try:
        from service.config_guard import anchor_baseline
        _anchor = anchor_baseline("boot")
        if _anchor.get("ok"):
            print(f"  Config guard: baseline anchored ({_anchor['config_sha256'][:12]}…)")
        else:
            print("  Config guard: config อ่านไม่ได้ — เดินต่อในโหมดแจ้งเตือนเฉพาะ")
    except Exception as _cg:
        print(f"  Config guard: ยังไม่ทำงาน ({_cg}) — ไม่บล็อกการเปิด")
    print(f"  Auth Token stored at: {TOKEN_FILE}")
    print(f"  Connected to DGX vLLM at: {CFG.get('vllm_url')}")
    print(f"============================================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.server_close()


if __name__ == "__main__":
    main()
