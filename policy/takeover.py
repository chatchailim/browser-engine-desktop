"""Human Takeover Mode and CAPTCHA / Anti-Bot Detection.
Safely pauses autonomous agent execution upon encountering security walls,
releasing controls to the user and resuming seamlessly when cleared.
"""
import re
import threading
from typing import Any, Optional, Tuple

from urllib.parse import urlparse

BOT_WALL_DETECTOR_JS = """() => {
    // 1. Specific anti-bot iframes: Cloudflare Turnstile, reCAPTCHA, hCaptcha
    const iframes = Array.from(document.querySelectorAll('iframe'));
    for (const f of iframes) {
        const src = (f.src || f.getAttribute('src') || '').toLowerCase();
        if (src.includes('challenges.cloudflare.com')) {
            return { detected: true, reason: 'Cloudflare Turnstile challenge iframe detected' };
        }
        if (src.includes('recaptcha') || src.includes('google.com/recaptcha')) {
            return { detected: true, reason: 'Google reCAPTCHA iframe detected' };
        }
        if (src.includes('hcaptcha.com')) {
            return { detected: true, reason: 'hCaptcha challenge iframe detected' };
        }
    }

    // 2. Cloudflare 'Just a moment' challenge page
    const title = (document.title || '').trim().toLowerCase();
    if (title.includes('just a moment')) {
        const isCf = document.querySelector('#cf-wrapper, #challenge-running, #cf-challenge-running, #challenge-form, .cf-browser-verification, .ray-id') !== null;
        if (isCf) {
            return { detected: true, reason: "Cloudflare 'Just a moment' challenge detected" };
        }
    }

    return { detected: false, reason: null };
}"""


class HumanTakeoverManager:
    def __init__(self):
        self.takeover_active = False
        self.pause_event = threading.Event()
        self.pause_event.set()  # set() means not paused (running)
        self.reason: Optional[str] = None

    def detect_security_wall(self, page: Any) -> Tuple[bool, Optional[str]]:
        """Check whether current page is displaying an anti-bot challenge or CAPTCHA based on specific signals."""
        try:
            url = page.url or ""
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()

            # Signal 1: Shopee path verify
            if "shopee." in host and "/verify" in path:
                return True, f"Shopee verification challenge detected: {url}"

            # Signal 2 & 3: iframes (challenges.cloudflare.com, recaptcha, hcaptcha) & Cloudflare challenge
            res = page.evaluate(BOT_WALL_DETECTOR_JS)
            if isinstance(res, dict) and res.get("detected"):
                return True, res.get("reason")
            elif isinstance(res, str):
                if re.search(r"เลื่อนจิ๊กซอว์|หมุนภาพ|puzzle", res, re.I):
                    return True, "Security verification text detected in page content"
        except Exception:
            pass
        return False, None

    def trigger_takeover(self, reason: str = "Human takeover requested") -> None:
        """Pause agent execution and signal frontend for human intervention."""
        self.takeover_active = True
        self.reason = reason
        self.pause_event.clear()

    def resume_agent(self) -> None:
        """User completed challenge; resume agent execution."""
        self.takeover_active = False
        self.reason = None
        self.pause_event.set()

    def wait_if_paused(self, timeout_s: float = 300.0) -> bool:
        """Block until takeover is cleared or timeout occurs."""
        return self.pause_event.wait(timeout=timeout_s)
