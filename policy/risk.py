"""Action Risk Classifier and Policy Gates.
Classifies actions into 4 risk tiers (Risk 0 to Risk 3) to protect business operations.
Enforces text normalization (NFC + decomposed Thai sara am unification) and checkout context rules.
"""
import re
import unicodedata
from typing import Optional
from models.schemas import ActionType, RiskLevel


def normalize_text(text: str) -> str:
    """Normalize text for consistent classification:
    1. Unicode NFC normalization
    2. Unify decomposed Thai sara am (U+0E4D + optional tone + U+0E32 -> tone + U+0E33)
    3. Collapse multiple whitespace
    """
    if not text:
        return ""
    norm = unicodedata.normalize("NFC", str(text))
    norm = re.sub(r"\u0E4D([\u0E48-\u0E4B]?)\u0E32", lambda m: m.group(1) + "\u0E33", norm)
    norm = norm.replace("\u0E4D\u0E32", "\u0E33")
    norm = re.sub(r"\s+", " ", norm).strip()
    return norm


# Risk 3: Financial, Irreversible, Destructive, Account Security
# Thai keywords (matches full or compound words)
RISK_3_THAI = (
    r"ชำระเงิน|ชำระค่าสินค้า|ชำระ|ซื้อทันที|ซื้อเลย|ซื้อ|จ่ายเงิน|จ่ายเลย|"
    r"ยืนยันการสั่งซื้อ|สั่งซื้อ|โอนเงิน|โอนเลย|ถอนเงิน|เติมเงิน|ผูกบัตร|รูดบัตร|เพิ่มบัตรใหม่|"
    r"ลบ|ยกเลิกสัญญา|บัตรเครดิต"
)

# English keywords require word boundaries (\b)
RISK_3_ENG = (
    r"\bpay\b|\bpayment\b|\bcheckout\b|\bplace order\b|\bbuy now\b|\bbuy\b|\bpay now\b|\border now\b|"
    r"\bcomplete purchase\b|\bdelete\b|\bremove account\b|\bchange password\b|"
    r"\breset password\b|\bpermissions\b|\bcredit card\b|\bconfirm order\b|"
    r"\bwithdraw\b|\btransfer\b|\badd card\b"
)
RISK_3_RE = re.compile(f"{RISK_3_THAI}|{RISK_3_ENG}", re.I)

# Risk 2: Submission, Publishing, Permanent action confirmation
RISK_2_THAI = r"ส่งข้อความ|ส่ง|ยืนยัน|เผยแพร่|โพสต์|ลงประกาศ|บันทึกถาวร"
RISK_2_ENG = r"\bsend\b|\bsubmit\b|\bconfirm\b|\bpublish\b|\bpost\b"
RISK_2_RE = re.compile(f"{RISK_2_THAI}|{RISK_2_ENG}", re.I)

# Checkout / Financial context pattern
CHECKOUT_CONTEXT_RE = re.compile(
    r"checkout|payment|cart|order|billing|ชำระเงิน|สั่งซื้อ|ตะกร้า|แคชเชียร์",
    re.I
)

# Generic confirmation keywords elevated to Risk 2 on checkout/payment context
CHECKOUT_CONFIRM_THAI = r"ตกลง|ถัดไป|ดำเนินการต่อ|ต่อไป|ไปต่อ"
CHECKOUT_CONFIRM_ENG = r"\bok\b|\bcontinue\b|\bnext\b|\bproceed\b"
CHECKOUT_CONFIRM_RE = re.compile(f"{CHECKOUT_CONFIRM_THAI}|{CHECKOUT_CONFIRM_ENG}", re.I)

# Risk 1: Drafting, Form filling, Non-critical editing
RISK_1_THAI = r"แก้ไข|กรอกข้อมูล|แนบไฟล์"
RISK_1_ENG = r"\bedit\b|\bupdate\b|\bfill\b|\bupload\b"
RISK_1_RE = re.compile(f"{RISK_1_THAI}|{RISK_1_ENG}", re.I)

# Explicit Read-only / Safe keywords
SAFE_RE = re.compile(r"search|next page|ดูรายละเอียด|อ่านเพิ่มเติม|view details|view|inspect|browse|read", re.I)

# Direct API Financial / Destructive keywords (GR5 / WO-015.1 R2)
RISK_3_API_THAI = r"จ่าย|โอน|ชำระ|สั่งซื้อ|ยกเลิก|ลบ|ปิดบัญชี|ถอน|เติมเงิน"
RISK_3_API_ENG = (
    r"\brefunds?\b|\btransfers?\b|\bpayments?\b|\bcharges?\b|\bdelete\b|\bpay\b|\bpurchases?\b|"
    r"\bwithdraw(?:al)?\b|\bcancels?\b|\bclose account\b|\border create\b|\bcreate order\b|\bplace order\b"
)
RISK_3_API_RE = re.compile(f"{RISK_3_API_THAI}|{RISK_3_API_ENG}", re.I)


