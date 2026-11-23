"""Unit test for Human Takeover Mode and Security Wall Detection.
Verifies detection of CAPTCHA/Turnstile and threading pause/resume lifecycle.
"""
from policy.takeover import HumanTakeoverManager


class MockPage:
    def __init__(self, url="https://shopee.co.th", title="Shopee", body=""):
        self.url = url
        self._title = title
        self._body = body

    def title(self):
        return self._title

    def evaluate(self, script):
        return self._body


def test_turnstile_detection():
    mgr = HumanTakeoverManager()
    page = MockPage(url="https://shopee.co.th/verify/traffic/error", title="Verify Traffic")
    detected, reason = mgr.detect_security_wall(page)
    assert detected is True
    assert "traffic/error" in reason
    print("PASS: Security wall detected in challenge URL")


def test_body_captcha_detection():
    mgr = HumanTakeoverManager()
    page = MockPage(url="https://shopee.co.th/login", body="กรุณายืนยันตัวตน เลื่อนจิ๊กซอว์เพื่อเข้าสู่ระบบ")
    detected, reason = mgr.detect_security_wall(page)
    assert detected is True
    assert "verification" in reason.lower()
    print("PASS: Security wall detected in page body text")


def test_pause_resume_lifecycle():
    mgr = HumanTakeoverManager()
    assert mgr.takeover_active is False
    assert mgr.wait_if_paused(timeout_s=0.1) is True

    # Trigger takeover
    mgr.trigger_takeover("User clicked Take Control button")
    assert mgr.takeover_active is True
    # Should now time out because it is paused
    assert mgr.wait_if_paused(timeout_s=0.1) is False

    # User completes challenge and clicks Resume
    mgr.resume_agent()
    assert mgr.takeover_active is False
    assert mgr.wait_if_paused(timeout_s=0.1) is True
    print("PASS: Takeover pause and resume lifecycle works correctly")


if __name__ == "__main__":
    test_turnstile_detection()
    test_body_captcha_detection()
    test_pause_resume_lifecycle()
    print("\nALL HUMAN TAKEOVER TESTS PASSED!")
