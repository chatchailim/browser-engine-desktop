"""Benchmark Test for Interactive Automation Engine.
Measures TTFA, Action Latency, Macro Execution Speed, and Router Resolution Time.
"""
import time
from models.schemas import ActionStep, ActionType, MacroDefinition, TargetCandidate, Strategy
from execution.adapter_base import UIAdapter
from execution.macro import ConditionalMacroRunner
from engine.router import InteractionRouter


class BenchmarkAdapter(UIAdapter):
    def click(self, x, y, selector=None): return True
    def double_click(self, x, y, selector=None): return True
    def right_click(self, x, y, selector=None): return True
    def mouse_move(self, x, y): return True
    def drag_and_drop(self, sx, sy, ex, ey, steps=10): return True
    def type_text(self, text, selector=None): return True
    def press_key(self, key): return True
    def hotkey(self, keys): return True
    def scroll(self, dx, dy, x=None, y=None): return True
    def wait_until(self, condition, timeout_ms=5000, poll_interval_ms=50): return True


class BenchmarkPage:
    def __init__(self):
        self.url = "http://localhost:8205"
        self.accessibility = self

    def evaluate(self, script, *args):
        # 20 DOM elements
        return [
            {"id": i, "role": "button", "selector": f"#btn-{i}", "text": f"Action {i}", "bounds": [10*i, 20*i, 100, 30], "center": [10*i+50, 20*i+15]}
            for i in range(1, 21)
        ]

    def snapshot(self, interesting_only=True):
        return {
            "role": "WebArea",
            "children": [
                {"role": "button", "name": f"Semantic Button {i}", "box": {"x": 50*i, "y": 100, "width": 80, "height": 30}}
                for i in range(1, 11)
            ]
        }


def benchmark_router():
    page = BenchmarkPage()
    t0 = time.monotonic()
    N = 100
    for _ in range(N):
        cand = InteractionRouter.resolve_target(page, "Action 15")
    elapsed_total_ms = (time.monotonic() - t0) * 1000
    avg_ms = elapsed_total_ms / N

    print(f"Router benchmark: {N} resolutions in {elapsed_total_ms:.1f}ms (Average: {avg_ms:.2f}ms/resolution)")
    assert avg_ms < 20.0, f"Router too slow: {avg_ms:.2f}ms > 20ms"
    print("PASS: Router resolution speed benchmark (< 20ms)")


def benchmark_macro_execution():
    adapter = BenchmarkAdapter()
    runner = ConditionalMacroRunner(adapter=adapter)

    steps = [
        ActionStep(action=ActionType.CLICK, target=TargetCandidate(strategy=Strategy.DOM, confidence=0.99, coordinates=(100, 200))),
        ActionStep(action=ActionType.TYPE_TEXT, text="Fast Macro Typing"),
        ActionStep(action=ActionType.PRESS_KEY, key="Enter")
    ]
    macro = MacroDefinition(macro_id="bench-macro", steps=steps, stop_on_failure=True)

    t0 = time.monotonic()
    res = runner.run_macro(macro, BenchmarkPage())
    elapsed_ms = (time.monotonic() - t0) * 1000

    print(f"3-Step Macro execution time: {elapsed_ms:.2f}ms (target: < 1200ms)")
    assert res["ok"] is True
    assert elapsed_ms < 1200.0, f"Macro execution too slow: {elapsed_ms}ms"
    print("PASS: 3-Step Macro execution speed benchmark (< 1200ms)")


if __name__ == "__main__":
    benchmark_router()
    benchmark_macro_execution()
    print("\nALL BENCHMARK TESTS PASSED!")
