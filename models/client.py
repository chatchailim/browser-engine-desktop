"""High-performance vLLM Client for DGX Spark (Qwen3.8-27B).
Optimized for low-latency inference over LAN with connection pooling.
"""
import json
import os
import pathlib
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

CONFIG_FILE = pathlib.Path(__file__).resolve().parent.parent / "config.json"


def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "vllm_url": "http://192.168.1.108/vllm/v1",
        "vllm_key_path": os.path.join(os.path.expanduser("~"), ".dgx", "vllm-api-key"),
        "model": "Qwen3.8-27B",
        "planner_timeout_seconds": 90
    }


CFG = load_config()


def get_vllm_key() -> str:
    kp = pathlib.Path(CFG.get("vllm_key_path", os.path.join(os.path.expanduser("~"), ".dgx", "vllm-api-key")))
    if kp.is_file():
        return kp.read_text(encoding="utf-8").strip()
    return os.environ.get("VLLM_KEY", "")


class DGXVllmClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        default_timeout: Optional[float] = None
    ):
        self.base_url = (base_url or CFG.get("vllm_url", "http://192.168.1.108/vllm/v1")).rstrip("/")
        self.model = model or CFG.get("model", "Qwen3.8-27B")
        self.api_key = get_vllm_key()
        self.default_timeout = default_timeout if default_timeout is not None else float(CFG.get("planner_timeout_seconds", 90))

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int = 250,
        temperature: float = 0.0,
        response_format: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """Send chat completion to vLLM on DGX Spark."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "chat_template_kwargs": {"enable_thinking": False}
        }
        if response_format:
            payload["response_format"] = response_format

        data = json.dumps(payload).encode("utf-8")
        call_timeout = float(timeout if timeout is not None else self.default_timeout)

        # WO-019 M4: ai_endpoints (สำรองหลายสมอง) — ตัวหลักก่อน, เจอ timeout/conn error = ลอง endpoint ถัดไป
        # กติกา: HTTP error (key/โมเดลผิด) ไม่ fallback (ปัญหาตั้งค่า — ให้เห็นเหตุจริง) · fail ทุก endpoint = raise เดิม
        endpoints = [self.base_url]
        for u in (CFG.get("ai_endpoints") or []):
            if isinstance(u, str) and u.strip().rstrip("/") != self.base_url:
                endpoints.append(u.strip().rstrip("/"))
        last_err = None
        for ep in endpoints:
            req = urllib.request.Request(f"{ep}/chat/completions", data=data, headers=headers, method="POST")
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=call_timeout) as resp:
                    raw = resp.read().decode("utf-8")
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    res = json.loads(raw)
                    res["client_latency_ms"] = elapsed_ms
                    res["endpoint_used"] = ep
                    return res
            except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
                last_err = e
                continue  # ตัวนี้ตาย (ตายตามเวลา/เครือข่าย) — จ่ายตัวสำรอง
            except urllib.error.HTTPError as he:
                err_body = he.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"DGX vLLM HTTP {he.code}: {err_body}") from he
            except Exception as e:
                last_err = e
                continue
        if isinstance(last_err, socket.timeout) or isinstance(last_err, TimeoutError) or (isinstance(last_err, urllib.error.URLError) and isinstance(last_err.reason, socket.timeout)):
            raise TimeoutError(f"planner_timeout: ทุก endpoint ไม่ตอบภายใน {call_timeout}s") from last_err
        raise RuntimeError(f"DGX vLLM connection error: {last_err}")

    def extract_text_or_json(self, completion_response: Dict[str, Any]) -> str:
        choices = completion_response.get("choices", [])
        if not choices:
            return ""
        msg = choices[0].get("message", {})
        return (msg.get("content") or "").strip()
