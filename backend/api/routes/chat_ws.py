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
from domain.agent_b_prompts import build_agent_b_prompt
from domain.tools import tools
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
    call_agent_b,
    compress_context,
    estimate_token_count,
    synthesize_and_send_voice,
    parse_xml_tool_calls,
)
from services.memory_service import execute_profile_update
from api.display_manager import broadcast_to_displays

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

            user_message = data.get("content", "")
            if not user_message:
                continue

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
                agent_a_text, jpaf_state = await collect_agent_a(
                    messages
                )

                if not agent_a_text:
                    agent_a_text = "（默默地點頭）"
                    await websocket.send_json(
                        {"type": "text_stream", "content": agent_a_text}
                    )

                # 更新 JPAF session
                if jpaf_state:
                    if jpaf_state.get("reflection_triggered"):
                        jpaf_session.apply_reflection(jpaf_state)
                    jpaf_session.update_persona(jpaf_state)

                jpaf_session.increment_turn()
                save_jpaf_state(jpaf_session.to_dict())

                # 將 Agent A 乾淨回覆加入共用 history
                messages.append({"role": "assistant", "content": agent_a_text})

                # ============================================================
                # 步驟 3：Agent B 工具決策呼叫
                # ============================================================
                print(f"[{AI_PROVIDER.upper()}] Agent B: deciding tools...")

                agent_b_system = build_agent_b_prompt(
                    agent_a_text, jpaf_state, user_message
                )
                agent_b_messages = [
                    {"role": "system", "content": agent_b_system},
                    {"role": "user", "content": f"請根據上述上下文決定工具呼叫。"},
                ]

                response = await call_agent_b(agent_b_messages)

                # ============================================================
                # 步驟 4：處理 Agent B 的 Tool Calls
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

                if response.choices and len(response.choices) > 0:
                    response_message = response.choices[0].message

                    # 解析 XML tool calls（針對不支援原生 FC 的模型）
                    content_text = strip_thinking(response_message.content or "")
                    xml_tool_calls, _ = parse_xml_tool_calls(content_text)

                    # 合併原生 + XML tool calls
                    all_calls: list[dict] = []
                    if response_message.tool_calls:
                        for tc in response_message.tool_calls:
                            try:
                                args = json.loads(tc.function.arguments)
                                all_calls.append(
                                    {"name": tc.function.name, "arguments": args}
                                )
                            except json.JSONDecodeError as e:
                                print(f"Tool call 參數解析失敗 ({tc.function.name}): {e}")

                    if xml_tool_calls:
                        print(f"偵測到 XML Tool Calls: {len(xml_tool_calls)} 個")
                        all_calls.extend(xml_tool_calls)

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

                # ---- 送出 behavior payload (FIRST) ----
                behavior_payload = _build_behavior_payload(
                    head_intensity, blush_level, eye_sync,
                    eye_l_open, eye_r_open, duration_sec,
                    mouth_form, brow_l_y, brow_r_y,
                    brow_l_angle, brow_r_angle, brow_l_form, brow_r_form,
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
    }
