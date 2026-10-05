"""聊天輸入的純資料正規化邊界。"""
import re
import time
from uuid import uuid4

_TURN_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def normalize_chat_input(data: dict, fallback_session_id: str | None = None) -> dict | None:
    text = data.get("content")
    if not isinstance(text, str) or not text.strip():
        return None
    raw_turn_id = data.get("turn_id")
    turn_id = raw_turn_id if isinstance(raw_turn_id, str) and _TURN_ID.fullmatch(raw_turn_id) else uuid4().hex
    return {
        "text": text.strip(), "session_id": fallback_session_id, "turn_id": turn_id,
        "model_name": "Rushia",
        "source": "voice" if data.get("source") == "voice" else "text",
        "timestamp": time.time(), "user_id": "default_user",
    }
