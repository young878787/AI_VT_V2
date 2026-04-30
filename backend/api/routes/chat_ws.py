"""
Chat WebSocket 端點（/ws/chat）：主對話迴圈。
Chat Orchestrator 負責協調 Dialogue Agent、Expression Agent、Memory Agent。
"""
import asyncio
import inspect
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.config import (
    AI_PROVIDER,
    MODEL_NAME,
    CHAT_PERSISTENCE_ENABLED,
    COMPRESS_TOKEN_THRESHOLD,
    COMPRESS_KEEP_RECENT,
)
from core.utils import normalize_session_id
from domain.jpaf import JPAFSession
from domain.agent_a_prompts import build_agent_a_prompt
from domain.agent_b_prompts import build_live2d_prompt, build_memory_prompt
from infrastructure.memory_store import (
    load_user_profile,
    load_memory_notes,
    load_session_messages,
    save_session_messages,
    load_jpaf_state,
    save_jpaf_state,
    append_memory_note,
)
from services.chat_service import (
    collect_agent_a,
    call_expression_agent,
    call_memory_agent,
    compress_context,
    estimate_token_count,
    synthesize_and_send_voice,
)
from services.agent_tool_pipeline import (
    EXPRESSION_AGENT_ALLOWED_TOOL_NAMES,
    MEMORY_AGENT_ALLOWED_TOOL_NAMES,
    extract_agent_tool_calls,
    filter_tool_calls_for_pool,
    get_meaningful_memory_tool_arguments,
    summarize_tool_names,
)
from services.expression_compiler import compile_expression_plan
from services.expression_intent_parser import parse_expression_intent
from services.expression_legacy_renderer import render_legacy_behavior_payload
from services.memory_service import execute_profile_update
from api.display_manager import broadcast_to_displays
from core.prompt_logger import log_turn, reset_log
from domain.tools.schema_loader import normalize_model_name

router = APIRouter()


def _sanitize_behavior_number(args: dict, key: str) -> float | None:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _sanitize_optional_behavior_number(args: dict, key: str) -> float | None:
    if key not in args:
        return None
    return _sanitize_behavior_number(args, key)


def _sanitize_blink_control_arguments(args: dict) -> dict:
    sanitized = {"action": args.get("action", "")}

    for key in ("duration_sec", "interval_min", "interval_max"):
        sanitized_value = _sanitize_optional_behavior_number(args, key)
        if sanitized_value is not None:
            sanitized[key] = sanitized_value

    return sanitized


def _has_complete_blink_interval_args(args: dict) -> bool:
    if args.get("action") != "set_interval":
        return True
    if "interval_min" not in args or "interval_max" not in args:
        return False
    return args["interval_min"] <= args["interval_max"]


def _sanitize_behavior_boolean(args: dict, key: str) -> bool | None:
    value = args.get(key)
    if not isinstance(value, bool):
        return None
    return value


def _sanitize_set_ai_behavior_arguments(args: dict) -> dict:
    sanitized: dict = {}

    for key in (
        "head_intensity",
        "blush_level",
        "eye_l_open",
        "eye_r_open",
        "duration_sec",
        "mouth_form",
        "brow_l_y",
        "brow_r_y",
        "brow_l_angle",
        "brow_r_angle",
        "brow_l_form",
        "brow_r_form",
        "speaking_rate",
        "eye_l_smile",
        "eye_r_smile",
        "brow_l_x",
        "brow_r_x",
    ):
        sanitized_value = _sanitize_behavior_number(args, key)
        if sanitized_value is not None:
            sanitized[key] = sanitized_value

    eye_sync = _sanitize_behavior_boolean(args, "eye_sync")
    if eye_sync is not None:
        sanitized["eye_sync"] = eye_sync

    return sanitized


async def _maybe_await(result):
    if inspect.isawaitable(result):
        await result


