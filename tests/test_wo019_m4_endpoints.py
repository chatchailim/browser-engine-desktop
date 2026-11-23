"""Tests: WO-019 M4 ai_endpoints fallback — ตัวหลักตาย → ตัวรองรับ · HTTP error ไม่ fallback · ทุกตัวตาย = raise เดิม."""
import json
import pathlib
import socket
import sys
import unittest
from unittest import mock

BASE = pathlib.Path(__file__).resolve().parent.parent  # repo แยกแล้ว — ห้ามชี้ path เดิมใน ai-platform-kit
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))


def _fake_urlopen(payload_body: bytes, status=200):
    """สร้าง fake urlopen คืน response จำลอง."""
    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload_body
    return R()


class M4Fallback(unittest.TestCase):
    def setUp(self):
        from models import client as client_mod
        importlib = __import__("importlib")
        self.c = importlib.reload(client_mod)
        self.cl = self.c.DGXVllmClient(base_url="http://primary.local/vllm/v1")
        self.orig_cfg = dict(self.c.CFG)
        self.c.CFG["ai_endpoints"] = ["http://primary.local/vllm/v1", "http://backup.local/vllm/v1"]

    def tearDown(self):
        self.c.CFG.clear()
        self.c.CFG.update(self.orig_cfg)

    def test_primary_works_no_fallback(self):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        seen = []

        def fake_urlopen(req, timeout):
            seen.append(req.full_url)
            return _fake_urlopen(body)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = self.cl.chat_completion([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(res["endpoint_used"], "http://primary.local/vllm/v1")
        self.assertEqual(len(seen), 1)

    def test_primary_timeout_fallback_backup(self):
        good = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        seen = []

        def fake_urlopen(req, timeout):
            seen.append(req.full_url)
            if "primary" in req.full_url:
                raise socket.timeout("timed out")
            return _fake_urlopen(good)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = self.cl.chat_completion([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(res["endpoint_used"], "http://backup.local/vllm/v1")
        self.assertEqual(len(seen), 2)

    def test_http_error_does_not_fallback(self):
        import urllib.error

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, b"bad key")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                self.cl.chat_completion([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertIn("401", str(ctx.exception))

    def test_all_down_raises_timeout(self):
        def fake_urlopen(req, timeout):
            raise socket.timeout("timed out")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(TimeoutError):
                self.cl.chat_completion([{"role": "user", "content": "x"}], max_tokens=5)

    def test_empty_endpoints_list_uses_main_only(self):
        self.c.CFG["ai_endpoints"] = []
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        seen = []

        def fake_urlopen(req, timeout):
            seen.append(req.full_url)
            return _fake_urlopen(body)
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = self.cl.chat_completion([{"role": "user", "content": "x"}], max_tokens=5)
        self.assertEqual(len(seen), 1)
        self.assertEqual(res["endpoint_used"], "http://primary.local/vllm/v1")


if __name__ == "__main__":
    unittest.main()