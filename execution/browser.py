"""Browser Adapter implementing UIAdapter over Playwright / CDP.
Provides high-speed browser interaction with optional visual click indicator on screen.
"""
import time
from typing import Any, Callable, List, Optional
from execution.adapter_base import UIAdapter

RIPPLE_EFFECT_JS = """(x, y) => {
    const d = document.createElement('div');
    d.style.position = 'fixed';
    d.style.left = (x - 12) + 'px';
    d.style.top = (y - 12) + 'px';
    d.style.width = '24px';
    d.style.height = '24px';
    d.style.borderRadius = '50%';
    d.style.backgroundColor = 'rgba(239, 68, 68, 0.7)';
    d.style.border = '2px solid white';
    d.style.pointerEvents = 'none';
    d.style.zIndex = '2147483647';
    d.style.transition = 'transform 0.4s ease-out, opacity 0.4s ease-out';
    document.body.appendChild(d);
    requestAnimationFrame(() => {
        d.style.transform = 'scale(2.0)';
        d.style.opacity = '0';
    });
    setTimeout(() => { d.remove(); }, 450);
}"""


class BrowserAdapter(UIAdapter):
    def __init__(self, page: Any, show_click_visual: bool = True):
        self.page = page
        self.show_click_visual = show_click_visual

    def _show_visual_click(self, x: int, y: int) -> None:
        if self.show_click_visual:
            try:
                self.page.evaluate(RIPPLE_EFFECT_JS, x, y)
            except Exception:
                pass

    def click(self, x: int, y: int, selector: Optional[str] = None) -> bool:
        try:
            if selector:
                loc = self.page.locator(selector).first
                if loc.is_visible(timeout=300):
                    loc.click(timeout=1500)
                    return True
            self._show_visual_click(x, y)
            self.page.mouse.click(x, y)
            return True
        except Exception:
            try:
                self.page.mouse.click(x, y)
                return True
            except Exception:
                return False

    def double_click(self, x: int, y: int, selector: Optional[str] = None) -> bool:
        try:
            if selector:
                loc = self.page.locator(selector).first
                if loc.is_visible(timeout=300):
                    loc.dblclick(timeout=1500)
                    return True
            self._show_visual_click(x, y)
            self.page.mouse.dblclick(x, y)
            return True
        except Exception:
            return False

    def right_click(self, x: int, y: int, selector: Optional[str] = None) -> bool:
        try:
            if selector:
                loc = self.page.locator(selector).first
                if loc.is_visible(timeout=300):
                    loc.click(button="right", timeout=1500)
                    return True
            self.page.mouse.click(x, y, button="right")
            return True
        except Exception:
            return False

    def mouse_move(self, x: int, y: int) -> bool:
        try:
            self.page.mouse.move(x, y)
            return True
        except Exception:
            return False

    def drag_and_drop(self, start_x: int, start_y: int, end_x: int, end_y: int, steps: int = 10) -> bool:
        try:
            self.page.mouse.move(start_x, start_y)
            self.page.mouse.down()
            self.page.mouse.move(end_x, end_y, steps=max(2, steps))
            self.page.mouse.up()
            return True
        except Exception:
            return False

    def type_text(self, text: str, selector: Optional[str] = None) -> bool:
        try:
            if selector:
                loc = self.page.locator(selector).first
                if loc.is_visible(timeout=300):
                    loc.fill(text, timeout=2000)
                    return True
            # Fast keystroke dispatch
            self.page.keyboard.type(text, delay=5)
            return True
        except Exception:
            return False

    def press_key(self, key: str) -> bool:
        try:
            self.page.keyboard.press(key)
            return True
        except Exception:
            return False

    def hotkey(self, keys: List[str]) -> bool:
        try:
            combo = "+".join(keys)
            self.page.keyboard.press(combo)
            return True
        except Exception:
            return False

    def scroll(self, delta_x: int, delta_y: int, x: Optional[int] = None, y: Optional[int] = None) -> bool:
        try:
            if x is not None and y is not None:
                self.page.mouse.move(x, y)
            self.page.mouse.wheel(delta_x, delta_y)
            return True
        except Exception:
            return False

    def wait_until(self, condition: Callable[[], bool], timeout_ms: int = 5000, poll_interval_ms: int = 50) -> bool:
        deadline = time.monotonic() + (timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            try:
                if condition():
                    return True
            except Exception:
                pass
            self.page.wait_for_timeout(poll_interval_ms)
        return False
