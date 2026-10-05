"""Chat WebSocket：JEV Emotion → 共用 state 的 Chat / JEV Action。"""

import asyncio
import hashlib
import json
import math
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.display_manager import broadcast_to_displays
from core.config import (
    CHAT_PROVIDER,
    CHAT_MODEL_NAME,
    CHAT_PERSISTENCE_ENABLED,
    COMPRESS_KEEP_RECENT,
    CHAT_CONTEXT_TOKEN_BUDGET,
)
from core.prompt_logger import log_turn, reset_log, trace, trace_event, trace_turn, bind_trace_event
from core.utils import normalize_session_id
from domain.agent_a_prompts import build_agent_a_prompt, build_turn_scope_hint
from domain.emotion_state import EMOTION_FIELDS, resolve_emotion_state, NEUTRAL_EMOTION_STATE
from domain.expression_intent_schema import (
    ALLOWED_EMOTIONS,
    ALLOWED_PERFORMANCE_MODES,
    normalize_expression_intent,
)
from domain.input_event import normalize_chat_input
from domain.chat_test_mode import resolve_test_mode
from domain.memory_routing import instruction_policy, POLICY_VERSION
from domain.memory_source import MemoryEventConflict, MemoryEventReplay, build_user_message
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
    retained_prompt_ranges,
    compress_context,
    estimate_token_count,
    synthesize_and_send_voice,
)
from services.expression_compiler import compile_expression_plan
from services.expression_legacy_renderer import render_legacy_behavior_payload
from services.memory_events import recent_memory_events, subscribe_memory_events


router = APIRouter()


