"""Automated Test Suite for README API Endpoints Integrity (Requirement R2 / Test N1, N2).
Validates that every /api/... endpoint documented across the entire README.md exists and is handled in server.py (no 404s).
"""
import json
import pathlib
import re
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import server
from policy.approval import ApprovalRequest


class TestReadmeEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme_path = pathlib.Path(__file__).resolve().parent.parent / "README.md"
        cls.assertTrue(cls.readme_path.is_file(), "README.md not found")

        # Pick free port for test server
        cls.port = 18399
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), server.InteractiveHandler)
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.httpd.shutdown()
            cls.httpd.server_close()
        except Exception:
            pass

    def extract_all_readme_endpoints(self, readme_text: str):
        """Extract all (method, endpoint, line_no) occurrences from entire README.md (not just table)."""
        endpoints = []
        for line_no, line in enumerate(readme_text.splitlines(), 1):
            # Skip generic doc patterns like /api/*
            for m in re.finditer(r'/api/[a-zA-Z0-9_/<>-]+', line):
                raw_path = m.group(0).rstrip('.,;:"\')')
                if raw_path == "/api/*" or raw_path.endswith("/*"):
                    continue
                prefix = line[:m.start()].strip()
                method_match = re.search(r'\b(GET|POST|PUT|DELETE)\b\s*`?$', prefix, re.IGNORECASE)
                if method_match:
                    method = method_match.group(1).upper()
                else:
                    if any(raw_path.startswith(p) for p in ['/api/status', '/api/vault/status', '/api/tasks', '/api/task/', '/api/approvals']) and not any(raw_path.startswith(p) for p in ['/api/task/1', '/api/task_']):
                        method = 'GET'
                    else:
                        method = 'POST'
                endpoints.append((method, raw_path, line_no))
        return endpoints

    def _call_endpoint(self, method: str, path: str, payload=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-Desktop-Token", server.EXPECTED_TOKEN)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_all_readme_endpoints_exist_in_server(self):
        """N1: Every /api/... endpoint across the entire README must be recognized by server.py (no 404)."""
        content = self.readme_path.read_text(encoding="utf-8")
        endpoints = self.extract_all_readme_endpoints(content)
        self.assertGreaterEqual(len(endpoints), 15, f"Expected at least 15 endpoints across README, found {len(endpoints)}")

        # Seed test data so parametric endpoints find target objects
        test_task_id = "test_readme_task_1"
        server.TASKS[test_task_id] = {
            "id": test_task_id,
            "url": "https://example.com",
            "goal": "Verify readme endpoint",
            "status": "completed",
            "steps": []
        }
        server.approval_manager._pending[f"{test_task_id}_1"] = ApprovalRequest(
            task_id=test_task_id, step_num=1, action_type="CLICK", target="#buy", risk_level="Risk 2", description="test"
        )
        server.approval_manager._pending[f"{test_task_id}_2"] = ApprovalRequest(
            task_id=test_task_id, step_num=2, action_type="CLICK", target="#buy", risk_level="Risk 2", description="test"
        )
        server.approval_manager._pending[f"{test_task_id}_3"] = ApprovalRequest(
            task_id=test_task_id, step_num=3, action_type="CLICK", target="#buy", risk_level="Risk 2", description="test"
        )

        failures = []
        for method, raw_path, line_no in endpoints:
            # Normalize path parameters and examples
            call_path = raw_path.replace("<task_id>", test_task_id)
            if "<task>/<step>" in call_path:
                step = 1 if "approve" in call_path else 2
                call_path = call_path.replace("<task>/<step>", f"{test_task_id}/{step}")
            elif "task_1/3" in call_path:
                call_path = call_path.replace("task_1/3", f"{test_task_id}/3")

            payload = None
            if method == "POST":
                if "approve" in call_path or "reject" in call_path:
                    parts = call_path.split("/")
                    if len(parts) >= 2:
                        try:
                            s_num = int(parts[-1])
                            t_id = parts[-2]
                            server.approval_manager._pending[f"{t_id}_{s_num}"] = ApprovalRequest(
                                task_id=t_id, step_num=s_num, action_type="CLICK", target="#btn", risk_level="Risk 2", description="test"
                            )
                        except ValueError:
                            pass
                elif "task" in call_path and not ("approve" in call_path or "reject" in call_path or "erase" in call_path):
                    payload = {"url": "https://example.com", "goal": "test"}
                elif "takeover" in call_path:
                    payload = {"reason": "test takeover"}
                elif "erase" in call_path:
                    payload = {"task_id": test_task_id}
                elif "purge" in call_path:
                    payload = {"retention_days": 90}
                elif "open-browser" in call_path:
                    payload = {"url": "https://example.com"}
                else:
                    payload = {}

            status, body = self._call_endpoint(method, call_path, payload)
            if status == 404:
                failures.append(f"Line {line_no}: {method} {raw_path} -> 404 (Route not found in server.py! Response: {body})")

        self.assertEqual(failures, [], f"Found 404 errors for documented endpoints:\n" + "\n".join(failures))

    def test_detector_catches_bogus_prose_endpoint(self):
        """N2: Verify the detector catches non-existent endpoints located in prose text."""
        bogus_prose_content = """
        ### Test Section
        ตัวอย่างการลบประวัติงานผ่าน API POST /api/vault/erase และ POST /api/vault/purge
        """
        extracted = self.extract_all_readme_endpoints(bogus_prose_content)
        self.assertEqual(len(extracted), 2)
        failures = []
        for method, raw_path, line_no in extracted:
            status, _ = self._call_endpoint(method, raw_path, {})
            if status == 404:
                failures.append(raw_path)
        self.assertEqual(failures, ["/api/vault/erase", "/api/vault/purge"])


if __name__ == "__main__":
    unittest.main()