class RiskPolicyEngine:
    @staticmethod
    def classify_api_call(
        recipe_name: str = "",
        method: Optional[str] = "GET",
        path: str = "",
        params: Optional[Any] = None,
        recipe: Optional[Any] = None
    ) -> RiskLevel:
        """Classify Direct API call risk according to Ground Rule 5 (WO-015/WO-015.1):
        - GET / HEAD -> Risk 0 (Safe)
        - POST / PUT / PATCH / DELETE -> Risk >= 2 (Confirm)
        - If recipe name, path, or payload content fields match financial or destructive words -> Risk 3 (Approval)
        - Missing method / unknown method -> Risk 3 (Deny-by-default)
        """
        if not method:
            return RiskLevel.RISK_3_APPROVAL

        meth = str(method).upper().strip()
        r_name = normalize_text(recipe_name)
        r_path = normalize_text(path)
        param_dict = params if isinstance(params, dict) else {}
        param_keys = " ".join(normalize_text(str(k)) for k in param_dict.keys())
        
        # Check recipe payload template fields if provided
        template_keys = ""
        if recipe and isinstance(recipe, dict) and isinstance(recipe.get("payload_template"), dict):
            template_keys = " ".join(normalize_text(str(k)) for k in recipe["payload_template"].keys())

        raw_context = f"{r_name} {r_path} {param_keys} {template_keys}".strip()
        # R2: Normalize separators (_, -, ., /, \) to whitespace before regex matching
        api_context_clean = re.sub(r"[_\-./\\ ]+", " ", raw_context)

        # 1. Critical / Financial / Destructive -> Risk 3
        if RISK_3_API_RE.search(api_context_clean) or RISK_3_RE.search(api_context_clean):
            return RiskLevel.RISK_3_APPROVAL

        # 2. State-changing methods -> Minimum Risk 2
        if meth in ("POST", "PUT", "PATCH", "DELETE"):
            return RiskLevel.RISK_2_CONFIRM

        # 3. Read-only methods -> Risk 0
        if meth in ("GET", "HEAD"):
            return RiskLevel.RISK_0_SAFE

        # 4. Unknown method -> Deny-by-default (Risk 3)
        return RiskLevel.RISK_3_APPROVAL

    @staticmethod
    def is_checkout_context(url: str = "", goal: str = "", context_text: str = "") -> bool:
        """Check if current page URL, goal, or context implies checkout/payment flow."""
        combined = f"{url} {goal} {context_text}"
        norm = normalize_text(combined)
        return bool(CHECKOUT_CONTEXT_RE.search(norm))

    @staticmethod
    def classify(
        action_type: ActionType,
        target_text: str = "",
        url: str = "",
        goal: str = "",
        aria_label: str = ""
    ) -> RiskLevel:
        """Classify action risk level based on action type, semantic labels, URL, and goal.
        Normalizes Thai text (including decomposed sara am) and enforces V9 inverted classifier
        on financial / checkout contexts.
        """
        # Handle Direct API action type
        if action_type == ActionType.API_CALL:
            # R5: Deny-by-default (Risk 3) when method is not supplied in general classify
            return RiskLevel.RISK_3_APPROVAL

        # Normalize all inputs
        norm_target = normalize_text(target_text)
        norm_aria = normalize_text(aria_label)
        norm_url = normalize_text(url)
        norm_goal = normalize_text(goal)

        combined = f"{norm_target} {norm_aria}".strip()
        full_context = f"{combined} {norm_url} {norm_goal}".strip()

        # Check checkout context
        in_checkout = RiskPolicyEngine.is_checkout_context(url=norm_url, goal=norm_goal)

        # -------------------------------------------------------------------------
        # V9 Inverted Risk Classifier for Financial Context:
        # On checkout/payment/cart/order/billing pages, ANY action that is not strictly
        # read-only defaults to AT LEAST Risk 2 (CONFIRM), protecting against unknown
        # buttons, icon-only buttons, and unlisted financial terms.
        # -------------------------------------------------------------------------
        if in_checkout:
            # 1. Critical / Financial / Irreversible (Risk 3 takes highest precedence)
            if RISK_3_RE.search(combined) or (
                RISK_3_RE.search(norm_target) or any(w in norm_target.lower() for w in ("ชำระ", "จ่าย", "ซื้อ", "pay", "order", "buy", "โอน", "บัตร"))
            ):
                return RiskLevel.RISK_3_APPROVAL

            # 2. Pure read-only safe action check in checkout context
            if action_type in (ActionType.SCROLL, ActionType.WAIT_UNTIL, ActionType.EXTRACT, ActionType.VERIFY):
                return RiskLevel.RISK_0_SAFE
            if norm_target and SAFE_RE.search(norm_target) and not RISK_2_RE.search(norm_target) and not RISK_3_RE.search(norm_target):
                return RiskLevel.RISK_0_SAFE

            # 3. Explicit confirmations or submissions
            if RISK_2_RE.search(combined) or CHECKOUT_CONFIRM_RE.search(combined):
                return RiskLevel.RISK_2_CONFIRM

            # 4. Deny-by-default for all other interactive actions in checkout context:
            # Any unlisted term, unknown button, or icon button without text defaults to Risk 2.
            return RiskLevel.RISK_2_CONFIRM

        # -------------------------------------------------------------------------
        # Standard Classifier (Outside Financial Context):
        # Preserves normal exploration, search, and navigation to prevent over-blocking.
        # -------------------------------------------------------------------------
        # 1. Read-only explicit safe action check
        if SAFE_RE.search(norm_target):
            if not RISK_3_RE.search(norm_target) and not RISK_2_RE.search(norm_target):
                return RiskLevel.RISK_0_SAFE

        # 2. Critical / Financial / Irreversible (Risk 3)
        if RISK_3_RE.search(combined):
            return RiskLevel.RISK_3_APPROVAL

        # 3. Submission / Publishing / Confirmation (Risk 2)
        if RISK_2_RE.search(combined):
            return RiskLevel.RISK_2_CONFIRM

        # Enter key in context indicating submit/post
        if action_type == ActionType.PRESS_KEY and "enter" in norm_target.lower():
            if RISK_2_RE.search(full_context):
                return RiskLevel.RISK_2_CONFIRM

        # 4. Modification / Drafting (Risk 1)
        if RISK_1_RE.search(combined) or action_type == ActionType.TYPE_TEXT:
            return RiskLevel.RISK_1_AUDIT

        # Default Read-only / Navigation / Exploration (Risk 0)
        return RiskLevel.RISK_0_SAFE
