"""Chat WebSocket：JEV Emotion → 共用 state 的 Chat / JEV Action。"""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.display_manager import broadcast_to_displays
from core.config import (
    AI_PROVIDER,
    MODEL_NAME,
    CHAT_PERSISTENCE_ENABLED,
    COMPRESS_TOKEN_THRESHOLD,
    COMPRESS_KEEP_RECENT,
)
from core.prompt_logger import log_turn, reset_log
from core.utils import normalize_session_id
from domain.agent_a_prompts import build_agent_a_prompt
from domain.agent_b_prompts import build_memory_prompt
from domain.emotion_state import resolve_emotion_state, NEUTRAL_EMOTION_STATE
from domain.expression_intent_schema import ALLOWED_EMOTIONS, normalize_expression_intent
from domain.jev_questions import (
    build_action_context,
    build_action_questions,
    build_emotion_context,
    build_emotion_questions,
    map_answers_to_intent,
)
from domain.tools.schema_loader import normalize_model_name
from infrastructure.memory_store import (
    append_memory_note,
    load_memory_notes,
    load_session_emotion_state,
    load_session_messages,
    load_user_profile,
    reset_session_emotion_state,
    save_session_emotion_state,
    save_session_messages,
)
from infrastructure.typesafe_client import call_jev
from services.agent_tool_pipeline import (
    MEMORY_AGENT_ALLOWED_TOOL_NAMES,
    extract_agent_tool_calls,
    filter_tool_calls_for_pool,
    get_meaningful_memory_tool_arguments,
    summarize_tool_names,
)
from services.chat_service import (
    call_memory_agent,
    collect_agent_a,
    compress_context,
    estimate_token_count,
    synthesize_and_send_voice,
)
from services.expression_compiler import compile_expression_plan
from services.expression_legacy_renderer import render_legacy_behavior_payload
from services.memory_service import execute_profile_update


router = APIRouter()


async def _execute_memory_tool_calls(
    memory_calls: list[dict],
    websocket: WebSocket,
    broadcast_func,
    execute_profile_update_fn,
    append_memory_note_fn,
    model_name: str = "Hiyori",
) -> dict:
    """只執行 Memory Agent 的有效記憶工具呼叫。"""
    del websocket, broadcast_func
    calls = filter_tool_calls_for_pool(
        memory_calls,
        allowed_tool_names=MEMORY_AGENT_ALLOWED_TOOL_NAMES,
        label="Memory Agent",
    )
    filtered = []
    for call in calls:
        name = call["name"]
        args = get_meaningful_memory_tool_arguments(name, call["arguments"], model_name=model_name)
        if args is None:
            continue
        if name == "update_user_profile":
            execute_profile_update_fn(args["action"], args["field"], args["value"], model_name=model_name)
        elif name == "save_memory_note":
            append_memory_note_fn(args["content"])
        else:
            continue
        filtered.append({**call, "arguments": args})
    return {"memory_calls": filtered}


def _fallback_action_intent(previous_state: dict | None) -> dict:
    """Action 失敗時延續可用的上一輪表情；首輪使用 neutral。"""
    if not isinstance(previous_state, dict):
        return {"emotion": "neutral", "performance_mode": "smile"}
    return {
        "emotion": previous_state.get("emotion", "neutral"),
        "performance_mode": previous_state.get("performanceMode", "smile"),
    }


async def _produce_and_send_action_plan(
    websocket: WebSocket,
    model_name: str,
    emotion_context: dict,
    emotion_state: dict,
    previous_expression_state: dict | None,
) -> dict:
    """JEV Action 與 Chat 同時執行；完成後提早送出表情 plan。"""
    try:
        questions = build_action_questions()
        answers = await call_jev(
            build_action_context(emotion_context, emotion_state, previous_expression_state),
            questions,
        )
        if not isinstance(answers, dict) or set(answers) != set(questions):
            raise ValueError("JEV Action answers 缺欄位")
        intent = map_answers_to_intent(answers)
        if intent.get("emotion") not in ALLOWED_EMOTIONS:
            raise ValueError("JEV Action 未產生有效 emotion choice")
    except Exception as exc:
        print(f"[JEV Action] 使用上一輪表情 fallback: {exc}")
        intent = _fallback_action_intent(previous_expression_state)

    intent["speaking_rate"] = {
        "happy": 1.25, "playful": 1.25, "teasing": 1.25,
        "sad": 0.8, "gloomy": 0.8, "shy": 0.95, "surprised": 1.15,
    }.get(intent.get("emotion"), 1.0)
    try:
        normalized = normalize_expression_intent(intent)
        plan = compile_expression_plan(
            normalized,
            model_name=model_name,
            previous_state=previous_expression_state,
        )
    except Exception as exc:
        print(f"[JEV Action] compiler fallback 至 neutral: {exc}")
        normalized = normalize_expression_intent({"emotion": "neutral", "performance_mode": "smile"})
        plan = compile_expression_plan(normalized, model_name=model_name, previous_state=None)
    render = render_legacy_behavior_payload(plan)
    await websocket.send_json(plan)
    await broadcast_to_displays(plan)
    for blink in render["blink_payloads"]:
        await websocket.send_json(blink)
        await broadcast_to_displays(blink)
    await websocket.send_json(render["behavior_payload"])
    await broadcast_to_displays(render["behavior_payload"])
    return {"plan": plan, "speaking_rate": render["speaking_rate"]}


