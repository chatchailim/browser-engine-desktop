"""Human-only console entry point. No server/planner API and no implicit opt-in."""
import argparse
from datetime import datetime, timezone
import json
import os
import pathlib
import sys
import time

APP = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from execution.guarded_desktop import GuardedDesktopSession, InputDenied

CONTROL = APP.parents[2] / "coordination/codex-anti/control.json"
STOP = APP / "service/DESKTOP-STOP"


def stop_requested():
    try:
        return STOP.exists() or json.loads(CONTROL.read_text("utf-8")).get("paused") is not False
    except (OSError, ValueError):
        return True


def pick_window(candidates):
    if not candidates:
        raise InputDenied("no_allowed_window_open_Notepad_and_restart")
    print("Choose the TEST document only. Do not select a song or another working file.")
    for number, candidate in enumerate(candidates, 1):
        print(f"  {number}: {candidate['title']} (PID {candidate['pid']})")
    answer = input("Window number (or STOP): ").strip()
    if not answer.isascii() or not answer.isdecimal() or not 1 <= int(answer) <= len(candidates):
        raise InputDenied("window_selection_cancelled_or_invalid")
    return candidates[int(answer) - 1]


def wait_for_target(backend, session, clock=time.monotonic, sleep=time.sleep):
    # Approval expiry is unchanged; waiting never renews it.
    limit = min(clock() + 20, session.pending[2])
    while clock() < limit:
        session._check_stop()
        if backend.target_ready():
            return
        sleep(.1)
    raise InputDenied("target_focus_wait_expired_no_input_sent")


def main():
    parser = argparse.ArgumentParser(description="Supervised experimental Windows input; use disposable documents only.")
    parser.add_argument("--enable-input", action="store_true")
    parser.add_argument("--hwnd", type=lambda v: int(v, 0))
    parser.add_argument("--select-foreground", action="store_true", help="Select foreground window after 15-second countdown")
    parser.add_argument("--choose-window", action="store_true", help="Choose an allowed window by number; no capture countdown")
    parser.add_argument("--allow-exe", help="Exact absolute executable path, no wildcards")
    args = parser.parse_args()
    if not args.enable_input:
        print("DISABLED. Explicit --enable-input, --hwnd and --allow-exe are required.")
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Interactive human terminal required; redirected/piped commands rejected.")
        return 2
    if (sum((bool(args.hwnd), args.select_foreground, args.choose_window)) != 1 or not args.allow_exe
            or not pathlib.Path(args.allow_exe).is_absolute() or not pathlib.Path(args.allow_exe).is_file()):
        parser.error("Supply a top-level HWND and an existing absolute executable path")
    if stop_requested():
        print("STOP active, control paused, or control unreadable. No native backend started.")
        return 2
    print("EXPERIMENTAL: global input may race with focus changes. Use a disposable test document.")
    print("No shell/security apps, hotkeys, clipboard, or credentials. Hold Escape to stop input.")
    print("Confirm the exact executable and HWND:", args.allow_exe, args.hwnd)
    log_dir = APP / "data/desktop-input"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / ("session-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + str(os.getpid()) + ".jsonl")

    def audit(record):
        record = dict(record, at=datetime.now(timezone.utc).isoformat())
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    backend, session = None, None
    stage = "awaiting_enable"
    try:
        audit({"event": "console_ready", "stage": stage})
        if input("Type ENABLE to create a five-minute session: ") != "ENABLE":
            audit({"event": "enable_declined"})
            return 2
        stage = "native_initialization"
        audit({"event": "stage", "stage": stage})
        # Native initialization is inside the diagnostic boundary and follows opt-in.
        from execution.win32_input import Win32InputBackend
        backend = Win32InputBackend(args.hwnd or 0)
        selected = None
        if args.choose_window:
            stage = "choosing_target"
            selected = pick_window(backend.list_allowed_windows(str(pathlib.Path(args.allow_exe))))
            backend.hwnd = selected["hwnd"]
        session = GuardedDesktopSession(backend, enabled=True, allowed_images=[str(pathlib.Path(args.allow_exe))],
                                        stop_requested=stop_requested, audit=audit)
        stage = "binding_target"
        audit({"event": "stage", "stage": stage})
        if args.select_foreground:
            print("Click INSIDE the empty Untitled Notepad now. Stay there until capture finishes.", flush=True)
            for remaining in range(15, 0, -1):
                if stop_requested():
                    raise InputDenied("session_stopped_before_capture")
                print(f"Capturing target in {remaining:2d} seconds...", flush=True)
                time.sleep(1)
        target = session.bind()
        if selected and (target.hwnd != selected["hwnd"] or target.pid != selected["pid"]
                         or target.created != selected["created"] or target.image.casefold() != selected["image"].casefold()):
            raise InputDenied("selected_window_changed_restart_required")
        print("Bound client rectangle (physical screen pixels):", target.rect)
        print('Actions: {"kind":"click","args":[x,y]}, {"kind":"drag","args":[x1,y1,x2,y2]},')
        print('         {"kind":"type","args":["test text"]}, {"kind":"key","args":["LEFT"]}')
        print("One JSON action at a time. STOP or Ctrl+C ends session. No automatic retry.")
        print("For the first typing test, enter TEST instead of JSON (adds:  TEST-456).")
        while True:
            stage = "awaiting_action"
            raw = input("action> ")
            if raw.strip().upper() in {"STOP", "QUIT", "EXIT"}:
                break
            try:
                data = {"kind": "type", "args": [" TEST-456"]} if raw.strip().upper() == "TEST" else json.loads(raw)
                token, action = session.prepare(data)
            except (ValueError, InputDenied) as exc:
                audit({"event": "action_rejected", "error_type": type(exc).__name__})
                print("Rejected:", str(exc) if isinstance(exc, InputDenied) else "invalid JSON")
                if session.stopped.is_set():
                    break
                continue
            print("Review exact action:", json.dumps({"kind": action.kind, "args": action.args}, ensure_ascii=False))
            print("Approval expires after 30 seconds. After approving, click inside the chosen Notepad.")
            if input("Approve this action? Type Y then Enter (anything else cancels): ").strip().upper() != "Y":
                audit({"event": "approval_declined"})
                print("Rejected; nothing executed.")
                session.pending = None
                continue
            print("Focus the bound target window now; release all modifiers. Do not move/resize it.", flush=True)
            stage = "executing_action"
            wait_for_target(backend, session)
            result = session.execute(token)
            print(json.dumps(result))
            print("Check the visible result yourself before approving another action. No automatic success claim.")
    except (KeyboardInterrupt, EOFError):
        audit({"event": "operator_stopped", "stage": stage})
        print("Stopped by operator.")
    except Exception as exc:
        record = {"event": "session_error", "stage": stage, "error_type": type(exc).__name__}
        if isinstance(exc, InputDenied):
            record["reason"] = str(exc)
        for field in ("hresult", "winerror"):
            value = getattr(exc, field, None)
            if type(value) is int:
                record[field] = value
        try:
            audit(record)
        except OSError:
            print("Diagnostic log could not be written.")
        # Do not leak arbitrary native error strings/control content to logs.
        print("Session stopped:", str(exc) if isinstance(exc, InputDenied) else type(exc).__name__)
        if isinstance(exc, InputDenied) and str(exc) == "security_process_blocked":
            print("The selected foreground app is blocked (for example a terminal). No input was sent.")
            print("Restart this test. After ENABLE, click inside the empty Notepad during the countdown.")
        return 1
    finally:
        if session is not None:
            session.close()
        elif backend is not None:
            backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
