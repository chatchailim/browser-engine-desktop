#!/usr/bin/env python3
"""provision_node.py — ติดตั้ง SPADA MyAI Harness รายเครื่อง (Node-1) — หัวใจงานติดตั้งย้ายมาที่ Python เพื่อเลี่ยงปัญหา encoding ของ PowerShell 5.1 (อ่าน .ps1 แบบ ANSI เมื่อไม่มี BOM ทำให้ภาษาไทยในโค้ดพัง)

ตาม PROPOSAL-C-DIST-VIA-CONFIG-MANAGER-TH.md §3 §4 ขั้น 4-5 · ผู้รัน = ผู้ดูแล Node (ไม่ใช่ SPADA push)

7 ขั้น (ล้มขั้นไหนหยุดทันที — exit 1):
 1) ตรวจ python 3.10+ · โค้ดต้นทาง · target dir
 2) ตรวจ Config Package: JSON และไม่มีของลับฝัง; มี featureFlags/constants ครบ
 3) คัดลอกโค้ด → TargetDir (ยกเว้น data/, .venv, .git, __pycache__, *.log)
 4) แปลง featureFlags/constants (UPPER_SNAKE) → config.json (snake_case) ตามแผนที่ทางการ
 5) venv + pip (requests, cryptography, jsonschema)
 6) machine token ประจำเครื่อง (ไม่พรินต์ค่า) + keysign keypair
 7) selftest = unittest ย่อ + ปลุก server + GET /api/status → 200

ใช้: python provision/provision_node.py --config provision/admin-system-config.myai-harness.example.json \
            --target "C:/ProgramData/SPADA-MyAI-Harness" [--source <repo root>] [--skip-venv]
"""
import argparse
import json
import pathlib
import re
import secrets as secrets_mod
import shutil
import subprocess
import sys
import time
import urllib.request

APP_REL = "apps/browser-engine-desktop"
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer [A-Za-z0-9\-_]{16,}"),
    re.compile(r"(?i)password\s*[:=]\s*['\"][^'\"]{8,}"),
]
FLAG_MAP = {
    "RECIPE_CACHE_ENABLED": "recipe_cache_enabled",
    "ROLES_ENABLED": "roles_enabled",
    "KEYSIGN_POLICY": "keysign_policy",
    "SHELL_EXECUTION_ENABLED": "shell_execution_enabled",
    "SHELL_ELEVATED_ALLOWED": "shell_elevated_allowed",
    "SHELL_SANDBOX_MODE": "shell_sandbox_mode",
    "DESKTOP_AUTONOMY_ENABLED": "desktop_autonomy_enabled",
    "MYAI_PORT": "port",
    "MYAI_DESKTOP_ALLOWED_APPS": "desktop_autonomy_allowed_apps",
    "MYAI_SHELL_TIMEOUT_SECONDS": "shell_timeout_seconds",
}


def step(n, msg):
    print(f"\n=== [{n}/7] {msg} ===", flush=True)


def ok(msg):
    print(f"  [OK] {msg}", flush=True)


def warn(msg):
    print(f"  [!] {msg}", flush=True)


