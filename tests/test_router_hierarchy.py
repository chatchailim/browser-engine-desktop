"""Unit test for 5-Level Interaction Router.
Verifies priority hierarchy: Direct API -> DOM -> Accessibility -> Vision Fallback.
"""
from models.schemas import Strategy
from engine.router import InteractionRouter


class MockPage:
    def __init__(self, dom_elements=None, ax_tree=None):
        self._dom = dom_elements or []
        self._ax = ax_tree or {}
        self.accessibility = self
        self.url = "http://example.com"

    def evaluate(self, script, *args):
        return self._dom

    def snapshot(self, interesting_only=True):
        return self._ax


def test_router_direct_api():
    """Level 1: Direct API should take precedence if payload present."""
    page = MockPage()
    res = InteractionRouter.resolve_target(
        page,
        target_description="Save",
        direct_api_payload={"url": "/api/save", "method": "POST"}
    )
    assert res.strategy == Strategy.DIRECT_API
    assert res.confidence == 1.0
    print("PASS: Level 1 Direct API resolved first")


def test_router_dom_match():
    """Level 2: DOM selector match with confidence >= 0.90."""
    dom_elements = [
        {"id": 1, "role": "button", "selector": "#save-btn", "text": "Save Draft", "bounds": [100, 200, 80, 30], "center": [140, 215]}
    ]
    page = MockPage(dom_elements=dom_elements)
    res = InteractionRouter.resolve_target(page, target_description="Save Draft")
    assert res.strategy == Strategy.DOM
    assert res.confidence >= 0.90
    assert res.coordinates == (140, 215)
    print("PASS: Level 2 DOM match resolved with high confidence")


def test_router_accessibility_match():
    """Level 3: Accessibility Tree match when DOM has no selector."""
    # DOM has no match
    dom_elements = []
    # AX tree has semantic match
    ax_tree = {
        "role": "WebArea",
        "children": [
            {"role": "button", "name": "Submit Order", "box": {"x": 200, "y": 300, "width": 120, "height": 40}}
        ]
    }
    page = MockPage(dom_elements=dom_elements, ax_tree=ax_tree)
    res = InteractionRouter.resolve_target(page, target_description="Submit Order", preferred_role="button")
    assert res.strategy == Strategy.ACCESSIBILITY
    assert res.confidence >= 0.85
    assert res.coordinates == (260, 320)
    print("PASS: Level 3 Accessibility match resolved when DOM unavailable")


def test_router_vision_fallback():
    """Level 4: Vision fallback when neither DOM nor AX match."""
    page = MockPage(dom_elements=[], ax_tree={})
    res = InteractionRouter.resolve_target(page, target_description="Canvas Drawing Knob")
    assert res.strategy == Strategy.VISION
    assert res.confidence == 0.75
    assert res.metadata.get("needs_vision_grounding") is True
    print("PASS: Level 4 Vision fallback triggered correctly")


if __name__ == "__main__":
    test_router_direct_api()
    test_router_dom_match()
    test_router_accessibility_match()
    test_router_vision_fallback()
    print("\nALL ROUTER HIERARCHY TESTS PASSED!")
