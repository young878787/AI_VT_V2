"""Development endpoints for compiling Live2D expression plans without AI chat."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import MODEL_NAME
from core.utils import env_flag
from domain.expression_debug_fixtures import build_fake_expression_debug_case
from services.expression_compiler import compile_expression_plan
from services.expression_intent_parser import parse_expression_intent

router = APIRouter()


class ExpressionPlanDebugRequest(BaseModel):
    modelName: str | None = Field(default=None, min_length=1)
    intent: dict[str, Any] | None = None
    previousState: dict[str, Any] | None = None
    kind: str | None = None
    motionKind: str | None = None
    intensity: str | None = "normal"
    random: bool = False
    scenario: str | None = None


@router.post("/api/debug/expression-plan")
async def compile_debug_expression_plan(payload: ExpressionPlanDebugRequest) -> dict[str, Any]:
    if not env_flag("EXPRESSION_DEBUG_API_ENABLED", True):
        raise HTTPException(status_code=404, detail="Expression debug API is disabled")

    model_name = (payload.modelName or MODEL_NAME or "Hiyori").strip()
    if not model_name:
        model_name = "Hiyori"

    debug_case: dict[str, Any] | None = None
    if payload.intent:
        expression_intent = dict(payload.intent)
        expression_intent.setdefault("spoken_text", "後端 debug expression_plan 測試。")
    else:
        try:
            debug_case = build_fake_expression_debug_case(
                kind=payload.kind,
                motion_kind=payload.motionKind,
                intensity=payload.intensity,
                randomize=payload.random,
                scenario=payload.scenario,
            )
            expression_intent = parse_expression_intent(
                debug_case["rawReply"],
                emotion_state=None,
                previous_state=payload.previousState,
                user_message=debug_case["spokenText"],
            )
            expression_intent["spoken_text"] = debug_case["spokenText"]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        plan = compile_expression_plan(
            expression_intent,
            model_name=model_name,
            previous_state=payload.previousState,
        )
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
        },
    }
