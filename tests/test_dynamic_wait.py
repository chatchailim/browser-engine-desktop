"""Unit test for Dynamic Wait vs Fixed Sleep.
Verifies that wait_until returns immediately when condition evaluates to True.
"""
import time
from execution.adapter_base import UIAdapter


class MinimalAdapter(UIAdapter):
    def click(self, x, y, selector=None): return True
    def double_click(self, x, y, selector=None): return True
    def right_click(self, x, y, selector=None): return True
    def mouse_move(self, x, y): return True
    def drag_and_drop(self, sx, sy, ex, ey, steps=10): return True
    def type_text(self, text, selector=None): return True
    def press_key(self, key): return True
    def hotkey(self, keys): return True
    def scroll(self, dx, dy, x=None, y=None): return True

    def wait_until(self, condition, timeout_ms=3000, poll_interval_ms=20):
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(poll_interval_ms / 1000.0)
        return False


def test_dynamic_wait_early_exit():
    adapter = MinimalAdapter()
    start = time.monotonic()
    # Condition becomes true after 80ms
    ready_time = start + 0.08
    ok = adapter.wait_until(lambda: time.monotonic() >= ready_time, timeout_ms=3000)
    elapsed = time.monotonic() - start

    assert ok is True
    assert elapsed < 0.3, f"Expected dynamic wait to return quickly, took {elapsed:.3f}s"
    print(f"PASS: Dynamic wait returned in {elapsed*1000:.1f}ms without waiting for full 3000ms timeout")


def test_dynamic_wait_timeout():
    adapter = MinimalAdapter()
    start = time.monotonic()
    # Never true
    ok = adapter.wait_until(lambda: False, timeout_ms=200)
    elapsed = time.monotonic() - start

    assert ok is False
    assert 0.18 <= elapsed <= 0.45
    print("PASS: Dynamic wait timed out correctly on false condition")


if __name__ == "__main__":
    test_dynamic_wait_early_exit()
    test_dynamic_wait_timeout()
    print("\nALL DYNAMIC WAIT TESTS PASSED!")
