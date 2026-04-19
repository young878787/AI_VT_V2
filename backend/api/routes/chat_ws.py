"""
Chat WebSocket 端點（/ws/chat）：主對話迴圈。
負責接收前端訊息、協調各服務、回傳串流文字與行為數據。
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
from domain.tools import tools
from domain.prompts import build_system_prompt
from infrastructure.ai_client import chat_create_with_fallback, EXTRA_BODY
from infrastructure.memory_store import (
    load_user_profile,
    load_memory_notes,
    load_session_messages,
    save_session_messages,
    append_memory_note,
)
from services.chat_service import (
    stream_final_text,
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

    # 初始化 messages（system prompt 將在每輪動態組裝）
    messages: list = []
    current_session_id: str | None = None
    tts_tasks: set[asyncio.Task] = set()

    try:
        while True:
            # 接收前端訊息
            data_str = await websocket.receive_text()
            data = json.loads(data_str)

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

            # 處理手動壓縮指令
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

            # ---- 步驟 1：動態組裝 System Prompt ----
            user_profile = load_user_profile()
            memory_notes = load_memory_notes()
            system_prompt = build_system_prompt(user_profile, memory_notes)

            # 更新或插入 system prompt（始終放在 messages[0]）
            if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
                messages[0] = {"role": "system", "content": system_prompt}
            else:
                messages.insert(0, {"role": "system", "content": system_prompt})

            # 加入使用者訊息
            messages.append({"role": "user", "content": user_message})

            # ---- 步驟 2-5：呼叫 LLM 並處理 Tool Calls ----
            try:
                print(f"[{AI_PROVIDER.upper()}] Sending: {user_message[:60]}...")

                response = await chat_create_with_fallback(
                    model=MODEL_NAME,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0.85,
                    extra_body=EXTRA_BODY,
                    max_tokens=256,
                )

                if not response.choices or len(response.choices) == 0:
                    raise Exception("API 回傳結果為空 (choices 陣列長度為 0)")

                response_message = response.choices[0].message

                # 攔截並解析 XML 格式的 tool_call（針對不支援原生 function calling 的模型）
                # strip_thinking 先移除 <think>...</think>，再進行 tool_call 解析
                content_text = strip_thinking(response_message.content or "")
                xml_tool_calls, content_text = parse_xml_tool_calls(content_text)
                if xml_tool_calls:
                    response_message.content = content_text

                # 預設動作參數
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
                has_tool_call = False
                streamed_output = False

                # ---- 步驟 3：處理所有 Tool Calls ----
                if response_message.tool_calls or xml_tool_calls:
                    has_tool_call = True

                    # 合併原生 tool_calls 和解析出來的 xml_tool_calls
                    all_calls: list[dict] = []
                    if response_message.tool_calls:
                        for tc in response_message.tool_calls:
                            try:
                                args = json.loads(tc.function.arguments)
                                all_calls.append(
                                    {"name": tc.function.name, "arguments": args}
                                )
                            except json.JSONDecodeError as e:
                                print(
                                    f"Tool call 參數解析失敗 ({tc.function.name}): {e}"
                                )

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

                    if response_message.tool_calls:
                        # 將 AI 的 tool calls 訊息加入歷史
                        messages.append(response_message)

                        # 模擬每個 tool 的回傳結果
                        for tool_call in response_message.tool_calls:
                            fn_name = tool_call.function.name
                            if fn_name == "set_ai_behavior":
                                result = "表情已更新"
                            elif fn_name == "update_user_profile":
                                result = "主人的資料已記住了"
                            elif fn_name == "save_memory_note":
                                result = "已記錄到回憶裡"
                            else:
                                result = "完成"

                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": fn_name,
                                    "content": result,
                                }
                            )

                        # ---- 步驟 4：串流取得最終文字回覆 ----
                        # 注意：第二次呼叫刻意不傳 tools，強制模型只輸出純文字，
                        # 防止無限工具呼叫循環。
                        behavior_payload = _build_behavior_payload(
                            head_intensity, blush_level, eye_sync,
                            eye_l_open, eye_r_open, duration_sec,
                            mouth_form, brow_l_y, brow_r_y,
                            brow_l_angle, brow_r_angle, brow_l_form, brow_r_form,
                        )
                        await websocket.send_json(behavior_payload)
                        await broadcast_to_displays(behavior_payload)
                        content = await stream_final_text(messages, websocket)
                        streamed_output = True
                    else:
                        # 只有 XML Tool Calls，不需要 Second Pass
                        content = content_text
                        if not content:
                            content = "（默默地點頭）"
                else:
                    # 沒有 Tool Calls，直接取文字
                    content = content_text

                # ---- 步驟 5：送出文字與動作到前端 ----
                if content:
                    messages.append({"role": "assistant", "content": content})

                    # AI 未呼叫 set_ai_behavior 時的防呆預設
                    if not has_tool_call:
                        head_intensity = 0.3
                        duration_sec = min(5.0 + len(content) * 0.1, 15.0)

                    # 工具流程未先送 behavior 時，在這裡補送
                    if not streamed_output:
                        behavior_payload = _build_behavior_payload(
                            head_intensity, blush_level, eye_sync,
                            eye_l_open, eye_r_open, duration_sec,
                            mouth_form, brow_l_y, brow_r_y,
                            brow_l_angle, brow_r_angle, brow_l_form, brow_r_form,
                        )
                        await websocket.send_json(behavior_payload)
                        await broadcast_to_displays(behavior_payload)
                        await websocket.send_json(
                            {"type": "text_stream", "content": content}
                        )

                # ---- 步驟 6：背景 Token 計數，檢查是否需要壓縮 ----
                # 壓縮必須在 stream_end 之前完成，確保前端事件順序正確：
                # compressing → compress_done → stream_end（而非 stream_end → compressing）
                token_count = estimate_token_count(messages)
                print(f"目前 token 估算: ~{token_count:,}")

                if token_count >= COMPRESS_TOKEN_THRESHOLD:
                    print(f"Token 數 ({token_count:,}) 接近上限，自動觸發壓縮...")
                    messages = await compress_context(messages, websocket)
                    if CHAT_PERSISTENCE_ENABLED and current_session_id:
                        save_session_messages(current_session_id, messages)

                if CHAT_PERSISTENCE_ENABLED and current_session_id:
                    save_session_messages(current_session_id, messages)

                # 送出結束信號（壓縮完成後再通知前端，事件順序正確）
                await websocket.send_json({"type": "stream_end"})

                # 非阻塞 TTS：在 stream_end 後背景執行，不影響首輪回覆速度
                if content:
                    task = asyncio.create_task(
                        synthesize_and_send_voice(websocket, content, speaking_rate)
                    )
                    tts_tasks.add(task)
                    task.add_done_callback(lambda t: tts_tasks.discard(t))

            except Exception as e:
                print(
                    f"[AI API error][{AI_PROVIDER.upper()}] Model={MODEL_NAME} URL=... | {e}"
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