@router.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    messages: list[dict] = []
    current_session_id: str | None = None
    previous_emotion_state: dict | None = None
    previous_expression_state: dict | None = None
    tts_tasks: set[asyncio.Task] = set()
    action_task: asyncio.Task | None = None

    try:
        while True:
            data = json.loads(await websocket.receive_text())
            incoming_session_id = normalize_session_id(data.get("session_id")) or current_session_id
            if incoming_session_id != current_session_id:
                current_session_id = incoming_session_id
                previous_expression_state = None
                if CHAT_PERSISTENCE_ENABLED and current_session_id:
                    messages = load_session_messages(current_session_id)
                    previous_emotion_state = load_session_emotion_state(current_session_id)
                else:
                    messages = []
                    previous_emotion_state = None

            if data.get("type") == "compress":
                if len(messages) > COMPRESS_KEEP_RECENT + 1:
                    messages = await compress_context(messages, websocket)
                    if CHAT_PERSISTENCE_ENABLED and current_session_id:
                        save_session_messages(current_session_id, messages)
                else:
                    await websocket.send_json({"type": "compress_done"})
                continue

            if data.get("type") == "reset":
                messages = []
                previous_emotion_state = None
                previous_expression_state = None
                if CHAT_PERSISTENCE_ENABLED and current_session_id:
                    save_session_messages(current_session_id, [])
                    reset_session_emotion_state(current_session_id)
                reset_log()
                await websocket.send_json({
                    "type": "emotion_update",
                    "state": dict(NEUTRAL_EMOTION_STATE),
                    "source": "neutral_fallback",
                })
                await websocket.send_json({"type": "reset_done"})
                continue

            if data.get("type") == "sync":
                await websocket.send_json({
                    "type": "emotion_update",
                    "state": previous_emotion_state or dict(NEUTRAL_EMOTION_STATE),
                    "source": "previous_fallback" if previous_emotion_state else "neutral_fallback",
                })
                continue

            user_message = data.get("content")
            if not isinstance(user_message, str) or not user_message.strip():
                continue
            model_name = normalize_model_name(data.get("model_name", "Hiyori"))
            emotion_context = build_emotion_context(user_message, messages, previous_emotion_state)
            try:
                emotion_answers = await call_jev(emotion_context, build_emotion_questions())
            except Exception as exc:
                print(f"[JEV Emotion] 呼叫失敗，使用 fallback: {exc}")
                emotion_answers = None
            emotion_state, source = resolve_emotion_state(emotion_answers, previous_emotion_state)
            previous_emotion_state = emotion_state
            if CHAT_PERSISTENCE_ENABLED and current_session_id:
                save_session_emotion_state(current_session_id, emotion_state)
            await websocket.send_json({"type": "emotion_update", "state": emotion_state, "source": source})

            prompt = build_agent_a_prompt(
                load_user_profile(), load_memory_notes(), emotion_state, model_name=model_name,
            )
            if messages and messages[0].get("role") == "system":
                messages[0] = {"role": "system", "content": prompt}
            else:
                messages.insert(0, {"role": "system", "content": prompt})
            messages.append({"role": "user", "content": user_message})

            action_task = asyncio.create_task(_produce_and_send_action_plan(
                websocket, model_name, emotion_context, emotion_state, previous_expression_state,
            ))
            try:
                agent_a_text = await collect_agent_a(messages)
                if not agent_a_text:
                    agent_a_text = "嗯……"
                await websocket.send_json({"type": "text_stream", "content": agent_a_text})
                action_result = await action_task
                action_task = None
                plan = action_result["plan"]
                previous_expression_state = plan.get("carryState")
                messages.append({"role": "assistant", "content": agent_a_text})

                memory_prompt = build_memory_prompt(user_message, agent_a_text, model_name)
                memory_response = await call_memory_agent([
                    {"role": "system", "content": memory_prompt},
                    {"role": "user", "content": "請分析用戶訊息，判斷是否需要記憶操作。"},
                ], model_name)
                memory_calls = extract_agent_tool_calls(
                    memory_response, model_name=model_name, label="Memory Agent",
                )
                executed = await _execute_memory_tool_calls(
                    memory_calls, websocket, broadcast_to_displays,
                    execute_profile_update, append_memory_note, model_name,
                )
                turn_count = sum(m.get("role") == "user" for m in messages)
                log_turn(
                    turn_count=turn_count,
                    system_prompt=prompt,
                    user_message=user_message,
                    dialogue_agent_output=agent_a_text,
                    tool_names=summarize_tool_names(executed["memory_calls"]),
                    output_tokens=estimate_token_count([{"role": "assistant", "content": agent_a_text}]),
                )
                if estimate_token_count(messages) >= COMPRESS_TOKEN_THRESHOLD:
                    messages = await compress_context(messages, websocket)
                if CHAT_PERSISTENCE_ENABLED and current_session_id:
                    save_session_messages(current_session_id, messages)
                await websocket.send_json({"type": "stream_end"})
                task = asyncio.create_task(synthesize_and_send_voice(
                    websocket, agent_a_text, action_result["speaking_rate"],
                ))
                tts_tasks.add(task)
                task.add_done_callback(tts_tasks.discard)
            except Exception as exc:
                if action_task is not None:
                    action_task.cancel()
                    await asyncio.gather(action_task, return_exceptions=True)
                    action_task = None
                if messages and messages[-1].get("role") == "user":
                    messages.pop()
                print(f"[Chat error][{AI_PROVIDER.upper()}] Model={MODEL_NAME} | {exc}")
                await websocket.send_json({"type": "error", "content": f"API 錯誤: {exc}"})
                raise
    except WebSocketDisconnect:
        print("Client disconnected")
    finally:
        if action_task is not None:
            action_task.cancel()
        for task in tts_tasks:
            task.cancel()
