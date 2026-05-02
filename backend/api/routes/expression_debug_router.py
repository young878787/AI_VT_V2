"""Development endpoints for compiling Live2D expression plans without AI chat."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import MODEL_NAME
from core.utils import env_flag
from services.expression_compiler import compile_expression_plan

router = APIRouter()


class ExpressionPlanDebugRequest(BaseModel):
    modelName: str | None = Field(default=None, min_length=1)
    intent: dict[str, Any] = Field(default_factory=dict)
    previousState: dict[str, Any] | None = None


@router.post("/api/debug/expression-plan")
async def compile_debug_expression_plan(payload: ExpressionPlanDebugRequest) -> dict[str, Any]:
    if not env_flag("EXPRESSION_DEBUG_API_ENABLED", True):
        raise HTTPException(status_code=404, detail="Expression debug API is disabled")

    model_name = (payload.modelName or MODEL_NAME or "Hiyori").strip()
    if not model_name:
        model_name = "Hiyori"

    if not payload.intent:
        raise HTTPException(status_code=400, detail="intent is required")

    try:
        plan = compile_expression_plan(
            dict(payload.intent),
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
        },
    }
