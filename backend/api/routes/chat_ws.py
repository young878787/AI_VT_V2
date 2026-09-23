"""Chat WebSocket：JEV Emotion → 共用 state 的 Chat / JEV Action。"""

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.display_manager import broadcast_to_displays
from core.config import (
    AI_PROVIDER,
    MODEL_NAME,
    CHAT_PERSISTENCE_ENABLED,
    COMPRESS_KEEP_RECENT,
)
from core.prompt_logger import log_turn, reset_log
from core.utils import normalize_session_id
from domain.agent_a_prompts import build_agent_a_prompt
from domain.emotion_state import resolve_emotion_state, NEUTRAL_EMOTION_STATE
from domain.expression_intent_schema import ALLOWED_EMOTIONS, normalize_expression_intent
from domain.input_event import normalize_chat_input
from domain.jev_questions import (
    build_action_context,
    build_action_questions,
    build_emotion_context,
    build_emotion_questions,
    map_answers_to_intent,
)
from infrastructure.memory_store import (
    load_session_emotion_state,
    load_session_messages,
    load_session_summary,
    load_user_profile,
    reset_session_emotion_state,
    save_session_emotion_state,
    save_session_messages,
    reset_session_summary,
)
from infrastructure.typesafe_client import call_jev
from infrastructure.memory_records import search_relevant_records
from services.agent_tool_pipeline import (
    MEMORY_AGENT_ALLOWED_TOOL_NAMES,
    filter_tool_calls_for_pool,
    get_meaningful_memory_tool_arguments,
)
from services.chat_service import (
    stream_agent_a,
    build_chat_context,
    compress_context,
    estimate_token_count,
    synthesize_and_send_voice,
)
from services.expression_compiler import compile_expression_plan
from services.expression_legacy_renderer import render_legacy_behavior_payload
from services.memory_jobs import enqueue_input
from services.memory_jobs import reset_epoch


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
    turn_id: str | None = None,
    legacy_payloads: bool = False,
    send_func=None,
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
    render = render_legacy_behavior_payload(plan) if legacy_payloads else None
    if turn_id:
        plan = {**plan, "turn_id": turn_id}
    send = send_func or websocket.send_json
    await send(plan)
    await broadcast_to_displays(plan)
    if render:
        for blink in render["blink_payloads"]:
            await send(blink)
            await broadcast_to_displays(blink)
        await send(render["behavior_payload"])
        await broadcast_to_displays(render["behavior_payload"])
    return {"plan": plan, "speaking_rate": plan.get("speakingRate", 1.0)}


