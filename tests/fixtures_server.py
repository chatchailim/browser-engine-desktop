"""Local HTTP Server Fixtures for WO-009 C1 & T4-T8 tests.
Serves mock pages on 127.0.0.1 to deterministically test safety, risk gates, and recovery.
"""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
import urllib.parse


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Quiet logging in tests

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        server: FixtureServer = self.server

        if path == "/api/clicks":
            self._send_json(server.event_log)
            return

        if path == "/api/visual_click":
            server.visual_clicks += 1
            server.event_log.append("visual_clicked")
            self._send_html("<h1>Visual Click Recorded</h1>")
            return

        if path == "/api/financial_click":
            server.financial_clicks += 1
            server.event_log.append("financial_clicked")
            self._send_html("<h1>Financial Click Recorded</h1>")
            return

        if path == "/api/order_click":
            server.order_clicks += 1
            server.event_log.append("order_clicked")
            self._send_html("<h1>Order Confirmed</h1>")
            return

        if path == "/api/cookie_action":
            btn = qs.get("btn", ["unknown"])[0]
            server.cookie_clicks.append(btn)
            server.event_log.append(f"cookie_{btn}")
            self._send_html(f"Cookie choice: {btn}")
            return

        if path == "/search":
            html = """<!DOCTYPE html>
            <html>
            <body>
                <h1>Search Engine</h1>
                <form action="/search_results" method="GET">
                    <input type="text" name="q" placeholder="Enter keyword" id="search-box">
                    <button type="submit" id="search-btn">Search</button>
                </form>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/search_results":
            q = qs.get("q", [""])[0]
            html = f"""<!DOCTYPE html>
            <html>
            <body>
                <h1>Search Results</h1>
                <div class="result-item">Found result for: {q}</div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/mock_article":
            html = """<!DOCTYPE html>
            <html lang="th">
            <head><meta charset="UTF-8"><title>กรุงเทพมหานคร - สารานุกรมจำลอง</title></head>
            <body>
                <h1>กรุงเทพมหานคร</h1>
                <p>กรุงเทพมหานคร เป็นเมืองหลวงและนครที่มีประชากรมากที่สุดของประเทศไทย</p>
                <div class="infobox">
                    <table>
                        <tr><th>ประชากร</th><td>5,527,948 คน</td></tr>
                        <tr><th>พื้นที่</th><td>1,568.7 ตร.กม.</td></tr>
                    </table>
                </div>
                <div class="content">ข้อมูลทั่วไปและการบริหารราชการส่วนท้องถิ่น</div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/mock_portal":
            html = """<!DOCTYPE html>
            <html lang="th">
            <head><meta charset="UTF-8"><title>ธนาคารแห่งประเทศไทย (จำลอง)</title></head>
            <body>
                <h1>ธนาคารแห่งประเทศไทย</h1>
                <p>ข่าวสารและมาตรการทางการเงิน</p>
                <div class="news-list">
                    <article>รายงานสรุปผลการจัดซื้อจัดจ้าง ประจำปี 2569</article>
                </div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/order":
            html = """<!DOCTYPE html>
            <html>
            <body>
                <h1>Checkout Page</h1>
                <p>Total: $99.00</p>
                <button id="btn-place-order" onclick="fetch('/api/order_click').then(() => { document.getElementById('msg').innerText = 'Order Submitted'; })">Place Order</button>
                <div id="msg"></div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/cookie_dual":
            html = """<!DOCTYPE html>
            <html>
            <body>
                <h1>Dual Cookie Banner</h1>
                <div id="cookie-banner" style="background:#eee; padding:20px;">
                    <p>We use cookies</p>
                    <button id="btn-reject" onclick="fetch('/api/cookie_action?btn=reject')">Reject all</button>
                    <button id="btn-accept" onclick="fetch('/api/cookie_action?btn=accept')">Accept all</button>
                </div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/cookie_accept_only":
            html = """<!DOCTYPE html>
            <html>
            <body>
                <h1>Accept Only Cookie Banner</h1>
                <div id="cookie-banner" style="background:#eee; padding:20px;">
                    <p>We use cookies</p>
                    <button id="btn-accept" onclick="fetch('/api/cookie_action?btn=accept')">Accept all</button>
                </div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/checkout_cookie_ok":
            html = """<!DOCTYPE html>
            <html>
            <body>
                <h1>Checkout & Payment Page</h1>
                <p>Total: $150.00</p>
                <div id="cookie-banner" style="background:#eee; padding:20px;">
                    <p>คุกกี้เว็บไซต์</p>
                    <button id="btn-agree" onclick="fetch('/api/cookie_action?btn=ok')">ตกลง</button>
                </div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/delete_modal":
            html = """<!DOCTYPE html>
            <html>
            <body>
                <h1>Account Settings</h1>
                <div class="modal" style="border:2px solid red; padding:20px;">
                    <h2>ยืนยันการลบบัญชี</h2>
                    <p>การลบข้อมูลนี้ไม่สามารถกู้คืนได้</p>
                    <button class="modal-close" id="btn-del">ลบบัญชีผู้ใช้</button>
                </div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/turnstile":
            html = """<!DOCTYPE html>
            <html>
            <body>
                <h1>Cloudflare Verification Required</h1>
                <iframe src="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2/dummy" width="300" height="65"></iframe>
                <div id="protected-content">Secret Data: 42</div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/turnstile_cleared":
            html = """<!DOCTYPE html>
            <html>
            <body>
                <h1>Welcome human</h1>
                <div id="protected-content">Secret Data: 42</div>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/no_target":
            html = """<!DOCTYPE html>
            <html>
            <body>
                <h1>Simple Page</h1>
                <p>No buttons or matching targets here.</p>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/visual_button":
            html = """<!DOCTYPE html>
            <html>
            <body style="margin:0; padding:20px;">
                <h1>Visual Only Target Page</h1>
                <p>This button is rendered on a canvas without DOM text or accessibility nodes.</p>
                <canvas id="visual-btn" width="160" height="50" style="cursor:pointer; display:block; margin-top:20px;"
                    onclick="fetch('/api/visual_click').then(() => { document.getElementById('msg').innerText = 'Visual Clicked'; })">
                </canvas>
                <div id="msg"></div>
                <script>
                    const cv = document.getElementById('visual-btn');
                    const ctx = cv.getContext('2d');
                    ctx.fillStyle = '#4CAF50';
                    ctx.fillRect(0, 0, 160, 50);
                    ctx.fillStyle = '#FFFFFF';
                    ctx.font = '18px sans-serif';
                    ctx.fillText('Action Button', 20, 32);
                </script>
            </body>
            </html>"""
            self._send_html(html)
            return

        if path == "/visual_financial":
            html = """<!DOCTYPE html>
            <html>
            <body style="margin:0; padding:20px;">
                <h1>Checkout Payment Gateway</h1>
                <p>Total: $250.00</p>
                <canvas id="financial-canvas" width="180" height="55" style="cursor:pointer; display:block; margin-top:20px;"
                    onclick="fetch('/api/financial_click').then(() => { document.getElementById('msg').innerText = 'Paid'; })">
                </canvas>
                <div id="msg"></div>
                <script>
                    const cv = document.getElementById('financial-canvas');
                    const ctx = cv.getContext('2d');
                    ctx.fillStyle = '#D32F2F';
                    ctx.fillRect(0, 0, 180, 55);
                    ctx.fillStyle = '#FFFFFF';
                    ctx.font = 'bold 20px sans-serif';
                    ctx.fillText('Confirm Pay', 25, 35);
                </script>
            </body>
            </html>"""
            self._send_html(html)
            return

        self._send_html("404 Not Found", code=404)

    def _send_html(self, content: str, code: int = 200):
        data = content.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, data, code: int = 200):
        import json
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FixtureServer(ThreadingHTTPServer):
    def __init__(self, port: int = 0):
        super().__init__(("127.0.0.1", port), FixtureHandler)
        self.order_clicks = 0
        self.visual_clicks = 0
        self.financial_clicks = 0
        self.cookie_clicks: List[str] = []
        self.event_log: List[str] = []


def run_fixture_server() -> FixtureServer:
    server = FixtureServer(port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