def _memory_status_message(event: dict) -> tuple[str, bool] | None:
    """將背景記憶事件轉成聊天可讀訊息；回傳值的第二欄表示是否結束追蹤。"""
    event_type = event.get("type")
    route = event.get("route")
    status = event.get("status")
    if event_type == "memory_route_pending":
        return "記憶：已收到，等待背景判定。", False
    if event_type == "memory_route_finalized":
        if route == "none":
            return "記憶：本輪不需要寫入。", True
        return "記憶：已排入背景整理。", False
    if event_type == "memory_committed":
        return "記憶：已完成寫入／更新。", True
    if event_type == "memory_job_finished":
        messages = {
            "ignored": "記憶：本輪未產生需要保存的變更。",
            "buffered": "記憶：需要更多上下文，稍後再整理。",
            "failed": "記憶：寫入失敗，請查看後端紀錄。",
        }
        if status in messages:
            return messages[status], status != "retry"
    return None


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
    active_turn_message: dict | None = None
    active_turn_partial: str = ""
    active_turn_committed = False
    active_event_id = None
    active_memory_routed = False
    short_term_enabled = True
    tts_tasks: set[asyncio.Task] = set()
    send_lock = asyncio.Lock()
    memory_event_queue: asyncio.Queue[dict] = asyncio.Queue()
    memory_event_turn_ids: dict[str, str] = {}
    queued_memory_revisions: set[int] = set()

    async def send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    def queue_memory_event(event: dict) -> None:
        event_id = str(event.get("event_id") or "")
        revision = event.get("revision")
        if event_id not in memory_event_turn_ids or revision in queued_memory_revisions:
            return
        if isinstance(revision, int):
            queued_memory_revisions.add(revision)
        memory_event_queue.put_nowait(event)

    unsubscribe_memory_events = subscribe_memory_events(queue_memory_event)

    async def relay_memory_events() -> None:
        try:
            while True:
                event = await memory_event_queue.get()
                status = _memory_status_message(event)
                if status is None:
                    continue
                message, terminal = status
                event_id = str(event.get("event_id") or "")
                turn_id = memory_event_turn_ids.get(event_id)
                if turn_id is None:
                    continue
                await send({
                    "type": "memory_status",
                    "turn_id": turn_id,
                    "event_id": event_id,
                    "status": event.get("status") or event.get("type"),
                    "content": message,
                })
                if terminal:
                    memory_event_turn_ids.pop(event_id, None)
        except asyncio.CancelledError:
            raise
        except Exception:
            # WebSocket 關閉時停止 UI 事件轉發，不影響背景 worker。
            return

    memory_event_task = asyncio.create_task(relay_memory_events())

    async def cancel_active(finalize_memory: bool = True) -> None:
        nonlocal active_task, active_turn_id, active_turn_text, active_turn_message, active_turn_partial
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
                if short_term_enabled and interrupted_text and not was_committed:
                    messages.append(dict(active_turn_message) if active_turn_message else {
                        "role": "user", "content": interrupted_text,
                    })
                if short_term_enabled and partial:
                    messages.append({
                        "role": "assistant", "content": partial, "status": "interrupted",
                    })
                if short_term_enabled and CHAT_PERSISTENCE_ENABLED and session_id and interrupted_text and not was_committed:
                    save_session_messages(session_id, messages)
                if finalize_memory and interrupted_event_id and not memory_routed and interrupted_text:
                    memory_runtime.route_background(
                        interrupted_event_id, interrupted_text, None, list(messages),
                        turn_id=interrupted_turn_id,
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
        active_turn_message = None
        active_turn_partial = ""
        active_turn_committed = False
        active_event_id = None
        active_memory_routed = False
        for task in tts_tasks:
            task.cancel()
        tts_tasks.clear()

    async def run_turn(turn_id: str, text: str, model_name: str, snapshot: dict, legacy: bool,
                       event_id=None, request_started: float | None = None) -> None:
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
                jev_started = time.monotonic()
                answers = await call_jev(context, questions)
            except Exception as exc:
                print(f"[JEV Decision] 呼叫失敗，使用 fallback: {exc}")
                answers = None
            trace("jev", {"answers": answers, "duration_sec": round(time.monotonic() - jev_started, 4),
                "policy": instruction_policy(text), "policy_version": POLICY_VERSION,
                "question_hash": question_hash, "error": "jev_call_failed" if answers is None else None})
            if event_id is not None:
                memory_runtime.route_background(
                    event_id, text, answers, snapshot["messages"], turn_id=turn_id,
                )
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
            if short_term_enabled and CHAT_PERSISTENCE_ENABLED and session_id:
                save_session_emotion_state(session_id, next_emotion)
            await send({"type": "emotion_update", "state": next_emotion, "source": source,
                        "turn_id": turn_id, "version": version})

            prompt = build_agent_a_prompt(snapshot["profile"], snapshot["memory"], next_emotion, model_name=model_name)
            if snapshot["summary"]:
                prompt += "\n\n本 session 已完成的對話摘要：\n" + snapshot["summary"][:4000]
            scope_hint = build_turn_scope_hint(text, snapshot["messages"])
            if scope_hint:
                prompt += "\n\n本輪對象約束：\n" + scope_hint
            chat_messages = build_chat_context(prompt, snapshot["messages"], text)
            profile_marker = "<untrusted_user_profile>\n"
            profile_marker_start = prompt.find(profile_marker)
            profile_start = (profile_marker_start + len(profile_marker)
                             if profile_marker_start >= 0 else prompt.find("使用者資料：\n") + len("使用者資料：\n"))
            trace("chat_context", {"messages": chat_messages, "session_id": session_id,
                "token_budget": CHAT_CONTEXT_TOKEN_BUDGET, "history_limit": 16,
                "history_count": len(snapshot["messages"]), "history": snapshot["messages"],
                "summary": snapshot["summary"],
                "summary_section_start": prompt.find("本 session 已完成的對話摘要：\n") + len("本 session 已完成的對話摘要：\n") if snapshot["summary"] else None,
                "jev_recent_dialogue": context["recent_dialogue"],
                "system_prompt_original_chars": len(prompt),
                "system_prompt_trimmed": chat_messages[0]["content"] != prompt,
                "system_retained_ranges": retained_prompt_ranges(prompt, chat_messages[0]["content"]),
                "memory_section_start": prompt.find(snapshot["memory"]) if snapshot["memory"] else None,
                "profile_section_start": profile_start,
                "profile": snapshot["profile"], "projected_memory": snapshot["memory"],
                "token_estimate": estimate_token_count(chat_messages)})
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

            first_token_latency_ms: float | None = None

            async def send_chunk(piece: str) -> None:
                nonlocal first_token_latency_ms
                nonlocal active_turn_partial
                if active_turn_id == turn_id:
                    if first_token_latency_ms is None and request_started is not None:
                        first_token_latency_ms = round((time.monotonic() - request_started) * 1000, 1)
                    await send({"type": "text_stream", "content": piece, "turn_id": turn_id})
                    if active_turn_id == turn_id:
                        active_turn_partial += piece

            generation_started = time.monotonic()
            reply = await stream_agent_a(chat_messages, send_chunk)
            if not reply:
                reply = "嗯……"
                await send_chunk(reply)
            if active_turn_id != turn_id:
                return
            if short_term_enabled:
                messages.extend([dict(snapshot["user_message"]), {"role": "assistant", "content": reply}])
            active_turn_committed = True
            if short_term_enabled and CHAT_PERSISTENCE_ENABLED and session_id:
                save_session_messages(session_id, messages)
            log_turn(turn_count=sum(item.get("role") == "user" for item in messages),
                     system_prompt=prompt, user_message=text, dialogue_agent_output=reply,
                     tool_names=[], output_tokens=estimate_token_count([{"role": "assistant", "content": reply}]))
            generation_ms = round((time.monotonic() - generation_started) * 1000, 1)
            output_tokens = estimate_token_count([{"role": "assistant", "content": reply}])
            tokens_per_second = round(output_tokens / max(generation_ms / 1000, 0.001), 1)
            await send({
                "type": "stream_end",
                "turn_id": turn_id,
                "metrics": {
                    "first_token_latency_ms": first_token_latency_ms,
                    "generation_ms": generation_ms,
                    "output_tokens": output_tokens,
                    "tokens_per_second": tokens_per_second,
                },
            })
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
        turn_id: str, text: str, model_name: str, legacy: bool, user_message: dict, mode=None,
    ) -> None:
        """將記憶接收／檢索與生成放在同一個可取消的回合 task。"""
        nonlocal active_event_id
        request_started = time.monotonic()
        event_id = None
        trace_turn.set(turn_id)
        trace_event.set(None)
        write_enabled = mode is None or mode.memory_write
        read_enabled = mode is None or mode.memory_read
        trace("test_mode", {"mode": mode.value if mode else "normal",
            "short_term_enabled": short_term_enabled, "memory_read_enabled": read_enabled,
            "memory_write_enabled": write_enabled})
        if write_enabled:
            try:
                event_id = await memory_runtime.accept(
                    session_id or "default_session", turn_id, text, list(messages), user_message,
                )
                active_event_id = event_id
                event_key = str(event_id)
                memory_event_turn_ids[event_key] = turn_id
                for event in recent_memory_events():
                    if str(event.get("event_id")) == event_key:
                        queue_memory_event(event)
                trace_event.set(event_id)
                bind_trace_event(event_id, turn_id)
                trace("memory_accept", {"session_id": session_id})
                await send({"type": "input_accepted", "turn_id": turn_id, "event_id": event_id.hex})
            except MemoryEventReplay:
                await send({
                    "type": "error", "turn_id": turn_id, "code": "turn_id_replayed",
                    "content": "此 turn_id 已處理",
                })
                return
            except MemoryEventConflict:
                await send({
                    "type": "error", "turn_id": turn_id, "code": "turn_id_conflict",
                    "content": "同一 turn_id 不可對應不同內容",
                })
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[Memory] 無法持久化輸入事件: {type(exc).__name__}")
                await send({"type": "memory_enqueue_error", "turn_id": turn_id})
        profile, relevant_memory = ({}, "")
        summary = load_session_summary(session_id) if short_term_enabled and session_id else ""
        if read_enabled:
            profile, relevant_memory = await memory_runtime.retrieve(text, event_id=event_id,
                recent_dialogue=list(messages) if short_term_enabled else [], summary=summary)
        snapshot = {
            "messages": list(messages) if short_term_enabled else [],
            "emotion": emotion_state if short_term_enabled else None,
            "expression": expression_state,
            "action": current_action,
            "profile": profile,
            "memory": relevant_memory,
            "summary": summary,
            "user_message": user_message,
        }
        await run_turn(
            turn_id, text, model_name, snapshot, legacy, event_id,
            request_started=request_started,
        )

    try:
        while True:
            data = json.loads(await websocket.receive_text())
            if not isinstance(data, dict):
                continue
            try:
                mode = resolve_test_mode(data.get("test_mode"))
            except (ValueError, TypeError):
                await send({"type": "error", "content": "無效的隔離測試模式", "turn_id": data.get("turn_id")})
                continue
            next_short_term = mode is None or mode.short_term
            incoming_session = normalize_session_id(data.get("session_id")) or session_id
            if incoming_session != session_id or next_short_term != short_term_enabled:
                await cancel_active()
                short_term_enabled = next_short_term
                session_id = incoming_session
                expression_state = None
                current_action = None
                if short_term_enabled and CHAT_PERSISTENCE_ENABLED and session_id:
                    messages = load_session_messages(session_id)
                    emotion_state = load_session_emotion_state(session_id)
                else:
                    messages = []
                    emotion_state = None
                version = 0

            control_type = data.get("type")
            if control_type in {"reset", "reset_session"}:
                await cancel_active(finalize_memory=False)
                # ``reset`` 保留既有 owner 長期重置語義；REST reset 完成後，
                # 前端改送 ``reset_session``，只同步這條連線的 closure 狀態，
                # 避免同一次使用者操作重複遞增 long-term generation。
                if control_type == "reset":
                    await memory_runtime.reset()
                messages, emotion_state, expression_state = [], None, None
                current_action = None
                version += 1
                if short_term_enabled and CHAT_PERSISTENCE_ENABLED and session_id:
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
                if short_term_enabled and len(messages) > COMPRESS_KEEP_RECENT + 1:
                    messages = await compress_context(messages, websocket, session_id, send)
                    if short_term_enabled and CHAT_PERSISTENCE_ENABLED and session_id:
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
            next_user_message = build_user_message(
                session_id or "default_session", turn_id, text, timestamp=input_event["timestamp"],
            )
            source_id = next_user_message["memory_source"]["source_id"]
            previous = next((item for item in messages
                             if isinstance(item, dict) and item.get("role") == "user"
                             and isinstance(item.get("memory_source"), dict)
                             and item["memory_source"].get("source_id") == source_id), None)
            if active_turn_id == turn_id or previous is not None:
                conflict = ((active_turn_text if active_turn_id == turn_id else previous.get("content")) != text)
                await send({
                    "type": "error", "turn_id": turn_id,
                    "code": "turn_id_conflict" if conflict else "turn_id_replayed",
                    "content": "同一 turn_id 不可對應不同內容" if conflict else "此 turn_id 已處理",
                })
                continue
            # 先切換回合，讓新輸入可以立即打斷生成中的舊回合；記憶檢索不能阻塞取消。
            await cancel_active()
            active_turn_id = turn_id
            active_turn_text = text
            active_turn_message = next_user_message
            active_turn_partial = ""
            active_turn_committed = False
            active_event_id = None
            active_memory_routed = False
            active_task = asyncio.create_task(prepare_and_run_turn(
                turn_id, text, model_name, data.get("legacy_payloads") is True,
                active_turn_message, mode,
            ))
    except WebSocketDisconnect:
        print("Client disconnected")
    finally:
        await cancel_active()
        unsubscribe_memory_events()
        memory_event_task.cancel()
        await asyncio.gather(memory_event_task, return_exceptions=True)
