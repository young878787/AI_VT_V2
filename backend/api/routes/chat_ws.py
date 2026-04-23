"""
Chat WebSocket 端點（/ws/chat）：主對話迴圈。
雙 Agent 架構：Agent A (JPAF Chat) → Agent B (Tools)。
"""
import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.config import (
    AI_PROVIDER,
    MODEL_NAME,
    CHAT_PERSISTENCE_ENABLED,
    COMPRESS_TOKEN_THRESHOLD,
    COMPRESS_KEEP_RECENT,
)
from core.utils import strip_thinking, normalize_session_id
from domain.jpaf import JPAFSession, extract_jpaf_state, strip_jpaf_tags
from domain.agent_a_prompts import build_agent_a_prompt
from domain.agent_b_prompts import build_live2d_prompt, build_memory_prompt
from domain.tools import live2d_tools, memory_tools
from infrastructure.ai_client import chat_create_with_fallback, EXTRA_BODY
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
    stream_agent_a,
    collect_agent_a,
    call_live2d_agent,
    call_memory_agent,
    compress_context,
    estimate_token_count,
    synthesize_and_send_voice,
    parse_xml_tool_calls,
)
from services.tool_arg_parser import parse_tool_call_arguments
from services.memory_service import execute_profile_update
from api.display_manager import broadcast_to_displays
from core.prompt_logger import log_turn, reset_log

router = APIRouter()


