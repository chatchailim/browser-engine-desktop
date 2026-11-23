"""Tests: T3 (2026-11-23) — Planner ผ่าน SPADA ai-host-gateway (Vision/ตัววางแผนเล็กไม่เกี่ยว)."""
import importlib
import json
import pathlib
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

APP = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))


class _GW(BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode())
        _GW.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        out = json.dumps({"choices": [{"message": {"content": "{\"ok\":true}"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class PlannerGatewayT(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), _GW)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.mc = importlib.import_module("models.client")
        self._cfg = dict(self.mc.CFG)
        self.tmp = tempfile.TemporaryDirectory()
        self.key = pathlib.Path(self.tmp.name) / "gw.key"
        self.key.write_text("sah_test_tool_key\n", encoding="utf-8")
        _GW.seen.clear()

    def tearDown(self):
        self.mc.CFG.clear()
        self.mc.CFG.update(self._cfg)
        self.tmp.cleanup()

    def _set(self, **kw):
        self.mc.CFG.update(kw)

    def test_planner_uses_gateway_and_its_own_key(self):
        self._set(planner_gateway_url=f"http://127.0.0.1:{self.port}/v1", planner_gateway_key_path=str(self.key))
        c = self.mc.DGXVllmClient()
        r = c.chat_completion([{"role": "user", "content": "plan"}])
        self.assertEqual(r["endpoint_used"], f"http://127.0.0.1:{self.port}/v1")
        self.assertEqual(_GW.seen[0]["path"], "/v1/chat/completions")
        self.assertEqual(_GW.seen[0]["auth"], "Bearer sah_test_tool_key")
        allowed = {"model", "messages", "max_tokens", "temperature", "chat_template_kwargs"}
        self.assertLessEqual(set(_GW.seen[0]["body"]), allowed)

    def test_no_fallback_to_direct_when_gateway_down(self):
        self._set(planner_gateway_url="http://127.0.0.1:9/v1", planner_gateway_key_path=str(self.key),
                  ai_endpoints=[f"http://127.0.0.1:{self.port}/v1"])
        c = self.mc.DGXVllmClient(default_timeout=2)
        with self.assertRaises(Exception):
            c.chat_completion([{"role": "user", "content": "plan"}])
        self.assertEqual(_GW.seen, [])  # ไม่หลุดไปเรียกปลายทางสำรอง

    def test_missing_key_is_refused(self):
        self._set(planner_gateway_url=f"http://127.0.0.1:{self.port}/v1", planner_gateway_key_path="")
        with self.assertRaises(self.mc.PlannerGatewayError):
            self.mc.DGXVllmClient().chat_completion([{"role": "user", "content": "plan"}])
        self.assertEqual(_GW.seen, [])

    def test_response_format_is_refused_via_gateway(self):
        self._set(planner_gateway_url=f"http://127.0.0.1:{self.port}/v1", planner_gateway_key_path=str(self.key))
        with self.assertRaises(ValueError):
            self.mc.DGXVllmClient().chat_completion([{"role": "user", "content": "x"}], response_format={"type": "json_object"})

    def test_without_gateway_config_route_is_direct(self):
        self.mc.CFG.pop("planner_gateway_url", None)
        r = self.mc.planner_route()
        self.assertFalse(r["via_gateway"])
        self.assertEqual(r["url"], str(self.mc.CFG.get("vllm_url", "http://192.168.1.108/vllm/v1")).rstrip("/"))


if __name__ == "__main__":
    unittest.main()
