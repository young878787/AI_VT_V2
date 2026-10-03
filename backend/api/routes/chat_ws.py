"""Chat WebSocket：JEV Emotion → 共用 state 的 Chat / JEV Action。"""

import asyncio
import hashlib
import json
import math

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.display_manager import broadcast_to_displays
from core.config import (
    CHAT_PROVIDER,
    CHAT_MODEL_NAME,
    CHAT_PERSISTENCE_ENABLED,
    COMPRESS_KEEP_RECENT,
)
from core.prompt_logger import log_turn, reset_log
from core.utils import normalize_session_id
from domain.agent_a_prompts import build_agent_a_prompt
from domain.emotion_state import EMOTION_FIELDS, resolve_emotion_state, NEUTRAL_EMOTION_STATE
from domain.expression_intent_schema import (
    ALLOWED_EMOTIONS,
    ALLOWED_PERFORMANCE_MODES,
    normalize_expression_intent,
)
from domain.input_event import normalize_chat_input
from domain.jev_questions import (
    BASE_EMOTION_CRITERIA,
    CONFIDENCE_THRESHOLD,
    JEV_DECISION_CRITERIA_VERSION,
    build_action_questions,
    build_jev_context,
    build_jev_questions,
    map_answers_to_intent,
)
from infrastructure.memory_store import (
    load_session_emotion_state,
    load_session_messages,
    load_session_summary,
    reset_session_emotion_state,
    save_session_emotion_state,
    save_session_messages,
    reset_session_summary,
)
from infrastructure.typesafe_client import call_jev
from services.chat_service import (
    stream_agent_a,
    build_chat_context,
    compress_context,
    estimate_token_count,
    synthesize_and_send_voice,
)
from services.expression_compiler import compile_expression_plan
from services.expression_legacy_renderer import render_legacy_behavior_payload


router = APIRouter()


def _fallback_action_intent(previous_state: dict | None) -> dict:
    """Action 失敗時延續可用的上一輪表情；首輪使用 neutral。"""
    if not isinstance(previous_state, dict):
        return {"emotion": "neutral", "performance_mode": "smile"}
    return {
        "emotion": previous_state.get("emotion", "neutral"),
        "performance_mode": previous_state.get("performanceMode", "smile"),
    }


def _choice_fallback_reason(answer: object, allowed: set[str]) -> str:
    if not isinstance(answer, dict):
        return "missing_answer"
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in allowed:
        return "invalid_choice"
    confidence = answer.get("confidence")
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not math.isfinite(confidence)):
        return "invalid_confidence"
    if confidence < CONFIDENCE_THRESHOLD:
        return "low_confidence"
    return "none"


def _record_choice_debug(debug: dict, answers: object, answer_key: str, prefix: str, allowed: set[str]) -> None:
    answer = answers.get(answer_key) if isinstance(answers, dict) else None
    debug[f"jev{prefix}Choice"] = "none"
    if not isinstance(answer, dict):
        return
    choice = answer.get("choice")
    if isinstance(choice, str) and choice in allowed:
        debug[f"jev{prefix}Choice"] = choice
    confidence = answer.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and math.isfinite(confidence):
        debug[f"jev{prefix}Confidence"] = float(confidence)
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, dict):
        for option in sorted(allowed):
            probability = probabilities.get(option)
            if (isinstance(probability, (int, float)) and not isinstance(probability, bool)
                    and math.isfinite(probability) and 0.0 <= probability <= 1.0):
                debug[f"jev{prefix}Probability_{option}"] = float(probability)


def _action_decision_debug(answers: object, previous_state: dict | None, history_count: int) -> dict:
    previous_emotion = previous_state.get("emotion") if isinstance(previous_state, dict) else None
    debug = {
        "jevDecisionCriteriaVersion": JEV_DECISION_CRITERIA_VERSION,
        "jevDecisionHistoryMessages": history_count,
        "jevDecisionPreviousEmotion": (
            previous_emotion if isinstance(previous_emotion, str) and previous_emotion in ALLOWED_EMOTIONS else "none"
        ),
    }
    _record_choice_debug(debug, answers, "base_emotion", "BaseEmotion", set(BASE_EMOTION_CRITERIA))
    _record_choice_debug(
        debug, answers, "interaction_attitude", "InteractionAttitude", ALLOWED_PERFORMANCE_MODES,
    )
    return debug