async def _execute_memory_tool_calls(
    memory_calls: list[dict],
    websocket: WebSocket,
    broadcast_func,
    execute_profile_update_fn,
    append_memory_note_fn,
    model_name: str = "Hiyori",
) -> dict:
    memory_calls = filter_tool_calls_for_pool(
        memory_calls,
        allowed_tool_names=MEMORY_AGENT_ALLOWED_TOOL_NAMES,
        label="Memory Agent",
    )

    print(
        "[Chat Orchestrator] "
        f"memory_calls={len(memory_calls)}, names={summarize_tool_names(memory_calls)}"
    )

    filtered_memory_calls: list[dict] = []
    for call in memory_calls:
        fn_name = call["name"]
        args = call["arguments"]

        if fn_name == "update_user_profile":
            meaningful_args = get_meaningful_memory_tool_arguments(fn_name, args, model_name=model_name)
            if meaningful_args is None:
                continue
            action = meaningful_args["action"]
            field = meaningful_args["field"]
            value = meaningful_args["value"]
            execute_profile_update_fn(action, field, value, model_name=model_name)
            call["arguments"] = meaningful_args
            filtered_memory_calls.append(call)
            print(f"User profile 已更新 [{action}] {field}: {value}")

        elif fn_name == "save_memory_note":
            meaningful_args = get_meaningful_memory_tool_arguments(fn_name, args, model_name=model_name)
            if meaningful_args is None:
                continue
            note_content = meaningful_args["content"]
            append_memory_note_fn(note_content)
            call["arguments"] = meaningful_args
            filtered_memory_calls.append(call)
            print(f"Memory note 已記錄: {note_content}")

    memory_calls = filtered_memory_calls

    return {
        "memory_calls": memory_calls,
    }