@router.websocket("/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    messages: list = []
    current_session_id: str | None = None
    tts_tasks: set[asyncio.Task] = set()

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
            incoming_session_id = normalize_session_id(data.get("session_id"))
            if CHAT_PERSISTENCE_ENABLED and incoming_session_id != current_session_id:
                if current_session_id and messages:
                    save_session_messages(current_session_id, messages)
                current_session_id = incoming_session_id
                messages = (
                    load_session_messages(current_session_id)
                    if current_session_id
                    else []
                )

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

            model_name: str = data.get("model_name", "Hiyori")

            # ================================================================
            # 步驟 1：組裝 Agent A 系統 Prompt（VTuber + JPAF）
            # ================================================================
            user_profile = load_user_profile()
            memory_notes = load_memory_notes()
            agent_a_system = build_agent_a_prompt(
                user_profile, memory_notes, jpaf_session
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
                print(f"[{AI_PROVIDER.upper()}] Agent A: {user_message[:60]}...")

                # ============================================================
                # 步驟 2：Agent A 串流呼叫（JPAF Chat，無 tools）
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

                # 將 Agent A 乾淨回覆加入共用 history
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
                # 步驟 3：Agent B-1 (Live2D) + B-2 (Memory) 並行呼叫
                # ============================================================
                print(f"[{AI_PROVIDER.upper()}] Agent B: parallel live2d + memory...")

                # B-1 Live2D prompt
                live2d_system = build_live2d_prompt(
                    agent_a_text,
                    jpaf_state,
                    emotion_state,
                    model_name,
                )
                live2d_messages = [
                    {"role": "system", "content": live2d_system},
                    {"role": "user", "content": "請根據上述上下文產生主表情，並視需要額外呼叫 blink_control，讓表情更有層次。"},
                ]

                # B-2 Memory prompt
                memory_system = build_memory_prompt(user_message, agent_a_text)
                memory_messages = [
                    {"role": "system", "content": memory_system},
                    {"role": "user", "content": "請分析用戶訊息，判斷是否需要記憶操作。"},
                ]

                # 並行呼叫
                live2d_response, memory_response = await asyncio.gather(
                    call_live2d_agent(live2d_messages, model_name),
                    call_memory_agent(memory_messages),
                )

                # ============================================================
                # 步驟 4：處理 Tool Calls
                # ============================================================
                head_intensity = 0.3
                blush_level = 0.0
                eye_l_open = 1.0
                eye_r_open = 1.0
                duration_sec = 5.0
                mouth_form = 0.0
                brow_l_y = 0.0
                brow_r_y = 0.0
                brow_l_angle = 0.0
                brow_r_angle = 0.0
                brow_l_form = 0.0
                brow_r_form = 0.0
                eye_sync = True
                speaking_rate = 1.0

                all_calls: list[dict] = []

                # --- 解析 Live2D response ---
                if live2d_response.choices and len(live2d_response.choices) > 0:
                    l2d_msg = live2d_response.choices[0].message
                    print(f"[Live2D Agent] content: {(l2d_msg.content or 'None')[:100]}")
                    print(f"[Live2D Agent] tool_calls count: {len(l2d_msg.tool_calls) if l2d_msg.tool_calls else 0}")

                    l2d_content = strip_thinking(l2d_msg.content or "")
                    l2d_xml_calls, _ = parse_xml_tool_calls(l2d_content)

                    if l2d_msg.tool_calls:
                        for tc in l2d_msg.tool_calls:
                            try:
                                args, was_normalized = parse_tool_call_arguments(tc.function.arguments or "")
                                all_calls.append({"name": tc.function.name, "arguments": args})
                                if was_normalized:
                                    print(f"[Live2D Agent][NORMALIZED_TOOL_ARGS] name={tc.function.name}")
                                print(f"[Live2D Agent] tool: {tc.function.name} => {tc.function.arguments[:200]}")
                            except json.JSONDecodeError as e:
                                raw_args = tc.function.arguments or ""
                                around_start = max(0, e.pos - 80)
                                around_end = min(len(raw_args), e.pos + 80)
                                print(
                                    f"[Live2D Agent][MALFORMED_TOOL_ARGS] "
                                    f"name={tc.function.name}, pos={e.pos}, len={len(raw_args)}, error={e}"
                                )
                                print(f"[Live2D Agent][MALFORMED_TOOL_ARGS][RAW] {raw_args}")
                                print(
                                    "[Live2D Agent][MALFORMED_TOOL_ARGS][AROUND] "
                                    f"{raw_args[around_start:around_end]}"
                                )
                    if l2d_xml_calls:
                        print(f"[Live2D Agent] XML tool calls: {len(l2d_xml_calls)}")
                        all_calls.extend(l2d_xml_calls)

                # --- 解析 Memory response ---
                if memory_response.choices and len(memory_response.choices) > 0:
                    mem_msg = memory_response.choices[0].message
                    print(f"[Memory Agent] content: {(mem_msg.content or 'None')[:100]}")
                    print(f"[Memory Agent] tool_calls count: {len(mem_msg.tool_calls) if mem_msg.tool_calls else 0}")

                    mem_content = strip_thinking(mem_msg.content or "")
                    mem_xml_calls, _ = parse_xml_tool_calls(mem_content)

                    if mem_msg.tool_calls:
                        for tc in mem_msg.tool_calls:
                            try:
                                args, was_normalized = parse_tool_call_arguments(tc.function.arguments or "")
                                all_calls.append({"name": tc.function.name, "arguments": args})
                                if was_normalized:
                                    print(f"[Memory Agent][NORMALIZED_TOOL_ARGS] name={tc.function.name}")
                                print(f"[Memory Agent] tool: {tc.function.name} => {tc.function.arguments[:100]}")
                            except json.JSONDecodeError as e:
                                print(f"Memory tool call 解析失敗: {e}")
                    if mem_xml_calls:
                        print(f"[Memory Agent] XML tool calls: {len(mem_xml_calls)}")
                        all_calls.extend(mem_xml_calls)
                else:
                    print("[Memory Agent] No choices returned!")

                print(f"[Agent B] all_calls 總數: {len(all_calls)}, names: {[c['name'] for c in all_calls]}")
                if not any(call["name"] == "set_ai_behavior" for call in all_calls):
                    print("[Agent B][WARN] 缺少 set_ai_behavior；本輪將退回預設 behavior payload，可能導致表情變化很小。")

                # --- 行為變數預設值（當 set_ai_behavior 未被呼叫時保持安全） ---
                head_intensity = 0.3
                blush_level = 0.0
                eye_sync = True
                eye_l_open = 1.0
                eye_r_open = 1.0
                duration_sec = 5.0
                mouth_form = 0.0
                brow_l_y = 0.0
                brow_r_y = 0.0
                brow_l_angle = 0.0
                brow_r_angle = 0.0
                brow_l_form = 0.0
                brow_r_form = 0.0
                speaking_rate = 1.0
                eye_l_smile = 0.0
                eye_r_smile = 0.0
                brow_l_x = 0.0
                brow_r_x = 0.0

                # --- 執行 tool calls ---
                for call in all_calls:
                    fn_name = call["name"]
                    args = call["arguments"]

                    if fn_name == "set_ai_behavior":
                        head_intensity = float(args.get("head_intensity", 0.3))
                        blush_level = float(args.get("blush_level", 0.0))
                        eye_sync = args.get("eye_sync", True)
                        eye_l_open = float(args.get("eye_l_open", 1.0))
                        eye_r_open = float(args.get("eye_r_open", 1.0))
                        duration_sec = float(args.get("duration_sec", 5.0))
                        mouth_form = float(args.get("mouth_form", 0.0))
                        brow_l_y = float(args.get("brow_l_y", 0.0))
                        brow_r_y = float(args.get("brow_r_y", 0.0))
                        brow_l_angle = float(args.get("brow_l_angle", 0.0))
                        brow_r_angle = float(args.get("brow_r_angle", 0.0))
                        brow_l_form = float(args.get("brow_l_form", 0.0))
                        brow_r_form = float(args.get("brow_r_form", 0.0))
                        speaking_rate = float(args.get("speaking_rate", 1.0))
                        eye_l_smile = float(args.get("eye_l_smile", 0.0))
                        eye_r_smile = float(args.get("eye_r_smile", 0.0))
                        brow_l_x = float(args.get("brow_l_x", 0.0))
                        brow_r_x = float(args.get("brow_r_x", 0.0))

                    elif fn_name == "blink_control":
                        blink_action = args.get("action", "")
                        blink_duration = float(args.get("duration_sec", 0))
                        blink_interval_min = args.get("interval_min")
                        blink_interval_max = args.get("interval_max")

                        blink_payload = {
                            "type": "blink_control",
                            "action": blink_action,
                        }
                        if blink_duration > 0:
                            blink_payload["durationSec"] = blink_duration
                        if blink_interval_min is not None:
                            blink_payload["intervalMin"] = float(blink_interval_min)
                        if blink_interval_max is not None:
                            blink_payload["intervalMax"] = float(blink_interval_max)

                        await websocket.send_json(blink_payload)
                        await broadcast_to_displays(blink_payload)
                        print(f"[BlinkControl] action={blink_action}, duration={blink_duration}")

                    elif fn_name == "update_user_profile":
                        action = args.get("action", "add")
                        field = args.get("field", "custom_notes")
                        value = args.get("value", "")
                        execute_profile_update(action, field, value)
                        print(f"User profile 已更新 [{action}] {field}: {value}")

                    elif fn_name == "save_memory_note":
                        note_content = args.get("content", "")
                        if note_content:
                            append_memory_note(note_content)
                            print(f"Memory note 已記錄: {note_content}")

                # ---- Prompt Log ----
                _a_tokens = estimate_token_count(
                    [{"role": "assistant", "content": agent_a_text}]
                )
                _b_tokens = 0
                for resp in (live2d_response, memory_response):
                    try:
                        if hasattr(resp, "usage") and resp.usage:
                            _b_tokens += getattr(resp.usage, "completion_tokens", 0) or 0
                    except Exception:
                        pass
                log_turn(
                    turn_count=jpaf_session.turn_count,
                    system_prompt=agent_a_system,
                    user_message=user_message,
                    agent_a_output=agent_a_text,
                    tool_names=[c["name"] for c in all_calls],
                    output_tokens=_a_tokens + _b_tokens,
                )

                # ---- 送出 behavior payload (FIRST) ----
                behavior_payload = _build_behavior_payload(
                    head_intensity, blush_level, eye_sync,
                    eye_l_open, eye_r_open, duration_sec,
                    mouth_form, brow_l_y, brow_r_y,
                    brow_l_angle, brow_r_angle, brow_l_form, brow_r_form,
                    eye_l_smile, eye_r_smile, brow_l_x, brow_r_x,
                )
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
                await websocket.send_json(
                    {"type": "error", "content": f"API 錯誤: {str(e)}"}
                )

    except WebSocketDisconnect:
        for task in list(tts_tasks):
            task.cancel()
        print("Client disconnected")
    except Exception as e:
        for task in list(tts_tasks):
            task.cancel()
        print(f"WebSocket error: {e}")


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
