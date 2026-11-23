"""Data Retention & PDPA Compliance Module (WO-012).
Implements:
- Targeted Right to Erasure (F7): deletes trace, task answers, and screenshots.
  Emits FactDataErased while preserving existing fact verifiable history.
- Time-based data retention pruning (F8): purges traces and screenshots older than retention window.
"""
import json
import os
import pathlib
import re
import sys
import time
from typing import Any, Dict, List, Optional

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TRACES_DIR = DATA_DIR / "traces"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
DOWNLOADS_DIR = DATA_DIR / "downloads"

from telemetry.vault import get_vault_manager, load_config

TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def is_safe_task_id(task_id: str) -> bool:
    """Validate task_id against strict allowlist (^[A-Za-z0-9._-]{1,128}$).
    Rejects path traversal (.., /, \\, :) and wildcard injection (*, ?).
    """
    if not task_id or not isinstance(task_id, str):
        return False
    if not TASK_ID_PATTERN.match(task_id):
        return False
    if ".." in task_id or "/" in task_id or "\\" in task_id or ":" in task_id:
        return False
    return True


class RetentionManager:
    @staticmethod
    def erase_task_data(
        task_id: str,
        reason: str = "PDPA Right to Erasure request",
        actor: Optional[Dict[str, str]] = None,
        env: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Erase all raw operational data associated with a task under PDPA (F7, R1, R2).
        Removes:
        1. Local JSONL trace file (data/traces/<task_id>.jsonl)
        2. Associated screenshots (data/screenshots/<hash>.jpg)
        3. Task download artifacts (matching <task_id>_ or <task_id>.)
        Emits FactDataErased to OneVault without breaking chain of custody.
        """
        if not is_safe_task_id(task_id):
            return {
                "ok": False,
                "task_id": task_id,
                "error": f"Invalid task_id '{task_id}'. Must match ^[A-Za-z0-9._-]{{1,128}}$ with no path traversal or wildcards.",
                "erased": False,
                "erased_artifacts": [],
                "fact_issued": False
            }

        erased_artifacts: List[str] = []
        screenshot_hashes_to_delete: List[str] = []

        # 1. Inspect trace for screenshot hashes before deleting
        traces_root = TRACES_DIR.resolve()
        trace_file = (TRACES_DIR / f"{task_id}.jsonl").resolve()
        try:
            trace_file.relative_to(traces_root)
        except ValueError:
            return {
                "ok": False,
                "task_id": task_id,
                "error": "Path traversal attempt detected",
                "erased": False,
                "erased_artifacts": [],
                "fact_issued": False
            }

        if trace_file.is_file():
            try:
                with open(trace_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                rec = json.loads(line)
                                sh = rec.get("decision", {}).get("screenshot_hash")
                                if sh and isinstance(sh, str):
                                    screenshot_hashes_to_delete.append(sh)
                            except Exception:
                                pass
            except Exception:
                pass

            try:
                trace_file.unlink(missing_ok=True)
                erased_artifacts.append("trace")
            except Exception as e:
                sys.stderr.write(f"[*] [PDPA] Could not remove trace file {trace_file}: {e}\n")

        # 2. Delete linked screenshots (strictly under SCREENSHOTS_DIR)
        shots_root = SCREENSHOTS_DIR.resolve()
        deleted_screenshots = 0
        for sh in set(screenshot_hashes_to_delete):
            if not re.match(r"^[a-f0-9]{32,64}$", sh, re.IGNORECASE):
                continue
            img_file = (SCREENSHOTS_DIR / f"{sh}.jpg").resolve()
            try:
                img_file.relative_to(shots_root)
                if img_file.is_file():
                    img_file.unlink(missing_ok=True)
                    deleted_screenshots += 1
            except Exception:
                pass
        if deleted_screenshots > 0:
            erased_artifacts.append("screenshots")

        # 3. Clean any task-specific downloads
        if DOWNLOADS_DIR.is_dir():
            dls_root = DOWNLOADS_DIR.resolve()
            prefix_under = f"{task_id}_"
            prefix_dot = f"{task_id}."
            for dl_file in list(DOWNLOADS_DIR.iterdir()):
                if not dl_file.is_file():
                    continue
                fname = dl_file.name
                if fname.startswith(prefix_under) or fname.startswith(prefix_dot):
                    try:
                        dl_file.resolve().relative_to(dls_root)
                        dl_file.unlink(missing_ok=True)
                        if "downloads" not in erased_artifacts:
                            erased_artifacts.append("downloads")
                    except Exception:
                        pass

        # 4. Emit FactDataErased to OneVault
        vault = get_vault_manager()
        fact = vault.create_erasure_fact(
            task_id=task_id,
            reason=reason,
            erased_artifacts=erased_artifacts,
            erased_by_actor=actor,
            env=env
        )
        if fact:
            vault.spool_fact(fact)
            vault.flush_spool(timeout=0.5)

        return {
            "ok": True,
            "task_id": task_id,
            "erased": True,
            "erased_artifacts": erased_artifacts,
            "reason": reason,
            "fact_issued": bool(fact),
            "fact_id": fact.get("factId") if fact else None
        }

    @staticmethod
    def purge_expired_traces(retention_days: Optional[int] = None) -> Dict[str, Any]:
        """Prune traces and screenshots exceeding the configured retention policy (F8).
        If retention_days == 0, purges all traces immediately.
        """
        cfg = load_config()
        if retention_days is not None:
            if not isinstance(retention_days, int) or isinstance(retention_days, bool):
                raise ValueError("retention_days must be an integer between 0 and 3650")
            if retention_days < 0 or retention_days > 3650:
                raise ValueError("retention_days out of range [0, 3650]")
            days = retention_days
        else:
            days = int(cfg.get("trace_retention_days", 90))

        now_ts = time.time()
        cutoff_seconds = days * 86400.0

        purged_traces = 0
        purged_screenshots = 0

        # Purge traces
        if TRACES_DIR.is_dir():
            for tf in list(TRACES_DIR.glob("*.jsonl")):
                try:
                    mtime = tf.stat().st_mtime
                    if days == 0 or (now_ts - mtime >= cutoff_seconds):
                        tf.unlink(missing_ok=True)
                        purged_traces += 1
                except Exception as e:
                    sys.stderr.write(f"[*] [Retention] Failed to delete trace {tf}: {e}\n")

        # Purge old screenshots
        if SCREENSHOTS_DIR.is_dir():
            for sf in list(SCREENSHOTS_DIR.glob("*.jpg")):
                try:
                    mtime = sf.stat().st_mtime
                    if days == 0 or (now_ts - mtime >= cutoff_seconds):
                        sf.unlink(missing_ok=True)
                        purged_screenshots += 1
                except Exception as e:
                    sys.stderr.write(f"[*] [Retention] Failed to delete screenshot {sf}: {e}\n")

        return {
            "ok": True,
            "retention_days": days,
            "purged_traces": purged_traces,
            "purged_screenshots": purged_screenshots
        }