@router.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    messages: list = []
    current_session_id: str | None = None
    tts_tasks: set[asyncio.Task] = set()
    last_behavior_payload: dict | None = None
    last_expression_carry_state: dict | None = None

    # 載入或初始化 JPAF session
    jpaf_data = load_jpaf_state()
    jpaf_session = (
        JPAFSession.from_dict(jpaf_data) if jpaf_data else JPAFSession()
    )

    # Send initial JPAF state to frontend
    await websocket.send_json({
        "type": "jpaf_update",
        "persona": jpaf_session.current_persona,
        "dominant": jpaf_session.dominant,
        "auxiliary": jpaf_session.auxiliary,
        "baseWeights": jpaf_session.base_weights,
        "turnCount": jpaf_session.turn_count,
    })

    try:
        while True:
            data_str = await websocket.receive_text()
            data = json.loads(data_str)

            # ---- Session 切換 ----
            incoming_session_id = _effective_incoming_session_id(
                data,
                current_session_id=current_session_id,
            )
            session_changed = incoming_session_id != current_session_id
            last_behavior_payload = _reset_behavior_payload_for_session(
                current_session_id=current_session_id,
                incoming_session_id=incoming_session_id,
                last_behavior_payload=last_behavior_payload,
            )
            last_expression_carry_state = _reset_expression_state_for_session(
                current_session_id=current_session_id,
                incoming_session_id=incoming_session_id,
                last_expression_carry_state=last_expression_carry_state,
            )
            messages = _reset_messages_for_session(
                current_session_id=current_session_id,
                incoming_session_id=incoming_session_id,
                messages=messages,
                persistence_enabled=CHAT_PERSISTENCE_ENABLED,
            )
            if CHAT_PERSISTENCE_ENABLED and session_changed:
                if current_session_id and messages:
                    save_session_messages(current_session_id, messages)
                current_session_id = incoming_session_id
                messages = (
                    load_session_messages(current_session_id)
                    if current_session_id
                    else []
                )
            elif session_changed:
                current_session_id = incoming_session_id

            # ---- 手動壓縮指令 ----
            if data.get("type") == "compress":
                if len(messages) > COMPRESS_KEEP_RECENT + 1:
                    messages = await compress_context(messages, websocket)
                    if CHAT_PERSISTENCE_ENABLED and current_session_id:
                        save_session_messages(current_session_id, messages)
                else:
                    await websocket.send_json({"type": "compress_done"})
                continue

            # ---- 記憶還原指令 ----
            # 由前端 handleReset 在 REST /api/reset-memory 成功後發送，
            # 負責清空後端 in-memory 短期記憶，並重新載入已重置的 JPAF session。
            if data.get("type") == "reset":
                # 1. 清空短期記憶（in-memory 對話歷史）
                messages = []
                last_behavior_payload = None
                # 2. 清空 session 持久化檔案（避免下次連線重新載入舊歷史）
                if CHAT_PERSISTENCE_ENABLED and current_session_id:
                    save_session_messages(current_session_id, [])
                # 3. 重新從磁碟載入已被 REST API 重置的 JPAF session
                jpaf_data = load_jpaf_state()
                jpaf_session = (
                    JPAFSession.from_dict(jpaf_data) if jpaf_data else JPAFSession()
                )
                # 3b. 清空 Prompt Log
                reset_log()
                # 4. 通知前端最新 JPAF 狀態（turn_count = 0）
                await websocket.send_json({
                    "type": "jpaf_update",
                    "persona": jpaf_session.current_persona,
                    "dominant": jpaf_session.dominant,
                    "auxiliary": jpaf_session.auxiliary,
                    "baseWeights": jpaf_session.base_weights,
                    "turnCount": jpaf_session.turn_count,
                })
                await websocket.send_json({"type": "reset_done"})
                continue

            user_message = data.get("content", "")
            if not user_message:
                continue

            model_name = normalize_model_name(data.get("model_name", "Hiyori"))

            # ================================================================
            # 步驟 1：組裝 Dialogue Agent 系統 Prompt（VTuber + JPAF）
            # ================================================================
            user_profile = load_user_profile()
            memory_notes = load_memory_notes()
            agent_a_system = build_agent_a_prompt(
                user_profile,
                memory_notes,
                jpaf_session,
                model_name=model_name,
            )

            # 更新或插入 system prompt
            if (
                messages
                and isinstance(messages[0], dict)
                and messages[0].get("role") == "system"
            ):
                messages[0] = {"role": "system", "content": agent_a_system}
            else:
                messages.insert(0, {"role": "system", "content": agent_a_system})

            messages.append({"role": "user", "content": user_message})

            try:
                print(f"[{AI_PROVIDER.upper()}] Dialogue Agent: {user_message[:60]}...")

                # ============================================================
                # 步驟 2：Dialogue Agent 串流呼叫（JPAF Chat，無 tools）
                # ============================================================
                agent_a_text, jpaf_state, emotion_state = await collect_agent_a(
                    messages
                )

                if not agent_a_text:
                    agent_a_text = "（默默地點頭）"

                # 更新 JPAF session
                if jpaf_state:
                    # (1) LLM 主動觸發的 Reflection（備用路徑）
                    if jpaf_state.get("reflection_triggered"):
                        jpaf_session.apply_reflection(jpaf_state)
                    # (2) 程式化 TemporaryWeight 追蹤 + 自動 Reflection
                    active_fn = jpaf_state.get("active_function") or jpaf_session.dominant
                    evolution = jpaf_session.apply_active_function(active_fn)
                    if evolution["reflection_triggered"]:
                        print(
                            f"[JPAF] 程式化 Reflection 觸發："
                            f"active={active_fn}, "
                            f"temp_w={evolution['temporary_weight']:.2f}"
                        )
                    # (3) Persona 更新（每輪都執行）
                    jpaf_session.update_persona(jpaf_state)

                jpaf_session.increment_turn()
                save_jpaf_state(jpaf_session.to_dict())

                # 將 Dialogue Agent 乾淨回覆加入共用 history
                messages.append({"role": "assistant", "content": agent_a_text})

                # 注入 JPAF weights snapshot 到短期記憶（模型可看到 weights 演化軌跡）
                messages.append({
                    "role": "system",
                    "content": (
                        f"[JPAF Turn {jpaf_session.turn_count}] "
                        f"dom={jpaf_session.dominant}, aux={jpaf_session.auxiliary}, "
                        f"persona={jpaf_session.current_persona} | "
                        f"weights: {jpaf_session.weights_inline()}"
                    ),
                })

                # ============================================================
                # 步驟 3：Expression Agent + Memory Agent 並行呼叫
                # ============================================================
                print(
                    f"[{AI_PROVIDER.upper()}] Chat Orchestrator: parallel Expression Agent + Memory Agent..."
                )

                previous_expression_source = last_expression_carry_state or last_behavior_payload
                previous_expression_state = _summarize_previous_expression_state(
                    previous_expression_source
                )

                # Expression Agent prompt
                live2d_system = build_live2d_prompt(
                    user_message,
                    agent_a_text,
                    previous_expression_state,
                    emotion_state,
                    model_name,
                )
                live2d_messages = [
                    {"role": "system", "content": live2d_system},
                    {"role": "user", "content": "請根據上述上下文輸出單一 JSON expression intent，僅回傳 JSON object，不要輸出說明文字或任何 tool calls。"},
                ]

                # Memory Agent prompt
                memory_system = build_memory_prompt(user_message, agent_a_text, model_name)
                memory_messages = [
                    {"role": "system", "content": memory_system},
                    {"role": "user", "content": "請分析用戶訊息，判斷是否需要記憶操作。"},
                ]

                # 並行呼叫
                live2d_response, memory_response = await asyncio.gather(
                    call_expression_agent(live2d_messages, model_name),
                    call_memory_agent(memory_messages, model_name),
                )

                # ============================================================
                # 步驟 4：處理 Chat Orchestrator tool calls
                # ============================================================
                memory_calls: list[dict] = []
                if live2d_response.choices and len(live2d_response.choices) > 0:
                    expression_msg = live2d_response.choices[0].message
                    expression_raw = expression_msg.content or ""
                    expression_tool_calls = getattr(expression_msg, "tool_calls", []) or []
                    print(f"[Expression Agent] content: {expression_raw[:200]}")
                    if not expression_raw.strip():
                        if expression_tool_calls:
                            raise ValueError(
                                "Expression Agent returned legacy tool-call output without JSON intent content"
                            )
                        raise ValueError(
                            "Expression Agent returned empty content without JSON intent"
                        )
                    expression_intent = parse_expression_intent(
                        expression_raw,
                        emotion_state=emotion_state,
                        previous_state=previous_expression_state,
                        user_message=user_message,
                    )
                    expression_intent = {**expression_intent, "spoken_text": agent_a_text}
                    expression_plan = compile_expression_plan(
                        expression_intent,
                        model_name=model_name,
                        previous_state=previous_expression_source,
                    )
                else:
                    raise ValueError("Expression Agent returned no choices")

                legacy_render = render_legacy_behavior_payload(expression_plan)
                behavior_payload = legacy_render["behavior_payload"]
                speaking_rate = legacy_render["speaking_rate"]
                expression_plan_log = _summarize_expression_plan_for_log(expression_plan)
                print(f"[Expression Plan] {expression_plan_log}")

                # --- 解析 Memory Agent response ---
                if memory_response.choices and len(memory_response.choices) > 0:
                    mem_msg = memory_response.choices[0].message
                    mem_tool_calls = getattr(mem_msg, "tool_calls", []) or []
                    print(f"[Memory Agent] content: {(mem_msg.content or 'None')[:100]}")
                    print(f"[Memory Agent] tool_calls count: {len(mem_tool_calls)}")
                    memory_calls = extract_agent_tool_calls(
                        memory_response,
                        model_name=model_name,
                        label="Memory Agent",
                    )
                    for tc in mem_tool_calls:
                        print(f"[Memory Agent] tool: {tc.function.name} => {(tc.function.arguments or '')[:100]}")
                else:
                    print("[Memory Agent] No choices returned!")

                execution_result = await _execute_memory_tool_calls(
                    memory_calls=memory_calls,
                    websocket=websocket,
                    broadcast_func=broadcast_to_displays,
                    execute_profile_update_fn=execute_profile_update,
                    append_memory_note_fn=append_memory_note,
                    model_name=model_name,
                )
                memory_calls = execution_result["memory_calls"]
                last_behavior_payload = {**behavior_payload, "speakingRate": speaking_rate}
                last_expression_carry_state = expression_plan.get("carryState")

                # ---- Prompt Log ----
                _a_tokens = estimate_token_count(
                    [{"role": "assistant", "content": agent_a_text}]
                )
                _b_tokens = 0
                for resp in (live2d_response, memory_response):
                    usage = getattr(resp, "usage", None)
                    if usage is None:
                        continue

                    completion_tokens = getattr(usage, "completion_tokens", 0)
                    if isinstance(completion_tokens, bool):
                        continue
                    if isinstance(completion_tokens, int):
                        _b_tokens += completion_tokens
                log_turn(
                    turn_count=jpaf_session.turn_count,
                    system_prompt=agent_a_system,
                    user_message=user_message,
                    dialogue_agent_output=agent_a_text,
                    tool_names=summarize_tool_names(memory_calls) + [expression_plan_log],
                    output_tokens=_a_tokens + _b_tokens,
                )

                # ---- 先送出 expression plan，再保留 legacy payload fallback ----
                await websocket.send_json(expression_plan)
                await broadcast_to_displays(expression_plan)

                for blink_payload in legacy_render["blink_payloads"]:
                    await websocket.send_json(blink_payload)
                    await broadcast_to_displays(blink_payload)

                # ---- 送出 behavior payload ----
                await websocket.send_json(behavior_payload)
                await broadcast_to_displays(behavior_payload)

                # ---- 送出 JPAF 狀態更新 ----
                await websocket.send_json({
                    "type": "jpaf_update",
                    "persona": jpaf_session.current_persona,
                    "dominant": jpaf_session.dominant,
                    "auxiliary": jpaf_session.auxiliary,
                    "baseWeights": jpaf_session.base_weights,
                    "turnCount": jpaf_session.turn_count,
                })

                # ---- 送出 buffered 文字 ----
                await websocket.send_json({"type": "text_stream", "content": agent_a_text})

                # ============================================================
                # 步驟 5：後處理
                # ============================================================
                token_count = estimate_token_count(messages)
                print(f"目前 token 估算: ~{token_count:,}")

                if token_count >= COMPRESS_TOKEN_THRESHOLD:
                    print(f"Token 數 ({token_count:,}) 接近上限，自動觸發壓縮...")
                    messages = await compress_context(messages, websocket)
                    if CHAT_PERSISTENCE_ENABLED and current_session_id:
                        save_session_messages(current_session_id, messages)

                if CHAT_PERSISTENCE_ENABLED and current_session_id:
                    save_session_messages(current_session_id, messages)

                await websocket.send_json({"type": "stream_end"})

                # 非阻塞 TTS
                if agent_a_text:
                    task = asyncio.create_task(
                        synthesize_and_send_voice(
                            websocket, agent_a_text, speaking_rate
                        )
                    )
                    tts_tasks.add(task)
                    task.add_done_callback(lambda t: tts_tasks.discard(t))

            except Exception as e:
                print(
                    f"[AI API error][{AI_PROVIDER.upper()}] Model={MODEL_NAME} | {e}"
                )
                try:
                    await websocket.send_json(
                        {"type": "error", "content": f"API 錯誤: {str(e)}"}
                    )
                except WebSocketDisconnect:
                    pass
                raise

    except WebSocketDisconnect:
        for task in list(tts_tasks):
            task.cancel()
        print("Client disconnected")
    except Exception as e:
        for task in list(tts_tasks):
            task.cancel()
        print(f"WebSocket error: {e}")
        raise


