"""Unit test for Conditional Macro Runner.
Verifies atomic multi-step execution, precondition checks, postcondition verification, and stop on failure.
"""
from models.schemas import ActionStep, ActionType, MacroDefinition, PreCondition, PostCondition, Strategy, TargetCandidate
from execution.adapter_base import UIAdapter
from execution.macro import ConditionalMacroRunner


class MockAdapter(UIAdapter):
    def __init__(self):
        self.clicked = []
        self.typed = []
        self.keys = []

    def click(self, x: int, y: int, selector=None):
        self.clicked.append((x, y, selector))
        return True

    def double_click(self, x: int, y: int, selector=None): return True
    def right_click(self, x: int, y: int, selector=None): return True
    def mouse_move(self, x: int, y: int): return True
    def drag_and_drop(self, sx, sy, ex, ey, steps=10): return True

    def type_text(self, text: str, selector=None):
        self.typed.append((text, selector))
        return True

    def press_key(self, key: str):
        self.keys.append(key)
        return True

    def hotkey(self, keys): return True
    def scroll(self, dx, dy, x=None, y=None): return True
    def wait_until(self, condition, timeout_ms=5000, poll_interval_ms=50): return condition()


class MockVerifier:
    def __init__(self, pre_val=True, post_val=True):
        self.pre_val = pre_val
        self.post_val = post_val

    def verify_precondition(self, page, pre, target=None):
        return self.pre_val

    def verify_postcondition(self, page, post, target=None, initial_url=None):
        return self.post_val

    def wait_until_stable(self, page, timeout_ms=2500):
        return True


class MockPage:
    def __init__(self):
        self.url = "http://example.com/search"


def test_successful_macro():
    adapter = MockAdapter()
    verifier = MockVerifier(pre_val=True, post_val=True)
    runner = ConditionalMacroRunner(adapter=adapter, verifier=verifier)

    steps = [
        ActionStep(
            action=ActionType.CLICK,
            target=TargetCandidate(strategy=Strategy.DOM, confidence=0.99, coordinates=(100, 200)),
            precondition=PreCondition(visible=True)
        ),
        ActionStep(
            action=ActionType.TYPE_TEXT,
            text="Asus Zenbook 14",
            postcondition=PostCondition(value_contains="Asus")
        ),
        ActionStep(
            action=ActionType.PRESS_KEY,
            key="Enter"
        )
    ]
    macro = MacroDefinition(macro_id="macro-01", steps=steps, stop_on_failure=True)
    res = runner.run_macro(macro, MockPage())

    assert res["ok"] is True
    assert res["total_steps"] == 3
    assert len(adapter.clicked) == 1
    assert adapter.typed[0][0] == "Asus Zenbook 14"
    assert adapter.keys[0] == "Enter"
    print("PASS: Macro executed all 3 steps successfully")


def test_stop_on_failure():
    adapter = MockAdapter()
    # Postcondition fails on step 1
    verifier = MockVerifier(pre_val=True, post_val=False)
    runner = ConditionalMacroRunner(adapter=adapter, verifier=verifier)

    steps = [
        ActionStep(
            action=ActionType.CLICK,
            target=TargetCandidate(strategy=Strategy.DOM, confidence=0.99, coordinates=(100, 200)),
            postcondition=PostCondition(toast_success=True)
        ),
        ActionStep(
            action=ActionType.TYPE_TEXT,
            text="Should not be typed!"
        )
    ]
    macro = MacroDefinition(macro_id="macro-fail", steps=steps, stop_on_failure=True)
    res = runner.run_macro(macro, MockPage())

    assert res["ok"] is False
    assert res["failed_step"] == 1
    assert len(adapter.typed) == 0, "Step 2 should NOT have executed!"
    print("PASS: Macro stopped immediately on step 1 failure (safe execution)")


if __name__ == "__main__":
    test_successful_macro()
    test_stop_on_failure()
    print("\nALL CONDITIONAL MACRO TESTS PASSED!")