@router.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    messages: list[dict] = []
    session_id: str | None = None
    emotion_state: dict | None = None
    expression_state: dict | None = None
    current_action: dict | None = None
    version = 0
    active_turn_id: str | None = None
    active_task: asyncio.Task | None = None
    tts_tasks: set[asyncio.Task] = set()
    send_lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def cancel_active() -> None:
        nonlocal active_task, active_turn_id
        if active_task is not None and not active_task.done():
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
            if active_turn_id:
                await send({"type": "turn_cancelled", "turn_id": active_turn_id})
        active_task = None
        active_turn_id = None
        for task in tts_tasks:
            task.cancel()
        tts_tasks.clear()

    async def run_turn(turn_id: str, text: str, model_name: str, snapshot: dict, legacy: bool) -> None:
        nonlocal messages, emotion_state, expression_state, version
        action_task: asyncio.Task | None = None
        try:
            context = build_emotion_context(text, snapshot["messages"], snapshot["emotion"], snapshot["memory"])
            if snapshot["action"]:
                context["current_action"] = snapshot["action"]
            try:
                answers = await call_jev(context, build_emotion_questions())
            except Exception as exc:
                print(f"[JEV Emotion] 呼叫失敗，使用 fallback: {exc}")
                answers = None
            next_emotion, source = resolve_emotion_state(answers, snapshot["emotion"])
            if active_turn_id != turn_id:
                return
            emotion_state = next_emotion
            version += 1
            if CHAT_PERSISTENCE_ENABLED and session_id:
                save_session_emotion_state(session_id, next_emotion)
            await send({"type": "emotion_update", "state": next_emotion, "source": source,
                        "turn_id": turn_id, "version": version})

            prompt = build_agent_a_prompt(snapshot["profile"], snapshot["memory"], next_emotion, model_name=model_name)
            if snapshot["summary"]:
                prompt += "\n\n本 session 已完成的對話摘要：\n" + snapshot["summary"][:4000]
            chat_messages = build_chat_context(prompt, snapshot["messages"], text)
            action_task = asyncio.create_task(_produce_and_send_action_plan(
                websocket, model_name, context, next_emotion, snapshot["expression"], turn_id, legacy, send,
            ))

            async def send_chunk(piece: str) -> None:
                if active_turn_id == turn_id:
                    await send({"type": "text_stream", "content": piece, "turn_id": turn_id})

            reply = await stream_agent_a(chat_messages, send_chunk)
            if not reply:
                reply = "嗯……"
                await send_chunk(reply)
            if active_turn_id != turn_id:
                return
            messages.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
            if CHAT_PERSISTENCE_ENABLED and session_id:
                save_session_messages(session_id, messages)
            log_turn(turn_count=sum(item.get("role") == "user" for item in messages),
                     system_prompt=prompt, user_message=text, dialogue_agent_output=reply,
                     tool_names=[], output_tokens=estimate_token_count([{"role": "assistant", "content": reply}]))
            await send({"type": "stream_end", "turn_id": turn_id})
            # Action 可以在文字完成後才到；TTS 不等待它。
            speaking_rate = 1.0
            if action_task.done() and not action_task.cancelled() and action_task.exception() is None:
                speaking_rate = action_task.result()["speaking_rate"]
            task = asyncio.create_task(synthesize_and_send_voice(websocket, reply, speaking_rate, turn_id, send))
            tts_tasks.add(task)
            task.add_done_callback(tts_tasks.discard)
            result = await action_task
            action_task = None
            if active_turn_id == turn_id:
                expression_state = result["plan"].get("carryState")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Chat error][{AI_PROVIDER.upper()}] Model={MODEL_NAME} | {exc}")
            if active_turn_id == turn_id:
                await send({"type": "error", "content": f"API 錯誤: {exc}", "turn_id": turn_id})
        finally:
            if action_task is not None and not action_task.done():
                action_task.cancel()
                await asyncio.gather(action_task, return_exceptions=True)

    try:
        while True:
            data = json.loads(await websocket.receive_text())
            if not isinstance(data, dict):
                continue
            incoming_session = normalize_session_id(data.get("session_id")) or session_id
            if incoming_session != session_id:
                await cancel_active()
                session_id = incoming_session
                expression_state = None
                current_action = None
                if CHAT_PERSISTENCE_ENABLED and session_id:
                    messages = load_session_messages(session_id)
                    emotion_state = load_session_emotion_state(session_id)
                else:
                    messages = []
                    emotion_state = None
                version = 0

            control_type = data.get("type")
            if control_type == "reset":
                await cancel_active()
                reset_epoch()
                messages, emotion_state, expression_state = [], None, None
                current_action = None
                version += 1
                if CHAT_PERSISTENCE_ENABLED and session_id:
                    save_session_messages(session_id, [])
                    reset_session_emotion_state(session_id)
                    reset_session_summary(session_id)
                reset_log()
                await send({"type": "emotion_update", "state": dict(NEUTRAL_EMOTION_STATE),
                            "source": "neutral_fallback"})
                await send({"type": "reset_done"})
                continue
            if control_type == "sync":
                await send({"type": "emotion_update", "state": emotion_state or dict(NEUTRAL_EMOTION_STATE),
                            "source": "previous_fallback" if emotion_state else "neutral_fallback",
                            "version": version})
                continue
            if control_type == "action_state":
                # 前端的播放進度是動作真值；僅接受目前輪次的回報。
                action_id = data.get("action_id")
                if (data.get("turn_id") == active_turn_id and data.get("status") in {"started", "finished", "cancelled"}
                        and isinstance(action_id, str) and 0 < len(action_id) <= 128):
                    current_action = ({"action_id": action_id, "status": "started"}
                                      if data["status"] == "started" else None)
                continue
            if control_type == "compress":
                if len(messages) > COMPRESS_KEEP_RECENT + 1:
                    messages = await compress_context(messages, websocket, session_id, send)
                    if CHAT_PERSISTENCE_ENABLED and session_id:
                        save_session_messages(session_id, messages)
                else:
                    await send({"type": "compress_done"})
                continue
            input_event = normalize_chat_input(data, session_id)
            if input_event is None:
                continue
            text = input_event["text"]
            model_name = input_event["model_name"]
            turn_id = input_event["turn_id"]
            snapshot = {
                "messages": list(messages), "emotion": emotion_state,
                "expression": expression_state,
                "action": current_action,
                "profile": json.loads(json.dumps(load_user_profile(), ensure_ascii=False)),
                "memory": search_relevant_records(text),
                "summary": load_session_summary(session_id) if session_id else "",
            }
            try:
                event_id = enqueue_input(session_id or "default_session", turn_id, text, model_name, snapshot["messages"], input_event["source"], input_event["timestamp"])
                await send({"type": "input_accepted", "turn_id": turn_id, "event_id": event_id})
            except Exception as exc:
                print(f"[Memory] 無法持久化輸入事件: {exc}")
                await send({"type": "memory_enqueue_error", "turn_id": turn_id})
            await cancel_active()
            active_turn_id = turn_id
            active_task = asyncio.create_task(run_turn(turn_id, text, model_name, snapshot, data.get("legacy_payloads") is True))
    except WebSocketDisconnect:
        print("Client disconnected")
    finally:
        await cancel_active()