def _build_behavior_payload(
    head_intensity: float,
    blush_level: float,
    eye_sync: bool,
    eye_l_open: float,
    eye_r_open: float,
    duration_sec: float,
    mouth_form: float,
    brow_l_y: float,
    brow_r_y: float,
    brow_l_angle: float,
    brow_r_angle: float,
    brow_l_form: float,
    brow_r_form: float,
    eye_l_smile: float = 0.0,
    eye_r_smile: float = 0.0,
    brow_l_x: float = 0.0,
    brow_r_x: float = 0.0,
    ) -> dict:
    """組裝行為數據 payload（發送給前端 & Display 端點）。"""
    return {
        "type": "behavior",
        "headIntensity": head_intensity,
        "blushLevel": blush_level,
        "eyeSync": eye_sync,
        "eyeLOpen": eye_l_open,
        "eyeROpen": eye_r_open,
        "durationSec": duration_sec,
        "mouthForm": mouth_form,
        "browLY": brow_l_y,
        "browRY": brow_r_y,
        "browLAngle": brow_l_angle,
        "browRAngle": brow_r_angle,
        "browLForm": brow_l_form,
        "browRForm": brow_r_form,
        "eyeLSmile": eye_l_smile,
        "eyeRSmile": eye_r_smile,
        "browLX": brow_l_x,
        "browRX": brow_r_x,
    }


