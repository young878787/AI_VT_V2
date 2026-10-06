"""Development endpoints for compiling Live2D expression plans without AI chat."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.utils import env_flag
from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_debug_fixtures import (
    DEBUG_EXPRESSION_RULES, DEBUG_MOTION_RULES, DEBUG_SCENARIOS, build_fake_expression_debug_case,
)
from domain.expression_eye_motion_library import EYE_MOTION_PRESETS
from domain.expression_motion_library import MOTION_BRANCH_LIBRARY
from domain.rushia_expression_profile import (
    FAMILY_ALIASES, FAMILY_VARIANTS, RUSHIA_IDLE_FAMILIES, canonical_rushia_family,
)
from services.expression_compiler import compile_expression_plan
from services.expression_intent_parser import parse_expression_intent

router = APIRouter()

EXPRESSION_VARIANT_LABELS = {
    "small_nod": "輕點頭", "quiet_glance": "安靜側看", "gentle_acknowledgement": "溫柔回應",
    "attentive_nod": "專注點頭", "listen_left": "向左傾聽", "listen_right": "向右傾聽",
    "look_up_left": "向左上思考", "look_up_right": "向右上思考", "consider_then_return": "沉思後回望",
    "warm_nod": "暖暖微笑點頭", "smile_left": "微笑左傾", "smile_right": "微笑右傾",
    "closed_smile_nod": "閉眼笑點頭", "closed_smile_left": "閉眼笑左傾", "closed_smile_right": "閉眼笑右傾",
    "playful_peek": "調皮探望", "playful_wink_left": "俏皮左眨眼", "playful_wink_right": "俏皮右眨眼",
    "tease_left": "左側逗弄", "tease_right": "右側逗弄", "firm_glare": "堅定瞪視", "restrained_turn": "克制轉頭",
    "lower_gaze": "難過垂眼", "sad_look_back": "低落回望", "quiet_sink": "安靜沉下", "quiet_side_glance": "陰沉側望",
    "shy_look_away": "害羞移開視線", "shy_peek_back": "害羞偷看", "small_gasp": "輕聲驚呼", "startled_recoil": "驚嚇後退",
    "question_left": "疑惑左傾", "question_right": "疑惑右傾",
}
EYE_STYLE_LABELS = {
    "none": "停用眼神微動", "soft_saccade": "柔和視線游移", "nervous_tremor": "緊張眼神微顫",
    "alert_scan": "警覺環視", "locked_stare": "集中凝視", "dizzy_dart": "暈眩跳動視線",
}
BLINK_STYLE_LABELS = {
    "normal": "自然眨眼", "teasing_pause": "逗弄停頓眨眼", "shy_fast": "害羞快眨",
    "surprised_hold": "驚訝睜眼停頓", "focused_pause": "專注暫停眨眼", "sleepy_slow": "睏倦慢眨",
}
IDLE_STYLE_LABELS = {
    "neutral_idle": "平靜待機", "happy_idle": "微笑待機", "crying_idle": "難過待機",
    "gloomy_idle": "陰沉待機", "angry_glare_idle": "生氣凝視待機", "shy_idle": "害羞待機",
    "surprised_idle": "驚訝收尾待機", "conflicted_idle": "疑惑待機",
}


class ExpressionPlanDebugRequest(BaseModel):
    modelName: str | None = Field(default=None, min_length=1)
    intent: dict[str, Any] | None = None
    previousState: dict[str, Any] | None = None
    kind: str | None = None
    motionKind: str | None = None
    expressionVariant: str | None = None
    eyeMotionStyle: str | None = None
    blinkStyle: str | None = None
    idleStyle: str | None = None
    intensity: str | None = "normal"
    random: bool = False
    scenario: str | None = None
    seed: int | None = Field(default=None, ge=0, le=2147483647)


def _require_debug_enabled() -> None:
    if not env_flag("EXPRESSION_DEBUG_API_ENABLED", True):
        raise HTTPException(status_code=404, detail="Expression debug API is disabled")


@router.get("/api/debug/expression-catalog")
async def get_debug_expression_catalog() -> dict[str, Any]:
    _require_debug_enabled()
    return {
        "modelName": "Rushia",
        "expressionFamilies": [
            {"id": family, "label": DEBUG_EXPRESSION_RULES[family]["label"],
             "variants": [{"id": variant, "label": EXPRESSION_VARIANT_LABELS.get(variant, variant)}
                          for variant, _patch in variants]}
            for family, variants in FAMILY_VARIANTS.items()
        ],
        "motions": [
            {"id": name, "label": rule["label"], "theme": rule["motion_theme"],
             "expressionKind": rule["expression"]}
            for name, rule in DEBUG_MOTION_RULES.items()
        ],
        "eyeStyles": [{"id": name, "label": EYE_STYLE_LABELS.get(name, name)} for name in EYE_MOTION_PRESETS],
        "blinkStyles": [{"id": name, "label": BLINK_STYLE_LABELS.get(name, name)} for name in BLINK_STRATEGIES],
        "idleStyles": [{"id": name, "label": IDLE_STYLE_LABELS.get(name, name), "family": family}
                       for name, family in RUSHIA_IDLE_FAMILIES.items()],
        "scenarios": [{"id": name, **item} for name, item in DEBUG_SCENARIOS.items()],
    }


def _debug_overrides(payload: ExpressionPlanDebugRequest, intent: dict[str, Any]) -> dict[str, str]:
    result = {}
    options = {
        "expressionVariant": (payload.expressionVariant, "expression_variant",
                              {name for variants in FAMILY_VARIANTS.values() for name, _patch in variants}),
        "eyeMotionStyle": (payload.eyeMotionStyle, "eye_motion_style", EYE_MOTION_PRESETS),
        "blinkStyle": (payload.blinkStyle, "blink_style", BLINK_STRATEGIES),
        "idleStyle": (payload.idleStyle, "idle_style", RUSHIA_IDLE_FAMILIES),
    }
    for key, (explicit, intent_key, allowed) in options.items():
        value = explicit if explicit is not None else intent.get(intent_key)
        # Default blink policies from fake replies retain their existing safety
        # behavior; only a supplied override changes closed-eye composition.
        if key == "blinkStyle" and explicit is None and payload.intent is None:
            continue
        if value is not None:
            if not isinstance(value, str) or value not in allowed:
                raise ValueError(f"Unknown debug {key}: {value!r}")
            result[key] = value
    family = intent.get("expression_family")
    if family is not None:
        family = canonical_rushia_family(family)
        if family not in FAMILY_VARIANTS:
            raise ValueError(f"Unknown debug expression family: {family!r}")
        intent["expression_family"] = family
    theme = intent.get("motion_theme")
    variant = intent.get("motion_variant")
    if theme is not None and theme not in MOTION_BRANCH_LIBRARY:
        raise ValueError(f"Unknown debug motion theme: {theme!r}")
    if variant is not None:
        if variant not in DEBUG_MOTION_RULES:
            raise ValueError(f"Unknown debug motion variant: {variant!r}")
        expected = DEBUG_MOTION_RULES[variant]["motion_theme"]
        if theme != expected:
            raise ValueError(f"Motion variant {variant!r} requires theme {expected!r}")
    return result


@router.post("/api/debug/expression-plan")
async def compile_debug_expression_plan(payload: ExpressionPlanDebugRequest) -> dict[str, Any]:
    _require_debug_enabled()
    for key, value, allowed in (
        ("kind", payload.kind, {*DEBUG_EXPRESSION_RULES, *FAMILY_ALIASES, "random"}),
        ("motionKind", payload.motionKind, DEBUG_MOTION_RULES),
        ("scenario", payload.scenario, DEBUG_SCENARIOS),
    ):
        if value is not None and value not in allowed:
            raise HTTPException(status_code=400, detail=f"Unknown debug {key}: {value!r}")

    model_name = "Rushia"

    debug_case: dict[str, Any] | None = None
    if payload.intent is not None:
        expression_intent = dict(payload.intent)
        expression_intent.setdefault("spoken_text", "後端 debug expression_plan 測試。")
        if payload.motionKind is not None:
            motion_rule = DEBUG_MOTION_RULES.get(payload.motionKind)
            if motion_rule is None:
                raise HTTPException(status_code=400, detail=f"Unknown debug motion kind: {payload.motionKind}")
            expression_intent.update({"motion_theme": motion_rule["motion_theme"],
                                      "motion_variant": motion_rule["motion_variant"]})
    else:
        try:
            debug_case = build_fake_expression_debug_case(
                kind=payload.kind,
                motion_kind=payload.motionKind,
                intensity=payload.intensity,
                randomize=payload.random,
                scenario=payload.scenario,
                seed=payload.seed,
            )
            expression_intent = parse_expression_intent(
                debug_case["rawReply"],
                emotion_state=None,
                previous_state=payload.previousState,
                user_message=debug_case["spokenText"],
            )
            expression_intent["spoken_text"] = debug_case["spokenText"]
            if debug_case.get("expressionFamily"):
                expression_intent["expression_family"] = debug_case["expressionFamily"]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        overrides = _debug_overrides(payload, expression_intent)
        if debug_case and debug_case.get("expressionVariant") and "expressionVariant" not in overrides:
            overrides["expressionVariant"] = debug_case["expressionVariant"]
        plan = compile_expression_plan(
            expression_intent,
            model_name=model_name,
            previous_state=payload.previousState,
            seed=payload.seed,
            debug_overrides=overrides,
        )
        if expression_intent.get("motion_variant") is not None:
            requested = expression_intent["motion_variant"]
            if plan["motionPlan"]["variant"] != requested:
                raise ValueError(f"Motion variant {requested!r} conflicts with the resolved expression family")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to compile expression plan: {exc}") from exc

    return {
        "plan": plan,
        "summary": {
            "preset": plan.get("basePose", {}).get("preset"),
            "bodyMotionProfile": plan.get("debug", {}).get("bodyMotionProfile"),
            "idlePlan": plan.get("debug", {}).get("idlePlan"),
            "emotion": plan.get("debug", {}).get("intentEmotion"),
            "label": debug_case.get("label") if debug_case else "custom intent",
            "source": "fake_ai_reply" if debug_case else "direct_intent",
            "rawReply": debug_case.get("rawReply") if debug_case else None,
            "spokenText": debug_case.get("spokenText") if debug_case else expression_intent.get("spoken_text"),
            "motionKind": debug_case.get("motionKind") if debug_case else expression_intent.get("motion_variant"),
            "expressionFamily": plan.get("debug", {}).get("expressionFamily"),
            "expressionVariant": plan.get("debug", {}).get("expressionVariant"),
            "eyeMotionStyle": plan.get("eyeMotionPlan", {}).get("style"),
            "blinkStyle": plan.get("blinkPlan", {}).get("style"),
            "idleStyle": plan.get("idlePlan", {}).get("name"),
            "seed": payload.seed,
        },
    }
