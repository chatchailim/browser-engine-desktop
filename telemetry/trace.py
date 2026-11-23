import datetime
import hashlib
import json
import pathlib
from typing import Any, Dict, List, Optional
from models.schemas import ActionTrace

TRACES_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "traces"


class TelemetryCollector:
    def __init__(self, trace_id: str):
        self.trace_id = trace_id
        self.traces: List[ActionTrace] = []
        self.ttfa_ms: Optional[int] = None
        self.start_time: Optional[float] = None
        self.jsonl_file = TRACES_DIR / f"{self.trace_id}.jsonl"
        self.observation_hashes: List[str] = []
        self.answer_hash: Optional[str] = None

    def record_trace(self, trace: ActionTrace) -> None:
        if self.ttfa_ms is None and trace.latency_breakdown_ms.get("execution", 0) > 0:
            self.ttfa_ms = trace.total_ms
        self.traces.append(trace)

    def write_jsonl_step(
        self,
        step_num: int,
        observation: str,
        strategy: str,
        confidence: float,
        action_type: str,
        target_desc: str,
        value: Optional[str] = None,
        is_sensitive: bool = False,
        latency_breakdown: Optional[Dict[str, int]] = None,
        result: str = "SUCCESS",
        error: Optional[str] = None,
        screenshot_hash: Optional[str] = None
    ) -> None:
        """Write single step record to data/traces/<task_id>.jsonl (Requirement B7).
        Enforces SHA-256 hashing only; raw image bytes are strictly excluded.
        """
        TRACES_DIR.mkdir(parents=True, exist_ok=True)

        obs_hash = hashlib.sha256(observation.encode("utf-8")).hexdigest()
        self.observation_hashes.append(obs_hash)

        val_logged = None
        if value:
            # If Risk >= 1, store sha256 instead of plaintext
            if is_sensitive:
                val_logged = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
            else:
                val_logged = str(value)

        lat = latency_breakdown or {}
        tot = lat.get("total", sum(lat.values()))

        decision_meta = {
            "strategy": strategy,
            "confidence": confidence
        }
        if screenshot_hash:
            decision_meta["screenshot_hash"] = screenshot_hash

        record = {
            "task_id": self.trace_id,
            "step_num": step_num,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "observation_hash": obs_hash,
            "decision": decision_meta,
            "action": {
                "type": action_type,
                "target": target_desc,
                "value": val_logged
            },
            "latency_ms": {
                "planning": lat.get("planning", 0),
                "execution": lat.get("execution", 0),
                "verification": lat.get("verification", 0),
                "total": tot
            },
            "result": result,
            "error": error
        }

        try:
            with open(self.jsonl_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def write_plan_step(self, goal: str, context: Optional[Dict[str, Any]] = None,
                        planning_ms: int = 0) -> None:
        """WO-019 M2/M4 proof: ขั้นวางแผนสำเร็จลง trace เดียวกับขั้นอื่น — พิสูจน์ย้อนได้ว่า
        planner ใช้บริบท/สมองตัวไหนจริง ณ รอบนั้น (detail ลง `value` — ช่องเดียวที่ trace เก็บ
        ข้อความจริง; observation ถูก hash ทิ้งข้อความเสมอ)."""
        ctx = context or {}
        biz = ctx.get("business_context") or ""
        detail = {
            "endpoint_used": ctx.get("endpoint_used") or "",
            "tier_model": ctx.get("tier_model") or "",
            "tier_fallback_reason": ctx.get("tier_fallback_reason") or "",
            "business_context_sha256": hashlib.sha256(biz.encode("utf-8")).hexdigest() if biz else "",
            "business_context_chars": len(biz),
            "planning_ms": int(planning_ms),
        }
        self.write_jsonl_step(
            step_num=0,
            observation=json.dumps(detail, ensure_ascii=False),
            strategy="planner",
            confidence=1.0,
            action_type="plan",
            target_desc="planner",
            value=json.dumps(detail, ensure_ascii=False),
            latency_breakdown={"planning": int(planning_ms)},
            result="SUCCESS",
        )

    def get_summary_metrics(self) -> Dict[str, Any]:
        """Aggregate performance KPIs across all executed actions."""
        total_actions = len(self.traces)
        if total_actions == 0:
            return {
                "trace_id": self.trace_id,
                "total_actions": 0,
                "ttfa_ms": 0,
                "strategies": {},
                "avg_action_ms": 0,
                "success_rate": 1.0
            }

        strategies: Dict[str, int] = {}
        total_ms = 0
        success_count = 0

        for t in self.traces:
            strategies[t.strategy] = strategies.get(t.strategy, 0) + 1
            total_ms += t.total_ms
            if t.result == "SUCCESS":
                success_count += 1

        avg_ms = int(total_ms / total_actions)
        success_rate = round(success_count / float(total_actions), 3)

        return {
            "trace_id": self.trace_id,
            "total_actions": total_actions,
            "ttfa_ms": self.ttfa_ms or 0,
            "strategies": strategies,
            "avg_action_ms": avg_ms,
            "success_rate": success_rate,
            "traces": [
                {
                    "step": t.step_num,
                    "action": t.action_type,
                    "strategy": t.strategy,
                    "confidence": t.confidence,
                    "target": t.target_desc,
                    "total_ms": t.total_ms,
                    "result": t.result,
                    "error": t.error
                }
                for t in self.traces
            ]
        }

    def set_answer(self, answer: str) -> None:
        """Record SHA-256 hash of extracted task answer (0 raw data in telemetry/fact)."""
        if answer:
            self.answer_hash = hashlib.sha256(str(answer).encode("utf-8")).hexdigest()

    def get_observation_hashes(self) -> List[str]:
        return list(self.observation_hashes)

    def get_answer_hash(self) -> Optional[str]:
        return self.answer_hash