def _reset_behavior_payload_for_session(
    current_session_id: str | None,
    incoming_session_id: str | None,
    last_behavior_payload: dict | None,
) -> dict | None:
    if incoming_session_id != current_session_id:
        return None
    return last_behavior_payload


def _effective_incoming_session_id(
    data: dict,
    current_session_id: str | None,
) -> str | None:
    incoming_session_id = normalize_session_id(data.get("session_id"))
    if incoming_session_id is not None:
        return incoming_session_id
    return current_session_id


def _reset_messages_for_session(
    current_session_id: str | None,
    incoming_session_id: str | None,
    messages: list,
    persistence_enabled: bool,
) -> list:
    if not persistence_enabled and incoming_session_id != current_session_id:
        return []
    return messages


def _reset_expression_state_for_session(
    current_session_id: str | None,
    incoming_session_id: str | None,
    last_expression_carry_state: dict | None,
) -> dict | None:
    if incoming_session_id != current_session_id:
        return None
    return last_expression_carry_state


def _summarize_expression_plan_for_log(expression_plan: dict) -> str:
    idle_plan = expression_plan.get("idlePlan") if isinstance(expression_plan, dict) else None
    if not isinstance(idle_plan, dict):
        return "expression_plan"

    name = idle_plan.get("name", "unknown_idle")
    enter_after_ms = idle_plan.get("enterAfterMs", "?")
    source = idle_plan.get("source") if isinstance(idle_plan.get("source"), dict) else {}
    action_ms = source.get("actionEnterAfterMs", "?")
    speaking_ms = source.get("speakingEnterAfterMs", "?")
    loop_events = idle_plan.get("loopEvents") if isinstance(idle_plan.get("loopEvents"), list) else []
    loop_names = [
        str(event.get("kind"))
        for event in loop_events
        if isinstance(event, dict) and event.get("kind")
    ]
    loop_summary = ",".join(loop_names) if loop_names else "none"
    ambient_enter_after_ms = idle_plan.get("ambientEnterAfterMs", "?")
    ambient_switch_interval_ms = idle_plan.get("ambientSwitchIntervalMs", "?")
    ambient_plan = idle_plan.get("ambientPlan") if isinstance(idle_plan.get("ambientPlan"), dict) else {}
    ambient_states = ambient_plan.get("states") if isinstance(ambient_plan.get("states"), list) else []
    ambient_names = [
        str(state.get("kind"))
        for state in ambient_states
        if isinstance(state, dict) and state.get("kind")
    ]
    ambient_summary = ",".join(ambient_names) if ambient_names else "none"
    return (
        f"expression_plan idlePlan {name} "
        f"enterAfterMs {enter_after_ms} "
        f"actionMs {action_ms} speakingMs {speaking_ms} "
        f"loopEvents {loop_summary} "
        f"ambientEnterMs {ambient_enter_after_ms} "
        f"ambientSwitchMs {ambient_switch_interval_ms} "
        f"ambientStates {ambient_summary}"
    )