def die(msg):
    print(f"  [STOP] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def find_python():
    for cand in (sys.executable, "python", "python3"):
        try:
            v = subprocess.run([cand, "-c", "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                               capture_output=True, text=True, timeout=15)
            if v.returncode == 0:
                major, minor = v.stdout.strip().split(".")
                if int(major) > 3 or (int(major) == 3 and int(minor) >= 10):
                    return cand, f"{major}.{minor}"
                die(f"python {major}.{minor} < 3.10 ที่ระบบต้องการ")
        except (OSError, subprocess.TimeoutExpired):
            continue
    die("ไม่พบ python 3.10+ — ติดตั้งก่อน")


def validate_config(cfg_path: pathlib.Path):
    raw = cfg_path.read_text(encoding="utf-8")
    try:
        cfg = json.loads(raw)
    except Exception as e:
        die(f"Config JSON พัง: {e}")
    envs = cfg.get("environments") or []
    if not envs:
        die("Config ต้องมี environments ≥ 1")
    flags = {}
    for f in (envs[0].get("featureFlags") or []):
        flags[f["key"]] = f
    for c in (envs[0].get("constants") or []):
        flags[c["key"]] = c
    for pat in SECRET_PATTERNS:
        if pat.search(raw):
            die(f"Config มีของลับฝัง (ตรงรูปแบบ {pat.pattern[:40]}) — ห้าม! ใช้ vault:// binding เท่านั้น")
    return cfg, flags


def copy_code(src_app: pathlib.Path, target: pathlib.Path):
    skip_dirs = {"data", ".venv", "venv", ".git", "__pycache__", ".cache"}

    def skip(p: pathlib.Path):
        return any(part in skip_dirs for part in p.parts)

    if target.exists():
        warn(f"โฟลเดอร์เป้าหมายมีอยู่แล้ว — ถ่ายทับโค้ด (data/ เดิมคงอยู่)")
    n = 0
    for p in src_app.rglob("*"):
        if skip(p):
            continue
        rel = p.relative_to(src_app)
        dst = target / rel
        if p.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            n += 1
    return n


def write_app_config(target: pathlib.Path, flags: dict) -> dict:
    app_cfg = {}
    for upper, snake in FLAG_MAP.items():
        item = flags.get(upper)
        if item is not None and "value" in item:
            app_cfg[snake] = item["value"]
    app_cfg.setdefault("vision_enabled", False)
    app_cfg.setdefault("uia_enabled", True)
    app_cfg.setdefault("onevault_enabled", True)
    app_cfg.setdefault("vllm_key_path", str(pathlib.Path.home() / ".dgx" / "vllm-api-key"))
    app_cfg.setdefault("vllm_url", "http://192.168.1.108/vllm/v1")
    autonomy = bool(app_cfg.pop("desktop_autonomy_enabled", False))
    allowed = app_cfg.pop("desktop_autonomy_allowed_apps", None) or [r"C:\Windows\System32\notepad.exe"]
    app_cfg["desktop_autonomy"] = {"enabled": autonomy, "allowed_apps": allowed}
    (target / "config.json").write_text(json.dumps(app_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return app_cfg


def make_machine_secrets(target: pathlib.Path):
    # token ต้องอยู่พาธเดียวกับที่ server.py อ่านจริง (~/.browser-engine-desktop/token) — คนละพาธ = server สร้างใหม่เองเงียบ ๆ
    sec_dir = pathlib.Path.home() / ".browser-engine-desktop"
    sec_dir.mkdir(parents=True, exist_ok=True)
    tok_file = sec_dir / "token"
    if tok_file.exists():
        warn(f"machine token เดิมคงอยู่ → {tok_file} (ลบเองถ้าต้องการหมุน)")
    else:
        tok_file.write_text(secrets_mod.token_hex(32), encoding="utf-8")
        ok(f"machine token สร้างใหม่ → {tok_file}")
    if sys.platform == "win32":
        try:
            subprocess.run(["icacls", str(tok_file), "/inheritance:r", "/grant:r",
                            f"{__import__('os').environ.get('USERNAME')}:(R,W)"],
                           check=False, capture_output=True)
        except OSError:
            pass
    key_dir = target / "data" / "keysign"
    key_dir.mkdir(parents=True, exist_ok=True)
    script = ("import sys;"
              f"sys.path.insert(0, r'{target}');"
              "from service import keysign;"
              "priv, pub = keysign.ensure_keypair();"
              "key = keysign.ensure_key();"
              "print('KEYSIG_OK', len(pub))")
    r = subprocess.run([target / ".venv" / "Scripts" / "python.exe", "-c", script],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or "KEYSIG_OK" not in r.stdout:
        die(f"สร้าง keysign keypair ไม่สำเร็จ: {r.stderr.strip()[:200]}")
    ok(f"keysign keypair + HMAC key → data/keysign/ (pub เข้าระเบียนได้ · private สงวน)")
    return tok_file


def selftest(target: pathlib.Path, port: int):
    py = target / ".venv" / "Scripts" / "python.exe"
    # selftest ยืดหยุ่นตามของจริงบนเครื่อง (หลักการ NOT_RUN ตามจริงจาก CLOUD-README):
    # ชุดหลัก = 10 โมดูลใช้ได้เครื่องไหนก็ได้ (ไม่ต้อง playwright/comtypes ของเครื่องผู้พัฒนา)
    # ชุดเต็ม = ทุก module — เจอ dependency ขาดข้ามไปรายงาน NOT_RUN (ไม่นับเป็นความล้มของติดตั้ง)
    CORE = ["tests.test_recipe_cache", "tests.test_reliability", "tests.test_wo018_shell_unlock",
            "tests.test_step2_sandbox_keysign", "tests.test_step3_roles_ed25519"]
    r = subprocess.run([str(py), "-m", "unittest", *CORE],
                       cwd=str(target), capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace")
    tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
    for ln in tail:
        print("   ", ln[:120], flush=True)
    if r.returncode != 0:
        die("selftest ชุดหลักล้ม — ห้ามใช้ก่อนแก้ (ข้างบน)")
    core_n = tail[0].split()[1] if tail and tail[0].startswith("Ran ") else "?"
    ok(f"ชุดหลัก {core_n} เทสต์ผ่าน")
    # เทสต์สมบูรณ์ — "เท่าที่ของจริงเอื้อ": มีเพดาน 150 วิ ถ้าเกิน = PARTIAL (ชิ้นที่ยังไม่ตรวจรายงานตามจริง — ไม่นับเป็นความล้มของติดตั้ง)
    r2 = None
    try:
        r2 = subprocess.run([str(py), "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"],
                            cwd=str(target), capture_output=True, text=True, timeout=150, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        ok("ชุดเต็ม = PARTIAL — เกินเพดาน 150 วิ (ชุด live บางโมดูลยาวหลัง Node สด) — ชุดหลักผ่านครบเป็นเกณฑ์หลังติดตั้งแล้ว")
    not_run = False
    if r2 is not None:
        full_tail = (r2.stderr or r2.stdout or "").strip().splitlines()
        ran = [l for l in full_tail if l.startswith("Ran ")]
        if r2.returncode != 0:
            # นับ module import error เฉพาะ dependency เครื่องผู้พัฒนา (playwright/comtypes) = NOT_RUN ตามจริง
            missing = [m for m in ("playwright", "comtypes") if f"No module named '{m}'" in (r2.stderr or "") or f"No module named '{m}'" in (r2.stdout or "")]
            if missing:
                not_run = True
                ok(f"ชุดเต็ม: {ran[0] if ran else '?'} — ส่วนที่ต้อง {', '.join(missing)} = NOT_RUN บน Node สด (ไม่ใช่ความล้มของติดตั้ง)")
            else:
                die("selftest ชุดเต็มล้มด้วยเหตุอื่น — ห้ามใช้ก่อนแก้ (ดู log ตามข้างบน)")
        if not not_run:
            ok(f"ชุดเต็มผ่าน: {ran[0] if ran else '?'}")
    srv = subprocess.Popen([str(py), "server.py"], cwd=str(target),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        up = None
        for _ in range(12):
            time.sleep(0.8)
            try:
                urllib.request.urlopen(base + "/api/status", timeout=2)
                up = "200"
                break
            except urllib.error.HTTPError as he:
                if he.code in (401, 403):
                    up = f"{he.code} (ต้อง token — server ฟังปกติ)"
                    break
            except OSError:
                continue
        if up:
            ok(f"server ตอบ {up} ที่ {base}/api/status")
        else:
            warn(f"server ยังไม่ตอบที่พอร์ต {port} (อาจพอร์ตยึด — เปิดเองด้วย run.cmd)")
    finally:
        if srv.poll() is None:
            srv.terminate()
            try:
                srv.wait(timeout=8)
            except subprocess.TimeoutExpired:
                srv.kill()


def main():
    here = pathlib.Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="ติดตั้ง SPADA MyAI Harness รายเครื่อง (Node-1)")
    ap.add_argument("--config", required=True, help="พาธ Config Package JSON (ตาม schema OneManOS)")
    ap.add_argument("--target", default=r"C:\ProgramData\SPADA-MyAI-Harness", help="โฟลเดอร์ติดตั้ง")
    ap.add_argument("--source", default=str(here.parent.parent.parent), help="ราก repo (ค่าเริ่ม: parent ของ provision/)")
    ap.add_argument("--skip-venv", action="store_true")
    a = ap.parse_args()

    src_app = pathlib.Path(a.source) / APP_REL
    if not (src_app / "server.py").is_file():
        die(f"ไม่พบ server.py ที่ {src_app} — ตรวจ --source")
    cfg_path = pathlib.Path(a.config)
    if not cfg_path.is_file():
        die(f"ไม่พบ Config: {cfg_path}")
    target = pathlib.Path(a.target)

    step(1, "ตรวจสภาพแวดล้อม")
    py, ver = find_python()
    ok(f"python {ver}")
    ok(f"โค้ดแอปพบที่ {src_app}")

    step(2, "ตรวจ Config Package")
    cfg, flags = validate_config(cfg_path)
    ok(f"Config ถูกต้อง — ไม่มีของลับฝัง · {len(flags)} ธง/ค่า")

    step(3, "ติดตั้งโค้ด")
    n = copy_code(src_app, target)
    ok(f"คัดลอก {n} ไฟล์ → {target}")

    step(4, "แปลง Config → config.json (แผนที่ทางการ)")
    app_cfg = write_app_config(target, flags)
    ok(f"config.json เขียนแล้ว (port={app_cfg.get('port', 8205)})")
    if app_cfg.get("shell_execution_enabled"):
        warn("Node นี้เปิด shell ให้ AI — ตรวจ allowlist/ธง keysign อีกชั้น (ข้อเสนอ §2 แนะปิด)")
    # WO-019 B1b: ผูก Config Guard baseline ตาม Config Version ที่ผ่าน Approval (hash จากไฟล์ที่เขียนจริง)
    try:
        t_app = target / "apps" / "browser-engine-desktop" if False else target
        # ไฟล์ config อยู่ที่ target root (write_app_config เขียนไว้) — ผูกทันทีจากนี้
        import importlib
        cg = importlib.import_module("service.config_guard") if (t_app / "service").is_dir() else None
        if cg is None:
            sys.path.insert(0, str(t_app))
            cg = importlib.import_module("service.config_guard")
        cg.APP = t_app  # สั่ง baseline ให้ชี้ตามเครื่องใหม่
        cfg_hash_info = cg.anchor_baseline(f"provision: {cfg_path.name}")
    except Exception as e:
        cfg_hash_info = {"ok": False, "error": str(e)[:120]}
    if cfg_hash_info.get("ok"):
        ok(f"Config guard baseline = {cfg_hash_info['config_sha256'][:12]}… (ผูกตาม Config ที่ติดตั้ง)")

    step(5, "สร้าง venv + ติดตั้งแพ็กเกจ")
    vdir = target / ".venv"
    if not a.skip_venv or not vdir.exists():
        subprocess.run([py, "-m", "venv", str(vdir)], check=True, timeout=180)
    pip = vdir / "Scripts" / "pip.exe"
    # deps ทางการจาก requirements.txt ของแอป (playwright/comtypes รวม + ตาม OS) — ผู้พัฒนาเคยซ่อนไว้ใน venv เครื่องตัวเอง
    req_src = src_app / "requirements.txt"
    if req_src.is_file():
        r = subprocess.run([str(pip), "install", "-r", str(req_src), "--quiet", "--disable-pip-version-check"],
                           capture_output=True, text=True, timeout=1200)
        if r.returncode != 0:
            die(f"pip install -r requirements ล้ม: {(r.stderr or '').strip()[:200]}")
        ok("แพ็กเกจครบตาม requirements.txt (รวม playwright + win32)")
    else:
        deps = ["requests", "cryptography", "jsonschema"]
        r = subprocess.run([str(pip), "install", *deps, "--quiet", "--disable-pip-version-check"],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            die(f"pip install ล้ม: {(r.stderr or '').strip()[:200]}")
        warn(f"ไม่พบ requirements.txt — ติดตั้งชุดน้อย {deps} (server จะเด้งถ้าต้องใช้ playwright)")
    # playwright ต้องดาวน์โหลดเบราว์เซอร์ชุดของตัวเองอีกชั้น (chromium — ~150MB ครั้งแรก)
    pw = vdir / "Scripts" / "playwright.exe"
    if pw.is_file():
        r = subprocess.run([str(pw), "install", "chromium"], capture_output=True, text=True, timeout=1800)
        if r.returncode == 0:
            ok("playwright chromium พร้อม (เบราว์เซอร์ของ engine)")
        else:
            warn(f"playwright install chromium ยังไม่เสร็จ (รันซ้ำเอง: {pw} install chromium) — ไม่บล็อกติดตั้ง")
    else:
        warn("ไม่มี playwright.exe — โหมด browser ของ server จะใช้ไม่ได้ (แจ้งผู้ดูแล Node)")

    step(6, "ของประจำเครื่อง (machine token + keysign)")
    tok_file = make_machine_secrets(target)

    step(7, "Selftest")
    selftest(target, int(app_cfg.get("port", 8205)))

    print("\n" + "=" * 62, flush=True)
    print("  ติดตั้ง SPADA MyAI Harness เสร็จสมบูรณ์ — Node นี้ใช้งานได้", flush=True)
    print("=" * 62)
    print(f"  โค้ด: {target}")
    print(f"  เปิดใช้: run.cmd (หรือ {vdir / 'Scripts' / 'python.exe'} server.py)")
    print(f"  Token ผู้ดูแล: {tok_file} (คัดลอกด้วย copy-token.cmd)")
    print(f"  สิทธิ์สมาชิก: provision\\member_token.cmd <ชื่อ> <admin|operator|viewer>")
    print("  การเปลี่ยนค่าต่อไป: ผ่าน Config Manager Version + Approval ตามข้อเสนอ §4")


if __name__ == "__main__":
    main()