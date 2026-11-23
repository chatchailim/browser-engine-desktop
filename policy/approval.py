"""Human Approval Manager for High-Risk Actions (Risk 2 & Risk 3).
Enforces mandatory safety gates before executing sensitive transactions.
"""
import threading
import time
from typing import Any, Dict, List, Optional


class ApprovalRequest:
    def __init__(
        self,
        task_id: str,
        step_num: int,
        action_type: str,
        target: str,
        risk_level: str,
        description: str
    ):
        self.task_id = task_id
        self.step_num = step_num
        self.action_type = action_type
        self.target = target
        self.risk_level = risk_level
        self.description = description
        self.created_at = time.time()
        self.event = threading.Event()
        self.status = "pending"  # pending, approved, rejected, cancelled


class ApprovalManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending: Dict[str, ApprovalRequest] = {}

    def request_approval(
        self,
        task_id: str,
        step_num: int,
        action_type: str,
        target: str,
        risk_level: str,
        description: str,
        timeout: float = 300.0
    ) -> bool:
        """Hold execution until explicit approval is granted or timeout expires."""
        key = f"{task_id}_{step_num}"
        req = ApprovalRequest(
            task_id=task_id,
            step_num=step_num,
            action_type=action_type,
            target=target,
            risk_level=risk_level,
            description=description
        )
        with self._lock:
            self._pending[key] = req

        # Wait for approval event or timeout (default 5 minutes / 300s)
        signaled = req.event.wait(timeout=timeout)

        with self._lock:
            self._pending.pop(key, None)

        if not signaled:
            req.status = "cancelled"
            return False

        return req.status == "approved"

    def approve(
        self,
        task_id: str,
        step_num: int,
        approver: Optional[Dict[str, str]] = None,
        env: Optional[Dict[str, str]] = None
    ) -> bool:
        """Grant approval for a pending risky step and issue FactWebActionApproved (F9)."""
        key = f"{task_id}_{step_num}"
        with self._lock:
            req = self._pending.get(key)
            if req and req.status == "pending":
                req.status = "approved"
                req.event.set()

                # Requirement 32 & F9: Emit FactWebActionApproved
                try:
                    import re
                    import sys
                    from telemetry.vault import get_vault_manager, now_utc_iso
                    vault = get_vault_manager()
                    if vault.enabled:
                        r_level = 2
                        try:
                            m = re.search(r"\d+", str(req.risk_level))
                            if m:
                                r_level = int(m.group(0))
                        except Exception:
                            r_level = 2

                        fact = vault.create_approval_fact(
                            task_id=task_id,
                            step_num=step_num,
                            action_type=req.action_type,
                            target=req.target,
                            risk_level=r_level,
                            approved_at_iso=now_utc_iso(),
                            approver_actor=approver,
                            env=env
                        )
                        if fact:
                            vault.spool_fact(fact)
                            vault.flush_spool_async()
                except Exception as e:
                    import sys
                    sys.stderr.write(f"[*] [OneVault] Approval fact skipped: {e}\n")

                return True
        return False

    approve_step = approve

    def reject(self, task_id: str, step_num: int) -> bool:
        """Explicitly reject a pending risky step."""
        key = f"{task_id}_{step_num}"
        with self._lock:
            req = self._pending.get(key)
            if req and req.status == "pending":
                req.status = "rejected"
                req.event.set()
                return True
        return False

    def get_pending(self, task_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """List active pending approvals."""
        with self._lock:
            res = []
            for req in self._pending.values():
                if task_id is None or req.task_id == task_id:
                    res.append({
                        "task_id": req.task_id,
                        "step_num": req.step_num,
                        "action_type": req.action_type,
                        "target": req.target,
                        "risk_level": req.risk_level,
                        "description": req.description,
                        "created_at": req.created_at,
                        "status": req.status
                    })
            return res


# Global Approval Manager instance
approval_manager = ApprovalManager()
