"""Automatic Recovery Engine for unexpected popups, cookie banners, and dialogs.
Prevents agent stall when pages present non-blocking overlay interruptions.
"""
from typing import Any, Dict, Optional, Tuple

from policy.risk import RiskPolicyEngine
from models.schemas import ActionType, RiskLevel

# Detects buttons that reject or limit cookies to necessary only.
# Strictly NEVER accepts all, agrees, or clicks generic "OK/ตกลง".
COOKIE_REJECT_JS = """() => {
    const buttons = Array.from(document.querySelectorAll('button, a, [role=button]'));
    const rejectRe = /(?:reject all|necessary only|ปฏิเสธคุกกี้|ปฏิเสธทั้งหมด|ปฏิเสธการยินยอม|จำเป็นเท่านั้น|ที่จำเป็นเท่านั้น|เฉพาะที่จำเป็น|decline all|only necessary|reject non-essential|reject cookies)/i;
    const acceptRe = /(?:accept all|ยอมรับทั้งหมด|ยอมรับคุกกี้|agree all|allow all|accept cookies|i accept|ตกลง|ok)/i;

    // Pass 1: Prioritize finding any Reject / Necessary Only button
    for (let i = 0; i < buttons.length; i++) {
        const b = buttons[i];
        const text = (b.innerText || b.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
        if (b.offsetWidth > 0 && b.offsetHeight > 0) {
            // Guard: Never treat disclaimers or legal/responsibility notices as cookie buttons
            if (/ความรับผิดชอบ|disclaimer/i.test(text)) continue;
            if (rejectRe.test(text) || (/^ปฏิเสธ$/i.test(text) && /cookie|คุกกี้|consent/i.test(b.closest('div, section, footer, [role=dialog]')?.innerText || ''))) {
                return { status: 'reject_candidate', index: i, text: text };
            }
        }
    }

    // Pass 2: Check if only Accept is available on the banner
    let foundAcceptOnly = false;
    let acceptText = '';
    let acceptIndex = -1;
    for (let i = 0; i < buttons.length; i++) {
        const b = buttons[i];
        const text = (b.innerText || b.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
        if (b.offsetWidth > 0 && b.offsetHeight > 0) {
            if (/ความรับผิดชอบ|disclaimer/i.test(text)) continue;
            // Check if button is within a cookie banner container or specifically mentions cookies
            const parent = b.closest('#cookie-banner, .cookie-banner, [id*="cookie" i], [class*="cookie" i], [role=dialog]');
            const isCookieContext = !!parent || /cookie|คุกกี้/i.test(text);
            if (isCookieContext && acceptRe.test(text)) {
                foundAcceptOnly = true;
                acceptText = text;
                acceptIndex = i;
                break;
            }
        }
    }

    if (foundAcceptOnly) {
        return { status: 'accept_only_detected', index: acceptIndex, text: acceptText };
    }

    return null;
}"""

MODAL_CLOSE_JS = """() => {
    const closes = Array.from(document.querySelectorAll('[aria-label="Close"], [aria-label="ปิด"], .modal-close, .popup-close, .close-button, button.close'));
    for (let i = 0; i < closes.length; i++) {
        const c = closes[i];
        if (c.offsetWidth > 0 && c.offsetHeight > 0) {
            const text = (c.innerText || c.getAttribute('aria-label') || '').trim();
            return { status: 'candidate', index: i, text: text };
        }
    }
    return null;
}"""


class RecoveryEngine:
    @staticmethod
    def handle_cookie_banner(page: Any) -> Tuple[Optional[str], bool]:
        """Attempt to dismiss cookie consent dialogs by selecting Reject / Necessary Only.
        Always evaluates RiskPolicyEngine.classify() before clicking.

        Returns (result_msg, require_takeover)
        """
        try:
            res = page.evaluate(COOKIE_REJECT_JS)
            if not res:
                return None, False

            btn_text = res.get("text", "")
            btn_idx = res.get("index", -1)

            # Mandatory P1-1: RiskPolicyEngine check before clicking
            risk = RiskPolicyEngine.classify(
                action_type=ActionType.CLICK,
                target_text=btn_text,
                url=page.url,
                goal="cookie_banner"
            )

            # If accept only or if button is classified >= Risk 2 (e.g. 'ตกลง' on checkout page):
            if res.get("status") == "accept_only_detected" or risk.value >= RiskLevel.RISK_2_CONFIRM.value:
                return "cookie_consent_only_accept_available", True

            if res.get("status") in ("reject_candidate", "rejected") and risk.value == RiskLevel.RISK_0_SAFE.value:
                page.evaluate("""(idx) => {
                    const buttons = Array.from(document.querySelectorAll('button, a, [role=button]'));
                    if (buttons[idx]) buttons[idx].click();
                }""", btn_idx)
                return f"cookie_rejected: {btn_text}", False
        except Exception:
            pass
        return None, False

    @staticmethod
    def close_unexpected_modal(page: Any) -> Optional[str]:
        """Attempt to dismiss unexpected popup modals blocking interaction safely.
        Always evaluates RiskPolicyEngine.classify() before clicking.
        """
        try:
            res = page.evaluate(MODAL_CLOSE_JS)
            if not res:
                return None

            btn_text = res.get("text", "")
            btn_idx = res.get("index", -1)

            # Mandatory P1-1: RiskPolicyEngine check before clicking modal close button
            risk = RiskPolicyEngine.classify(
                action_type=ActionType.CLICK,
                target_text=btn_text,
                url=page.url,
                goal="close_modal"
            )

            # Never dismiss modal if Risk is >= 1 (destructive, confirm, delete, pay, etc.)
            if risk.value != RiskLevel.RISK_0_SAFE.value:
                return None

            page.evaluate("""(idx) => {
                const closes = Array.from(document.querySelectorAll('[aria-label="Close"], [aria-label="ปิด"], .modal-close, .popup-close, .close-button, button.close'));
                if (closes[idx]) closes[idx].click();
            }""", btn_idx)
            return "modal_closed"
        except Exception:
            pass
        return None

