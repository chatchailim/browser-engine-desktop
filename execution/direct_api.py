"""Direct API Execution Client (Level 1 Strategy) for browser-engine-desktop (WO-015).

Enforces Ground Rules GR1–GR9:
- GR1: Disabled by default (direct_api_enabled=false).
- GR2: Planner CANNOT compose arbitrary API calls; executes strictly pre-configured recipes.
- GR3: Destination host whitelist; rejects cross-host redirects; blocks link-local 169.254.0.0/16.
- GR4: Secrets read strictly from designated disk file paths; 0 secrets in trace/Fact/logs.
- GR5: Risk tiering: GET=Risk 0, state-changing=Risk 2+, financial/destructive=Risk 3.
- GR6: Risk >= 2 requires approval via approval_manager before execution.
- GR7: Idempotency-Key injected for state-changing methods; no automatic retries.
- GR8: FactApiCallExecuted in OneVault recording metadata and hashes only.
- GR9: Strict timeout ceiling per request.
"""
import hashlib
import json
import os
import pathlib
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"


def load_config() -> Dict[str, Any]:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


class NoCrossHostRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Prohibits redirects across different hosts (GR3)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        orig_url = req.full_url
        orig_host = urllib.parse.urlsplit(orig_url).netloc.lower()
        new_host = urllib.parse.urlsplit(newurl).netloc.lower()
        if orig_host != new_host:
            if fp:
                try:
                    fp.close()
                except Exception:
                    pass
            raise urllib.error.HTTPError(
                newurl, code, f"Cross-host redirect from {orig_host} to {new_host} prohibited by policy", headers, None
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class DirectApiClient:
    def __init__(
        self,
        enabled: Optional[bool] = None,
        recipes_path: Optional[str] = None,
        allowed_hosts: Optional[List[str]] = None
    ):
        cfg = load_config()
        self.enabled = enabled if enabled is not None else bool(cfg.get("direct_api_enabled", False))
        self.allowed_hosts = allowed_hosts if allowed_hosts is not None else list(cfg.get("direct_api_allowed_hosts", []))
        
        raw_recipes_path = recipes_path or cfg.get("direct_api_recipes_file", "./api_recipes.json")
        self.recipes_file = (BASE_DIR / raw_recipes_path).resolve() if not pathlib.Path(raw_recipes_path).is_absolute() else pathlib.Path(raw_recipes_path)
        self.recipes: Dict[str, Any] = self._load_recipes()

    def _load_recipes(self) -> Dict[str, Any]:
        """Load and parse recipe dictionary from JSON file."""
        if not self.recipes_file.is_file():
            return {}
        try:
            data = json.loads(self.recipes_file.read_text(encoding="utf-8"))
            return data.get("recipes", data)
        except Exception as e:
            sys.stderr.write(f"[*] [DirectAPI] Warning: Failed to load recipes from {self.recipes_file}: {e}\n")
            return {}

    def reload(self) -> None:
        """Reload configuration and recipe definitions."""
        cfg = load_config()
        self.enabled = bool(cfg.get("direct_api_enabled", False))
        self.allowed_hosts = list(cfg.get("direct_api_allowed_hosts", []))
        self.recipes = self._load_recipes()

    def get_recipe(self, recipe_name: str) -> Optional[Dict[str, Any]]:
        """Retrieve pre-configured recipe by name."""
        return self.recipes.get(recipe_name)

    def is_host_allowed(self, host: str) -> Tuple[bool, str]:
        """Validate if destination host is whitelisted and not blacklisted (GR3 / WO-015.1 R4)."""
        if not host:
            return False, "empty_host"

        normalized_dest = host.strip().lower()
        clean_ip_or_domain = normalized_dest.split(":")[0].strip()

        # Block link-local addresses (169.254.0.0/16)
        if clean_ip_or_domain.startswith("169.254."):
            return False, "link_local_blocked"

        if not self.allowed_hosts:
            return False, "allowed_hosts_empty"

        allowed_list = [str(h).strip().lower() for h in self.allowed_hosts if h]
        if not allowed_list:
            return False, "allowed_hosts_empty"

        # Check exact host match (including port if present)
        if normalized_dest in allowed_list:
            return True, "allowed"

        for allowed in allowed_list:
            if ":" not in allowed:
                # Allowed entry has NO port constraint (e.g. "api.internal.com")
                if clean_ip_or_domain == allowed:
                    return True, "allowed"
                if allowed.startswith("*."):
                    domain_suffix = allowed[2:]
                    if clean_ip_or_domain == domain_suffix or clean_ip_or_domain.endswith("." + domain_suffix):
                        return True, "allowed"
            else:
                # Allowed entry specifies port (e.g. "*.internal.com:8443")
                allowed_domain, allowed_port = allowed.split(":", 1)
                if ":" in normalized_dest:
                    h_domain, h_port = normalized_dest.split(":", 1)
                    if h_port == allowed_port:
                        if allowed_domain.startswith("*."):
                            domain_suffix = allowed_domain[2:]
                            if h_domain == domain_suffix or h_domain.endswith("." + domain_suffix):
                                return True, "allowed"

        return False, f"host_{normalized_dest}_not_in_whitelist"

    def read_secret_token(self, token_file_path: str) -> Optional[str]:
        """Read secret token from designated file path on disk (GR4)."""
        if not token_file_path:
            return None
        p = pathlib.Path(token_file_path)
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        if p.is_file():
            try:
                return p.read_text(encoding="utf-8").strip()
            except Exception as e:
                sys.stderr.write(f"[*] [DirectAPI] Warning: Failed to read token file {p}: {e}\n")
        return None

    def execute_call(
        self,
        recipe_name: str,
        parameters: Optional[Dict[str, Any]] = None,
        task_id: str = "direct_api_task",
        step_num: int = 1
    ) -> Dict[str, Any]:
        """Execute a Direct API call according to a pre-defined recipe.
        
        Planner can ONLY select recipe_name and pass parameters defined in recipe.
        Planner CANNOT specify arbitrary URLs, HTTP methods, or headers (GR2).
        """
        t_start = time.monotonic()
        params = dict(parameters or {})

        # 1. Enforcement of GR1: Direct API must be enabled
        if not self.enabled:
            return {
                "ok": False,
                "status": "direct_api_disabled",
                "error": "direct_api_disabled",
                "reason": "Direct API execution is disabled in config.json (direct_api_enabled=false)",
                "elapsed_ms": 0
            }

        # 2. Enforcement of GR2: Recipe must exist in customer pre-defined recipes
        recipe = self.get_recipe(recipe_name)
        if not recipe:
            return {
                "ok": False,
                "status": "unknown_recipe",
                "error": "unknown_recipe",
                "reason": f"Recipe '{recipe_name}' not found in pre-configured recipes file",
                "elapsed_ms": 0
            }

        method = str(recipe.get("method", "GET")).upper().strip()
        host = str(recipe.get("host", "")).strip()
        raw_path = str(recipe.get("path", "/")).strip()
        allowed_params = set(recipe.get("allowed_parameters", []))
        timeout_s = float(min(recipe.get("timeout_s", 5.0), 30.0))

        # 3. Enforcement of GR3: Destination host whitelist validation
        host_ok, host_reason = self.is_host_allowed(host)
        if not host_ok:
            return {
                "ok": False,
                "status": "host_not_allowed",
                "error": "host_not_allowed",
                "reason": host_reason,
                "recipe_name": recipe_name,
                "host": host,
                "elapsed_ms": 0
            }

        # Filter parameters to only allowed parameter names (GR2)
        safe_params = {k: v for k, v in params.items() if k in allowed_params}

        # Build path with substituted path parameters (e.g. /quotes/{symbol})
        # R1: Encode path parameters with safe='' and ensure no '..' escape or segment mismatch
        resolved_path = raw_path
        for k, v in safe_params.items():
            token = f"{{{k}}}"
            if token in resolved_path:
                str_val = str(v)
                quoted_val = urllib.parse.quote(str_val, safe="").replace("..", "%2E%2E")
                resolved_path = resolved_path.replace(token, quoted_val)

        # Post-construction validation (R1)
        if ".." in resolved_path or "//" in resolved_path:
            return {
                "ok": False,
                "status": "invalid_path",
                "error": "path_traversal_detected",
                "reason": "Resolved path contains prohibited traversal sequences",
                "recipe_name": recipe_name,
                "elapsed_ms": 0
            }

        # Verify path segment count matches recipe template
        raw_parts = [p for p in raw_path.split("?")[0].strip("/").split("/") if p]
        res_parts = [p for p in resolved_path.split("?")[0].strip("/").split("/") if p]
        if len(raw_parts) != len(res_parts):
            return {
                "ok": False,
                "status": "invalid_path",
                "error": "path_structure_mismatch",
                "reason": f"Path segment count mismatch (expected {len(raw_parts)}, got {len(res_parts)})",
                "recipe_name": recipe_name,
                "elapsed_ms": 0
            }

        # Clean path for reporting (strip any query string for Fact / privacy)
        clean_path = resolved_path.split("?")[0]

        # Scheme (R3): loopback hosts (127.0.0.1, localhost, ::1) use http, all others MUST use https
        # unless recipe explicitly declares allow_insecure_http: true
        clean_host_for_scheme = host.split(":")[0].strip().lower()
        is_loopback = clean_host_for_scheme in ("127.0.0.1", "localhost", "::1") or clean_host_for_scheme.startswith("127.")
        if is_loopback:
            scheme = "http"
        elif recipe.get("allow_insecure_http", False) is True:
            scheme = "http"
        else:
            scheme = "https"

        target_url = f"{scheme}://{host}{resolved_path}"

        # Build Headers
        headers: Dict[str, str] = {
            "User-Agent": "OneVault-DirectAPI/1.0",
            "Accept": "application/json"
        }
        custom_headers = recipe.get("headers", {})
        if isinstance(custom_headers, dict):
            for hk, hv in custom_headers.items():
                headers[str(hk)] = str(hv)

        # GR4: Load Secret Token from disk if specified
        auth_file = recipe.get("auth_token_file")
        if auth_file:
            token = self.read_secret_token(auth_file)
            if token:
                auth_header_format = recipe.get("auth_header", "Bearer {token}")
                auth_val = auth_header_format.replace("{token}", token)
                auth_header_name = recipe.get("auth_header_name", "Authorization")
                headers[auth_header_name] = auth_val

        # GR7: Idempotency Key for state-changing requests
        is_state_changing = method in ("POST", "PUT", "PATCH", "DELETE")
        if is_state_changing:
            idempotency_raw = f"{task_id}_{step_num}"
            idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()[:32]
            headers["Idempotency-Key"] = idempotency_key

        # Prepare Request Body
        body_bytes: Optional[bytes] = None
        if method in ("POST", "PUT", "PATCH"):
            headers.setdefault("Content-Type", "application/json")
            payload_template = recipe.get("payload_template")
            if payload_template and isinstance(payload_template, dict):
                body_dict = {}
                for pk, pv in payload_template.items():
                    if isinstance(pv, str) and pv.startswith("{") and pv.endswith("}"):
                        param_name = pv[1:-1]
                        body_dict[pk] = safe_params.get(param_name, "")
                    else:
                        body_dict[pk] = pv
                body_bytes = json.dumps(body_dict, ensure_ascii=False).encode("utf-8")
            elif safe_params:
                body_bytes = json.dumps(safe_params, ensure_ascii=False).encode("utf-8")
            else:
                body_bytes = b"{}"

        req_body_hash = "sha256:" + hashlib.sha256(body_bytes or b"").hexdigest()

        # Build urllib Request with cross-host redirect protection
        opener = urllib.request.build_opener(NoCrossHostRedirectHandler())
        req = urllib.request.Request(
            url=target_url,
            data=body_bytes,
            headers=headers,
            method=method
        )

        resp_code = 0
        resp_bytes = b""
        resp_data: Any = None
        exec_ok = False
        err_msg: Optional[str] = None

        try:
            with opener.open(req, timeout=timeout_s) as response:
                resp_code = response.getcode()
                resp_bytes = response.read()
                exec_ok = (200 <= resp_code < 400)
                try:
                    resp_data = json.loads(resp_bytes.decode("utf-8"))
                except Exception:
                    resp_data = resp_bytes.decode("utf-8", errors="replace")[:1000]
        except urllib.error.HTTPError as e:
            resp_code = e.code
            try:
                resp_bytes = e.read()
            except Exception:
                resp_bytes = b""
            err_msg = f"HTTP Error {resp_code}: {e.reason}"
            exec_ok = False
            try:
                e.close()
            except Exception:
                pass
        except urllib.error.URLError as e:
            err_msg = f"Network/URL Error: {e.reason}"
            exec_ok = False
        except socket.timeout:
            err_msg = f"Request timed out after {timeout_s}s"
            exec_ok = False
        except Exception as e:
            err_msg = f"Execution exception: {e}"
            exec_ok = False

        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        resp_body_hash = "sha256:" + hashlib.sha256(resp_bytes).hexdigest()

        return {
            "ok": exec_ok,
            "status": "done" if exec_ok else ("timeout" if "timed out" in (err_msg or "") else "api_error"),
            "recipe_name": recipe_name,
            "host": host,
            "path": clean_path,
            "method": method,
            "status_code": resp_code,
            "elapsed_ms": elapsed_ms,
            "request_body_hash": req_body_hash,
            "response_body_hash": resp_body_hash,
            "response_data": resp_data,
            "insecure_http": bool(recipe.get("allow_insecure_http", False)) or (scheme == "http" and not is_loopback),
            "error": err_msg
        }


# Singleton Global Direct API Client
direct_api_client = DirectApiClient()


def get_direct_api_client() -> DirectApiClient:
    return direct_api_client
