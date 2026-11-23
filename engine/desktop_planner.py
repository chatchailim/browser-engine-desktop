"""AI → desktop actions bridge (ระดับ A): planner เดิมแตกเป้าเป็นลำดับคำสั่งเดสก์ท็อป.

สัญญา:
- planner ตัวจริง (DGX LLM) ได้รับ: เป้าหมายงาน + กรอบหน้าต่าง (client rect) + ข้อจำกัดคำสั่ง
  → ตอบ JSON ลำดับ actions แบบเข้ม (kind/args)
- ตรวจทุกคำสั่งด้วย action_from_json() เดิม (schema คุมเข้มก่อนถึงเซสชัน) — ขยับเกิน schema ไม่ได้แม้โมเดลแนะนำ
- ไม่มีตรรกะ OS ใด ๆ ในไฟล์นี้ (backend/guard เป็นผู้กระทำจริง)
"""
import json
from execution.guarded_desktop import action_from_json

SYSTEM_PROMPT = """You convert a user goal into a STRICT sequence of desktop GUI actions for a
pre-selected window (client area rectangle provided). Available actions ONLY:
{"kind":"click","args":[x,y]}      click at physical screen pixel inside the rect
{"kind":"drag","args":[x1,y1,x2,y2]}  drag within the same window
{"kind":"type","args":["text"]}    type text (1..500 chars, no newline/control chars)
{"kind":"key","args":["ENTER|TAB|BACKSPACE|DELETE|ESC|LEFT|RIGHT|UP|DOWN|HOME|END"]}
Return strict JSON: {"actions":[...]} with at most {max_actions} actions.
Do not invent other verbs. Do not target coordinates outside the client rect given.
If the goal cannot be done with these actions, return {"actions":[]} and put the reason in "reason"."""


def plan_desktop_actions(planner, goal, client_rect, max_actions=60):
    """planner = TaskPlanner-like with .client.chat_completion; returns list of Action (validated)."""
    user = (f"GOAL: {goal}\nCLIENT_RECT (x1,y1,x2,y2): {client_rect}\n"
            f"MAX_ACTIONS: {max_actions}\n"
            "Respond with the strict JSON object only.")
    messages = [{"role": "system", "content": SYSTEM_PROMPT.replace("{max_actions}", str(max_actions))},
                {"role": "user", "content": user}]
    resp = planner.client.chat_completion(messages, max_tokens=1500, temperature=0.0)
    raw = planner.client.extract_text_or_json(resp)
    import re
    clean = re.sub(r"^```json\s*|\s*```$", "", (raw or "").strip(), flags=re.MULTILINE)
    m = re.search(r"(\{.*\})", clean, re.DOTALL)
    data = json.loads(m.group(1) if m else clean)
    actions_data = data.get("actions")
    if not isinstance(actions_data, list):
        raise ValueError("plan_desktop_invalid_actions")
    if len(actions_data) > max_actions:
        raise ValueError("plan_desktop_too_many_actions")
    actions = [action_from_json(a) for a in actions_data]  # strict schema (เดิม) — ผิดชนิดใด = งานถูกปฏิเสธทั้งใบ
    return actions, data.get("reason")


def plan_desktop_actions_via_recipe(client, goal, client_rect, max_actions=60):
    """ทางยืดหยุ่น: ส่ง client (DGXVllmClient) ตรง เมื่อไม่มี TaskPlanner ครบอยู่มือ"""
    class _P:  # duck-type shim
        pass
    p = _P()
    p.client = client
    return plan_desktop_actions(p, goal, client_rect, max_actions)