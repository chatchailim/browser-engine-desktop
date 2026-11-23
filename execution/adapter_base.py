"""Abstract Base Class for UI Adapters (Browser CDP and Windows UIA).
Defines universal input methods across browser and desktop environments.
"""
from abc import ABC, abstractmethod
from typing import Any, Callable, List, Optional, Tuple


class UIAdapter(ABC):
    @abstractmethod
    def click(self, x: int, y: int, selector: Optional[str] = None) -> bool:
        """Click at coordinate (x, y) or directly via selector."""
        pass

    @abstractmethod
    def double_click(self, x: int, y: int, selector: Optional[str] = None) -> bool:
        """Double click at coordinate (x, y)."""
        pass

    @abstractmethod
    def right_click(self, x: int, y: int, selector: Optional[str] = None) -> bool:
        """Right click (context menu) at coordinate (x, y)."""
        pass

    @abstractmethod
    def mouse_move(self, x: int, y: int) -> bool:
        """Move cursor / hover at coordinate (x, y)."""
        pass

    @abstractmethod
    def drag_and_drop(self, start_x: int, start_y: int, end_x: int, end_y: int, steps: int = 10) -> bool:
        """Smoothly drag from start coordinate to end coordinate."""
        pass

    @abstractmethod
    def type_text(self, text: str, selector: Optional[str] = None) -> bool:
        """Type text into current focused element or selector."""
        pass

    @abstractmethod
    def press_key(self, key: str) -> bool:
        """Press a keyboard key (e.g. Enter, Escape, Backspace, Tab)."""
        pass

    @abstractmethod
    def hotkey(self, keys: List[str]) -> bool:
        """Execute key combination (e.g. ['Control', 'a'], ['Control', 'c'])."""
        pass

    @abstractmethod
    def scroll(self, delta_x: int, delta_y: int, x: Optional[int] = None, y: Optional[int] = None) -> bool:
        """Scroll mouse wheel by (delta_x, delta_y)."""
        pass

    @abstractmethod
    def wait_until(self, condition: Callable[[], bool], timeout_ms: int = 5000, poll_interval_ms: int = 50) -> bool:
        """Dynamically wait until condition evaluates to True or timeout expires."""
        pass
