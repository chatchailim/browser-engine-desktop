"""State Verifier for Preconditions and Postconditions.
Validates action outcomes (toast, navigation, focus, value matching, DOM stability).
"""
import time
from typing import Any, Optional
from models.schemas import PreCondition, PostCondition, TargetCandidate

COMMON_TOAST_SELECTORS = [
    ".toast", ".toast-success", ".alert-success", "[role=alert]",
    ".notification-success", ".snackbar", ".ant-notification-notice-success"
]


class StateVerifier:
    @staticmethod
    def verify_precondition(page: Any, precondition: PreCondition, target: Optional[TargetCandidate] = None) -> bool:
        """Verify preconditions before dispatching action."""
        if not precondition:
            return True

        if precondition.url_matches:
            if precondition.url_matches not in page.url:
                return False

        if target and target.selector:
            try:
                loc = page.locator(target.selector).first
                if precondition.visible and not loc.is_visible(timeout=300):
                    return False
                if precondition.enabled and loc.is_disabled(timeout=300):
                    return False
            except Exception:
                pass

        return True

    @staticmethod
    def verify_postcondition(page: Any, postcondition: PostCondition, target: Optional[TargetCandidate] = None, initial_url: Optional[str] = None) -> bool:
        """Verify postconditions after action execution."""
        if not postcondition:
            return True

        # Check navigation
        if postcondition.navigation and initial_url:
            if page.url == initial_url:
                # Wait briefly for navigation if in progress
                try:
                    page.wait_for_url(lambda u: u != initial_url, timeout=1200)
                except Exception:
                    return False

        # Check selector visible
        if postcondition.selector_visible:
            try:
                loc = page.locator(postcondition.selector_visible).first
                if not loc.is_visible(timeout=1500):
                    return False
            except Exception:
                return False

        # Check selector hidden
        if postcondition.selector_hidden:
            try:
                loc = page.locator(postcondition.selector_hidden).first
                if loc.is_visible(timeout=1000):
                    return False
            except Exception:
                pass

        # Check value contains
        if postcondition.value_contains:
            sel = target.selector if target and target.selector else None
            try:
                if sel:
                    val = page.locator(sel).input_value(timeout=1000)
                    if postcondition.value_contains.lower() not in val.lower():
                        return False
            except Exception:
                pass

        # Check text present in document
        if postcondition.text_present:
            try:
                text_ok = page.evaluate("(t) => document.body ? document.body.innerText.includes(t) : false", postcondition.text_present)
                if not text_ok:
                    return False
            except Exception:
                return False

        # Check success toast
        if postcondition.toast_success:
            found = False
            for sel in COMMON_TOAST_SELECTORS:
                try:
                    if page.locator(sel).first.is_visible(timeout=500):
                        found = True
                        break
                except Exception:
                    pass
            if not found:
                return False

        return True

    @staticmethod
    def wait_until_stable(page: Any, timeout_ms: int = 2500) -> bool:
        """Wait until DOM mutations settle."""
        js = """() => new Promise((resolve) => {
            let timer = null;
            const obs = new MutationObserver(() => {
                clearTimeout(timer);
                timer = setTimeout(() => { obs.disconnect(); resolve(true); }, 100);
            });
            obs.observe(document.body, { childList: true, subtree: true, attributes: true });
            timer = setTimeout(() => { obs.disconnect(); resolve(true); }, 150);
        })"""
        try:
            page.evaluate(js)
            return True
        except Exception:
            return False
