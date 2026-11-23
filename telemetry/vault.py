"""OneVault Integration & Fact Spooling Engine for browser-engine-desktop (WO-012).
Enforces:
- Standalone by default (onevault_enabled=false).
- Contract C1 Actor validation (shared/c1).
- Contract C2 canonical RFC 8785 hashing (jcs-sha256-v1) (shared/c2).
- Non-blocking spooling queue (data/spool/) resilient to crashes and vault downtime.
- Privacy-first Fact generation: stripped URLs, SHA-256 hashes only, 0 raw data leaks.
"""
import datetime
import hashlib
import json
import os
import pathlib
import sys
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent.parent

# Ensure repo root in sys.path for shared/c1 and shared/c2
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from shared.c1.subject_id import parse_local_id, INSTANCE
from shared.c2.jcs import HASH_ALGORITHM_V1, fact_content_hash_v1, verify_fact


def load_config() -> Dict[str, Any]:
    cfg_file = BASE_DIR / "config.json"
    if cfg_file.is_file():
        try:
            return json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def now_utc_iso() -> str:
    """Return ISO 8601 UTC timestamp ending in Z."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


import threading

def sanitize_url(raw_url: str) -> str:
    """Sanitize URL by removing query strings, fragments, and userinfo (username/password) to eliminate PII/credentials (Ground Rule 22)."""
    if not raw_url or not isinstance(raw_url, str):
        return ""
    try:
        parts = urllib.parse.urlsplit(raw_url.strip())
        # Strip userinfo (username:password) from netloc
        if parts.hostname:
            netloc = f"{parts.hostname}:{parts.port}" if parts.port else parts.hostname
        elif parts.netloc:
            netloc = parts.netloc.split("@")[-1]
        else:
            netloc = ""
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except Exception:
        s = raw_url.split("?")[0].split("#")[0]
        if "@" in s:
            scheme, rest = s.split("://", 1) if "://" in s else ("", s)
            return (scheme + "://" if scheme else "") + rest.split("@")[-1]
        return s


class OneVaultManager:
    def __init__(
        self,
        enabled: Optional[bool] = None,
        vault_dir: Optional[str] = None,
        spool_dir: Optional[str] = None,
        max_spool_size: Optional[int] = None
    ):
        cfg = load_config()
        self.enabled = enabled if enabled is not None else bool(cfg.get("onevault_enabled", False))
        
        # Vault destination directory (default ./onevault or env ONEVAULT_DIR)
        raw_vault_dir = vault_dir or os.environ.get("ONEVAULT_DIR") or cfg.get("onevault_dir", "./onevault")
        self.vault_dir = pathlib.Path(raw_vault_dir).resolve() if not pathlib.Path(raw_vault_dir).is_absolute() else pathlib.Path(raw_vault_dir)

        # Local Spooling Queue directory
        raw_spool_dir = spool_dir or cfg.get("spool_dir", "./data/spool")
        self.spool_dir = (BASE_DIR / raw_spool_dir).resolve() if not pathlib.Path(raw_spool_dir).is_absolute() else pathlib.Path(raw_spool_dir)

        self.max_spool_size = int(max_spool_size or cfg.get("max_spool_size", 1000))
        self.last_synced_at: Optional[str] = None

    def get_c1_actor(self, env: Optional[Dict[str, str]] = None) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
        """Validate and resolve actor identity according to SPADA Contract C1.
        Returns:
            (actor_dict, error_message)
        """
        source_env = env if env is not None else os.environ
        instance = source_env.get("ONEVAULT_INSTANCE")
        actor_id = source_env.get("ONEVAULT_ACTOR")

        if not instance or not actor_id:
            return None, "C1: Missing ONEVAULT_INSTANCE or ONEVAULT_ACTOR environment variables."

        if not INSTANCE.fullmatch(instance):
            return None, f"C1: Invalid ONEVAULT_INSTANCE '{instance}' (must match {INSTANCE.pattern})."

        try:
            parse_local_id(actor_id)
        except TypeError as e:
            return None, f"C1: Invalid ONEVAULT_ACTOR '{actor_id}' ({e})."

        return {
            "system": "onevault",
            "instance": instance,
            "id": actor_id
        }, None

    def create_task_fact(
        self,
        task_id: str,
        url: str,
        goal: str,
        total_steps: int,
        max_risk_level: int,
        result: str,
        elapsed_ms: int,
        observation_hashes: Optional[List[str]] = None,
        answer_hash: Optional[str] = None,
        env: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Create FactWebTaskExecuted conforming to C1 and C2 standards (Requirement 32)."""
        if not self.enabled:
            return None

        actor, err = self.get_c1_actor(env)
        if not actor:
            sys.stderr.write(f"[*] [OneVault] FactWebTaskExecuted skipped: {err}\n")
            return None

        clean_url = sanitize_url(url)
        clean_obs = [h for h in (observation_hashes or []) if isinstance(h, str) and h]

        payload = {
            "taskId": task_id,
            "url": clean_url,
            "goal": str(goal),
            "totalSteps": int(total_steps),
            "maxRiskLevel": int(max_risk_level),
            "result": str(result),
            "elapsedMs": int(elapsed_ms),
            "observationHashes": clean_obs,
            "answerHash": str(answer_hash) if answer_hash else ""
        }

        fact = {
            "factId": f"fact-web-task-{uuid.uuid4().hex}",
            "factType": "FactWebTaskExecuted",
            "actor": actor,
            "subjectId": f"web-task:{task_id}",
            "occurredAt": now_utc_iso(),
            "payload": payload,
            "hashAlgorithm": HASH_ALGORITHM_V1
        }
        fact["contentHash"] = fact_content_hash_v1(fact)
        return fact

    def create_approval_fact(
        self,
        task_id: str,
        step_num: int,
        action_type: str,
        target: str,
        risk_level: int,
        approved_at_iso: Optional[str] = None,
        approver_actor: Optional[Dict[str, str]] = None,
        env: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Create FactWebActionApproved recording who approved a risky step and when (Requirement 32 & F9)."""
        if not self.enabled:
            return None

        actor = approver_actor
        if not actor or not isinstance(actor, dict) or "id" not in actor:
            actor, err = self.get_c1_actor(env or (approver_actor if isinstance(approver_actor, dict) else None))
            if not actor:
                sys.stderr.write(f"[*] [OneVault] FactWebActionApproved skipped: {err}\n")
                return None

        approved_time = approved_at_iso or now_utc_iso()

        payload = {
            "taskId": task_id,
            "stepNum": int(step_num),
            "actionType": str(action_type),
            "target": str(target),
            "riskLevel": int(risk_level),
            "approvedAt": approved_time,
            "approver": actor["id"]
        }

        fact = {
            "factId": f"fact-web-action-approved-{uuid.uuid4().hex}",
            "factType": "FactWebActionApproved",
            "actor": actor,
            "subjectId": f"web-task:{task_id}",
            "occurredAt": approved_time,
            "payload": payload,
            "hashAlgorithm": HASH_ALGORITHM_V1
        }
        fact["contentHash"] = fact_content_hash_v1(fact)
        return fact

    def create_erasure_fact(
        self,
        task_id: str,
        reason: str = "PDPA Right to Erasure request",
        erased_artifacts: Optional[List[str]] = None,
        erased_by_actor: Optional[Dict[str, str]] = None,
        env: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Create FactDataErased documenting compliant erasure under PDPA (Requirement 32 & F7)."""
        if not self.enabled:
            return None

        actor = erased_by_actor
        if not actor or not isinstance(actor, dict) or "id" not in actor:
            actor, err = self.get_c1_actor(env or (erased_by_actor if isinstance(erased_by_actor, dict) else None))
            if not actor:
                sys.stderr.write(f"[*] [OneVault] FactDataErased skipped: {err}\n")
                return None

        erased_time = now_utc_iso()

        payload = {
            "taskId": task_id,
            "erasedBy": actor["id"],
            "erasedAt": erased_time,
            "reason": str(reason),
            "erasedArtifacts": list(erased_artifacts or ["trace", "answers", "screenshots"])
        }

        fact = {
            "factId": f"fact-data-erased-{uuid.uuid4().hex}",
            "factType": "FactDataErased",
            "actor": actor,
            "subjectId": f"web-task:{task_id}",
            "occurredAt": erased_time,
            "payload": payload,
            "hashAlgorithm": HASH_ALGORITHM_V1
        }
        fact["contentHash"] = fact_content_hash_v1(fact)
        return fact

    def create_api_call_fact(
        self,
        task_id: str,
        step_num: int,
        recipe_name: str,
        host: str,
        path: str,
        method: str,
        status_code: int,
        elapsed_ms: int,
        risk_level: int,
        request_body_hash: str,
        response_body_hash: str,
        insecure_http: bool = False,
        env: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Create FactApiCallExecuted conforming to C1 and C2 standards (GR8 & WO-015).
        Records metadata and hashes only: 0 raw payload, 0 headers, 0 secrets.
        """
        if not self.enabled:
            return None

        actor, err = self.get_c1_actor(env)
        if not actor:
            sys.stderr.write(f"[*] [OneVault] FactApiCallExecuted skipped: {err}\n")
            return None

        clean_path = str(path).split("?")[0].strip()

        payload = {
            "taskId": str(task_id),
            "stepNum": int(step_num),
            "recipeName": str(recipe_name),
            "host": str(host),
            "path": clean_path,
            "method": str(method).upper(),
            "statusCode": int(status_code),
            "elapsedMs": int(elapsed_ms),
            "riskLevel": int(risk_level),
            "requestBodyHash": str(request_body_hash),
            "responseBodyHash": str(response_body_hash),
            "insecureHttp": bool(insecure_http)
        }

        fact = {
            "factId": f"fact-api-call-{uuid.uuid4().hex}",
            "factType": "FactApiCallExecuted",
            "actor": actor,
            "subjectId": f"web-task:{task_id}",
            "occurredAt": now_utc_iso(),
            "payload": payload,
            "hashAlgorithm": HASH_ALGORITHM_V1
        }
        fact["contentHash"] = fact_content_hash_v1(fact)
        return fact

    def create_desktop_window_read_fact(
        self,
        task_id: str,
        process_name: str,
        window_title_hash: str,
        element_count: int,
        elapsed_ms: int,
        structure_hash: str,
        occurred_at_iso: Optional[str] = None,
        env: Optional[Dict[str, str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Create FactDesktopWindowRead conforming to C1 and C2 standards (GR8 & WO-016).
        Records audit metadata only: process name, title hash, element count, structure hash.
        Zero plaintext user window text, zero screenshots.
        """
        if not self.enabled:
            return None

        actor, err = self.get_c1_actor(env)
        if not actor:
            sys.stderr.write(f"[*] [OneVault] FactDesktopWindowRead skipped: {err}\n")
            return None

        occurred_time = occurred_at_iso or now_utc_iso()

        payload = {
            "taskId": str(task_id),
            "processName": str(process_name),
            "windowTitleHash": str(window_title_hash),
            "elementCount": int(element_count),
            "elapsedMs": int(elapsed_ms),
            "structureHash": str(structure_hash)
        }

        fact = {
            "factId": f"fact-desktop-read-{uuid.uuid4().hex}",
            "factType": "FactDesktopWindowRead",
            "actor": actor,
            "subjectId": f"desktop-task:{task_id}",
            "occurredAt": occurred_time,
            "payload": payload,
            "hashAlgorithm": HASH_ALGORITHM_V1
        }
        fact["contentHash"] = fact_content_hash_v1(fact)
        return fact

    def spool_fact(self, fact: Dict[str, Any]) -> bool:
        """Write fact atomically to local spool queue (data/spool/<factId>.json).
        Never blocks the user task on failure.
        """
        if not fact or not isinstance(fact, dict):
            return False

        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)

            # Check queue capacity ceiling (Ground Rule 34)
            current_files = list(self.spool_dir.glob("*.json"))
            if len(current_files) >= self.max_spool_size:
                sys.stderr.write(f"[*] [OneVault Spool] Warning: Spool queue reached capacity ({self.max_spool_size}).\n")
                return False

            fact_id = fact.get("factId", f"fact-{uuid.uuid4().hex}")
            spool_path = self.spool_dir / f"{fact_id}.json"
            tmp_path = self.spool_dir / f"{fact_id}.tmp"

            tmp_path.write_text(json.dumps(fact, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, spool_path)
            return True
        except Exception as e:
            sys.stderr.write(f"[*] [OneVault Spool] Failed to spool fact: {e}\n")
            return False

    def _do_flush(self) -> Dict[str, int]:
        """Internal flush execution."""
        if not self.spool_dir.is_dir():
            return {"flushed": 0, "failed": 0, "pending": 0}

        spool_files = sorted(self.spool_dir.glob("*.json"))
        flushed_count = 0
        failed_count = 0

        for s_file in spool_files:
            try:
                fact_content = json.loads(s_file.read_text(encoding="utf-8"))
                fact_type = fact_content.get("factType", "Unknown")
                fact_id = fact_content.get("factId", s_file.stem)
                content_hash = fact_content.get("contentHash", "")

                if not content_hash or len(content_hash) < 10:
                    # Invalid fact in spool, remove
                    s_file.unlink(missing_ok=True)
                    continue

                safe_type = "".join(c if c.isalnum() else "-" for c in fact_type)
                sub_dir = content_hash[7:9]
                target_dir = self.vault_dir / "facts" / safe_type / sub_dir
                target_dir.mkdir(parents=True, exist_ok=True)

                target_path = target_dir / f"{fact_id}.json"
                tmp_target = target_dir / f"{fact_id}.{uuid.uuid4().hex}.tmp"

                tmp_target.write_text(json.dumps(fact_content, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(tmp_target, target_path)

                # Remove from spool once confirmed written
                s_file.unlink(missing_ok=True)
                flushed_count += 1
            except PermissionError:
                failed_count += 1
                try:
                    tmp_target.unlink(missing_ok=True)
                except Exception:
                    pass
                sys.stderr.write(f"[*] [OneVault Sync] Spool collision on {s_file.name}; retained for next retry.\n")
            except Exception as e:
                failed_count += 1
                # Leave in spool for next retry
                sys.stderr.write(f"[*] [OneVault Sync] Could not write {s_file.name} to vault: {e}\n")

        remaining = len(list(self.spool_dir.glob("*.json")))
        if flushed_count > 0:
            self.last_synced_at = now_utc_iso()

        return {
            "flushed": flushed_count,
            "failed": failed_count,
            "pending": remaining
        }

    def flush_spool(self, timeout: Optional[float] = None) -> Dict[str, int]:
        """Flush spooled facts from local queue to the OneVault central repository.
        Writes atomically to vault_dir/facts/<factType>/<ch[7:9]>/<factId>.json.
        Idempotent: Re-transmitting existing fact files does not duplicate records.
        If timeout is provided, runs in a daemon thread and returns without blocking if timeout expires.
        """
        if timeout is None or timeout <= 0:
            return self._do_flush()

        result_container: Dict[str, Any] = {}

        def target():
            try:
                result_container["result"] = self._do_flush()
            except Exception as e:
                result_container["error"] = e

        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout=timeout)

        if t.is_alive():
            sys.stderr.write(f"[*] [OneVault Sync] Spool flush timed out after {timeout}s; leaving facts in spool.\n")
            remaining = len(list(self.spool_dir.glob("*.json"))) if self.spool_dir.is_dir() else 0
            return {"flushed": 0, "failed": 1, "pending": remaining}

        if "result" in result_container:
            return result_container["result"]
        if "error" in result_container:
            sys.stderr.write(f"[*] [OneVault Sync] Spool flush error: {result_container['error']}\n")

        remaining = len(list(self.spool_dir.glob("*.json"))) if self.spool_dir.is_dir() else 0
        return {"flushed": 0, "failed": 1, "pending": remaining}

    def flush_spool_async(self, timeout: float = 30.0) -> threading.Thread:
        """Flush spooled facts in a background daemon thread so caller never blocks."""
        t = threading.Thread(target=self.flush_spool, kwargs={"timeout": timeout}, daemon=True)
        t.start()
        return t

    def get_status(self, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Return real-time connection, spool metrics, and C1 actor status."""
        pending_count = len(list(self.spool_dir.glob("*.json"))) if self.spool_dir.is_dir() else 0
        actor, err = self.get_c1_actor(env)

        return {
            "enabled": self.enabled,
            "vault_dir": str(self.vault_dir),
            "spool_dir": str(self.spool_dir),
            "spool_pending": pending_count,
            "last_synced_at": self.last_synced_at,
            "c1_actor": actor["id"] if actor else None,
            "c1_instance": actor["instance"] if actor else None,
            "c1_error": err
        }


# Global Manager Singleton
global_vault_manager = OneVaultManager()


def get_vault_manager() -> OneVaultManager:
    return global_vault_manager


def set_vault_manager(manager: OneVaultManager) -> None:
    global global_vault_manager
    global_vault_manager = manager