def _summarize_previous_expression_state(behavior_payload: dict | None) -> dict | None:
    if not behavior_payload:
        return None

    mouth_form = float(behavior_payload.get("mouthForm", 0.0))
    eye_l_open = float(behavior_payload.get("eyeLOpen", 1.0))
    eye_r_open = float(behavior_payload.get("eyeROpen", 1.0))
    eye_l_smile = float(behavior_payload.get("eyeLSmile", 0.0))
    eye_r_smile = float(behavior_payload.get("eyeRSmile", 0.0))
    eye_sync = bool(behavior_payload.get("eyeSync", True))
    brow_l_y = float(behavior_payload.get("browLY", 0.0))
    brow_r_y = float(behavior_payload.get("browRY", 0.0))
    brow_l_angle = float(behavior_payload.get("browLAngle", 0.0))
    brow_r_angle = float(behavior_payload.get("browRAngle", 0.0))
    brow_l_form = float(behavior_payload.get("browLForm", 0.0))
    brow_r_form = float(behavior_payload.get("browRForm", 0.0))
    brow_l_x = float(behavior_payload.get("browLX", 0.0))
    brow_r_x = float(behavior_payload.get("browRX", 0.0))

    summary_parts: list[str] = []
    if mouth_form > 0.18:
        summary_parts.append("嘴角偏上揚")
    elif mouth_form < -0.18:
        summary_parts.append("嘴角明顯下壓")
    else:
        summary_parts.append("嘴角接近中性")

    if eye_l_smile > 0.35 or eye_r_smile > 0.35:
        summary_parts.append("眼睛帶笑")
    elif eye_l_open > 1.05 or eye_r_open > 1.05:
        summary_parts.append("眼睛偏張大")
    elif eye_l_open < 0.85 or eye_r_open < 0.85:
        summary_parts.append("眼睛偏瞇")
    else:
        summary_parts.append("雙眼自然")

    if (
        abs(brow_l_angle) > 0.25
        or abs(brow_r_angle) > 0.25
        or abs(brow_l_y) > 0.2
        or abs(brow_r_y) > 0.2
        or abs(brow_l_form) > 0.2
        or abs(brow_r_form) > 0.2
        or abs(brow_l_x) > 0.12
        or abs(brow_r_x) > 0.12
    ):
        summary_parts.append("眉毛有明顯表情")
    else:
        summary_parts.append("眉毛變化不大")

    if not eye_sync:
        summary_parts.append("左右表情不對稱")

    return {
        "summary": "、".join(summary_parts),
        "mouth_form": mouth_form,
        "eye_sync": eye_sync,
        "eye_l_open": eye_l_open,
        "eye_r_open": eye_r_open,
        "eye_l_smile": eye_l_smile,
        "eye_r_smile": eye_r_smile,
        "brow_l_y": brow_l_y,
        "brow_r_y": brow_r_y,
        "brow_l_angle": brow_l_angle,
        "brow_r_angle": brow_r_angle,
        "brow_l_form": brow_l_form,
        "brow_r_form": brow_r_form,
        "brow_l_x": brow_l_x,
        "brow_r_x": brow_r_x,
    }