async def _produce_and_send_action_plan(
    websocket: WebSocket,
    model_name: str,
    answers: object,
    previous_expression_state: dict | None,
    history_count: int,
    question_hash: str,
    turn_id: str | None = None,
    legacy_payloads: bool = False,
    send_func=None,
) -> dict:
    """將單次 JEV 回應的兩個 Choice 編譯為表情 plan。"""
    base_answer = answers.get("base_emotion") if isinstance(answers, dict) else None
    attitude_answer = answers.get("interaction_attitude") if isinstance(answers, dict) else None
    base_fallback_reason = _choice_fallback_reason(base_answer, set(BASE_EMOTION_CRITERIA))
    attitude_fallback_reason = _choice_fallback_reason(attitude_answer, ALLOWED_PERFORMANCE_MODES)
    fallback = _fallback_action_intent(previous_expression_state)
    try:
        intent = map_answers_to_intent(answers if isinstance(answers, dict) else {})
    except Exception as exc:
        print(f"[JEV Decision] 表演欄位解析失敗，使用 fallback: {exc}")
        intent = {}
        base_fallback_reason = "mapping_error"
        attitude_fallback_reason = "mapping_error"
    if "emotion" not in intent:
        intent["emotion"] = fallback["emotion"]
    if "performance_mode" not in intent:
        intent["performance_mode"] = fallback["performance_mode"] if not isinstance(answers, dict) else "smile"

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
        base_fallback_reason = "compiler_error"
        attitude_fallback_reason = "compiler_error"
    decision_debug = _action_decision_debug(
        answers, previous_expression_state, history_count,
    )
    fallback_count = sum(reason != "none" for reason in (base_fallback_reason, attitude_fallback_reason))
    decision_debug["jevDecisionSource"] = (
        "jev" if fallback_count == 0 else "fallback" if fallback_count == 2 else "partial_fallback"
    )
    decision_debug["jevBaseEmotionFallbackReason"] = base_fallback_reason
    decision_debug["jevInteractionAttitudeFallbackReason"] = attitude_fallback_reason
    decision_debug["jevResolvedEmotion"] = plan["debug"]["intentEmotion"]
    decision_debug["jevResolvedAttitude"] = plan["debug"]["intentPerformanceMode"]
    decision_debug["jevDecisionQuestionHash"] = question_hash
    expected_action_fields = set(build_action_questions())
    missing_action_fields = sorted(expected_action_fields - set(answers)) if isinstance(answers, dict) else sorted(expected_action_fields)
    decision_debug["jevDecisionMissingActionFields"] = ",".join(missing_action_fields) or "none"
    plan["debug"].update(decision_debug)
    print("[JEV Decision] " + json.dumps(decision_debug, ensure_ascii=False), flush=True)
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
    app = getattr(websocket, "app", None)
    memory_runtime = getattr(getattr(app, "state", None), "memory_runtime", None)
    if memory_runtime is None:
        raise RuntimeError("MemoryRuntime 尚未啟動")
    messages: list[dict] = []
    session_id: str | None = None
    emotion_state: dict | None = None
    expression_state: dict | None = None
    current_action: dict | None = None
    version = 0
    active_turn_id: str | None = None
    active_task: asyncio.Task | None = None
    active_turn_text: str | None = None
    active_turn_partial: str = ""
    active_turn_committed = False
    active_event_id = None
    active_memory_routed = False
    tts_tasks: set[asyncio.Task] = set()
    send_lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def cancel_active(finalize_memory: bool = True) -> None:
        nonlocal active_task, active_turn_id, active_turn_text, active_turn_partial
        nonlocal active_turn_committed, active_event_id, active_memory_routed, messages
        if active_task is not None and not active_task.done():
            interrupted_turn_id = active_turn_id
            interrupted_text = active_turn_text
            was_committed = active_turn_committed
            interrupted_event_id = active_event_id
            memory_routed = active_memory_routed
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
            if interrupted_turn_id:
                partial = active_turn_partial if not was_committed else ""
                if interrupted_text and not was_committed:
                    messages.append({"role": "user", "content": interrupted_text})
                if partial:
                    messages.append({
                        "role": "assistant", "content": partial, "status": "interrupted",
                    })
                if CHAT_PERSISTENCE_ENABLED and session_id and interrupted_text and not was_committed:
                    save_session_messages(session_id, messages)
                if finalize_memory and interrupted_event_id and not memory_routed and interrupted_text:
                    memory_runtime.route_background(
                        interrupted_event_id, interrupted_text, None, list(messages),
                    )
                await send({
                    "type": "turn_cancelled",
                    "turn_id": interrupted_turn_id,
                    "status": "cancelled" if was_committed else "interrupted",
                    "partial_text": partial,
                })
        active_task = None
        active_turn_id = None
        active_turn_text = None
        active_turn_partial = ""
        active_turn_committed = False
        active_event_id = None
        active_memory_routed = False
        for task in tts_tasks:
            task.cancel()
        tts_tasks.clear()

    async def run_turn(turn_id: str, text: str, model_name: str, snapshot: dict, legacy: bool,
                       event_id=None) -> None:
        nonlocal messages, emotion_state, expression_state, version, active_turn_partial
        nonlocal active_turn_committed, active_memory_routed
        action_task: asyncio.Task | None = None
        try:
            context = build_jev_context(
                text,
                snapshot["messages"],
                snapshot["emotion"],
                snapshot["expression"],
                snapshot["memory"],
                snapshot["action"],
            )
            questions = build_jev_questions()
            question_hash = hashlib.sha256(
                json.dumps(questions, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:12]
            try:
                answers = await call_jev(context, questions)
            except Exception as exc:
                print(f"[JEV Decision] 呼叫失敗，使用 fallback: {exc}")
                answers = None
            if event_id is not None:
                memory_runtime.route_background(event_id, text, answers, snapshot["messages"])
                active_memory_routed = True
            emotion_answers = (
                {field: answers.get(field) for field in EMOTION_FIELDS}
                if isinstance(answers, dict) else None
            )
            next_emotion, source = resolve_emotion_state(emotion_answers, snapshot["emotion"])
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
                websocket,
                model_name,
                answers,
                snapshot["expression"],
                len(context["recent_dialogue"]),
                question_hash,
                turn_id,
                legacy,
                send,
            ))

            async def send_chunk(piece: str) -> None:
                nonlocal active_turn_partial
                if active_turn_id == turn_id:
                    await send({"type": "text_stream", "content": piece, "turn_id": turn_id})
                    if active_turn_id == turn_id:
                        active_turn_partial += piece

            reply = await stream_agent_a(chat_messages, send_chunk)
            if not reply:
                reply = "嗯……"
                await send_chunk(reply)
            if active_turn_id != turn_id:
                return
            messages.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply}])
            active_turn_committed = True
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
            print(f"[Chat error][{CHAT_PROVIDER.upper()}] Model={CHAT_MODEL_NAME} | {exc}")
            if active_turn_id == turn_id:
                await send({"type": "error", "content": f"API 錯誤: {exc}", "turn_id": turn_id})
        finally:
            if action_task is not None and not action_task.done():
                action_task.cancel()
                await asyncio.gather(action_task, return_exceptions=True)

    async def prepare_and_run_turn(
        turn_id: str, text: str, model_name: str, legacy: bool,
    ) -> None:
        """將記憶接收／檢索與生成放在同一個可取消的回合 task。"""
        nonlocal active_event_id
        event_id = None
        try:
            event_id = await memory_runtime.accept(session_id or "default_session", turn_id, text, list(messages))
            active_event_id = event_id
            await send({"type": "input_accepted", "turn_id": turn_id, "event_id": event_id.hex})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[Memory] 無法持久化輸入事件: {type(exc).__name__}")
            await send({"type": "memory_enqueue_error", "turn_id": turn_id})
        profile, relevant_memory = await memory_runtime.retrieve(text, event_id=event_id)
        snapshot = {
            "messages": list(messages), "emotion": emotion_state,
            "expression": expression_state,
            "action": current_action,
            "profile": profile,
            "memory": relevant_memory,
            "summary": load_session_summary(session_id) if session_id else "",
        }
        await run_turn(turn_id, text, model_name, snapshot, legacy, event_id)

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
                await cancel_active(finalize_memory=False)
                await memory_runtime.reset()
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
            # 先切換回合，讓新輸入可以立即打斷生成中的舊回合；記憶檢索不能阻塞取消。
            await cancel_active()
            active_turn_id = turn_id
            active_turn_text = text
            active_turn_partial = ""
            active_turn_committed = False
            active_event_id = None
            active_memory_routed = False
            active_task = asyncio.create_task(prepare_and_run_turn(
                turn_id, text, model_name, data.get("legacy_payloads") is True,
            ))
    except WebSocketDisconnect:
        print("Client disconnected")
    finally:
        await cancel_active()
