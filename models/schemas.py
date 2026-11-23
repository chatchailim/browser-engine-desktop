"""Data models and schemas for OneVault Interactive Agent Engine.
Defines types for Actions, Strategies, Macro pipelines, Pre/Post-conditions, Risk, and Traces.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union


class Strategy(str, Enum):
    DIRECT_API = "direct_api"
    DOM = "dom"
    ACCESSIBILITY = "accessibility"
    VISION = "vision"
    WINDOWS_UIA = "windows_uia"


class ActionType(str, Enum):
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    RIGHT_CLICK = "right_click"
    MOUSE_MOVE = "mouse_move"
    DRAG_AND_DROP = "drag_and_drop"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    HOTKEY = "hotkey"
    SCROLL = "scroll"
    WAIT_UNTIL = "wait_until"
    NAVIGATE = "navigate"
    EXTRACT = "extract"
    VERIFY = "verify"
    API_CALL = "api_call"
    MACRO = "macro"
    BLOCKED = "blocked"
    DONE = "done"


class RiskLevel(int, Enum):
    RISK_0_SAFE = 0       # scroll, hover, open page, search
    RISK_1_AUDIT = 1      # fill form, upload draft, edit document
    RISK_2_CONFIRM = 2    # send message, submit form, publish
    RISK_3_APPROVAL = 3   # payment, delete, sign agreement, change permissions


@dataclass
class CoordinateContext:
    norm_x: int  # 0..1000
    norm_y: int  # 0..1000
    viewport_width: int = 1280
    viewport_height: int = 900
    device_scale_factor: float = 1.0
    scroll_x: int = 0
    scroll_y: int = 0

    def to_pixel(self) -> Tuple[int, int]:
        px = int(self.norm_x * self.viewport_width / 1000.0)
        py = int(self.norm_y * self.viewport_height / 1000.0)
        return px, py

    @classmethod
    def from_pixel(cls, x: int, y: int, width: int = 1280, height: int = 900) -> "CoordinateContext":
        norm_x = max(0, min(1000, int(x * 1000.0 / max(1, width))))
        norm_y = max(0, min(1000, int(y * 1000.0 / max(1, height))))
        return cls(norm_x=norm_x, norm_y=norm_y, viewport_width=width, viewport_height=height)


@dataclass
class TargetCandidate:
    strategy: Strategy
    confidence: float  # 0.0 .. 1.0
    selector: Optional[str] = None
    role: Optional[str] = None
    name: Optional[str] = None
    bounds: Optional[Tuple[int, int, int, int]] = None  # x, y, width, height
    coordinates: Optional[Tuple[int, int]] = None       # x, y
    element_id: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PreCondition:
    visible: bool = True
    enabled: bool = True
    focused: bool = False
    url_matches: Optional[str] = None


@dataclass
class PostCondition:
    selector_visible: Optional[str] = None
    selector_hidden: Optional[str] = None
    navigation: bool = False
    value_contains: Optional[str] = None
    text_present: Optional[str] = None
    toast_success: bool = False
    custom_eval: Optional[str] = None


@dataclass
class ActionStep:
    action: ActionType
    target: Optional[TargetCandidate] = None
    text: Optional[str] = None
    key: Optional[str] = None
    keys: Optional[List[str]] = None
    start_coord: Optional[Tuple[int, int]] = None
    end_coord: Optional[Tuple[int, int]] = None
    delta_x: int = 0
    delta_y: int = 0
    wait_condition: Optional[str] = None
    precondition: Optional[PreCondition] = None
    postcondition: Optional[PostCondition] = None
    timeout_ms: int = 4000
    risk: RiskLevel = RiskLevel.RISK_0_SAFE
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MacroDefinition:
    macro_id: str
    steps: List[ActionStep]
    stop_on_failure: bool = True
    description: str = ""


@dataclass
class PlanTask:
    task_id: str
    goal: str
    sub_tasks: List[Dict[str, Any]]
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionTrace:
    trace_id: str
    step_num: int
    action_type: str
    strategy: str
    confidence: float
    target_desc: str
    latency_breakdown_ms: Dict[str, int] = field(default_factory=dict)  # planning, execution, verification
    total_ms: int = 0
    result: str = "SUCCESS"  # SUCCESS, FAILED, BLOCKED, TAKEOVER
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
