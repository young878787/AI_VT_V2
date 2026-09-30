"""
短期 Chat session、Session Summary 與 Emotion State 的檔案持久化。
長期記憶統一由 PostgreSQL MemoryRuntime 管理。
"""
import os
import json
import tempfile

from core.config import CHAT_SESSION_DIR, CHAT_PERSISTENCE_MAX_MESSAGES, EMOTION_STATE_DIR
from core.utils import get_msg_field
from core.utils import normalize_session_id
from domain.emotion_state import validate_emotion_state

# ============================================================
def _atomic_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path), delete=False) as file:
            temporary_path = file.name
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


# ============================================================
# Session Emotion State
# ============================================================
def _emotion_state_path(session_id: str) -> str:
    normalized = normalize_session_id(session_id)
    if not normalized or normalized != session_id:
        raise ValueError("無效的 session_id")
    return os.path.join(EMOTION_STATE_DIR, f"{normalized}.json")


def load_session_emotion_state(session_id: str) -> dict | None:
    """缺檔或內容不符合契約時回 None，不載入舊版 JPAF 資料。"""
    try:
        with open(_emotion_state_path(session_id), "r", encoding="utf-8") as f:
            return validate_emotion_state(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_session_emotion_state(session_id: str, state: dict) -> None:
    """先驗證，再以同目錄暫存檔原子替換 session state。"""
    validated = validate_emotion_state(state)
    if validated is None:
        raise ValueError("無效的 Emotion State")
    path = _emotion_state_path(session_id)
    os.makedirs(EMOTION_STATE_DIR, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=EMOTION_STATE_DIR, delete=False
        ) as file:
            temporary_path = file.name
            json.dump(validated, file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def reset_session_emotion_state(session_id: str) -> None:
    try:
        os.unlink(_emotion_state_path(session_id))
    except FileNotFoundError:
        pass


def _session_summary_path(session_id: str) -> str:
    normalized = normalize_session_id(session_id)
    if not normalized or normalized != session_id:
        raise ValueError("無效的 session_id")
    return os.path.join(CHAT_SESSION_DIR, f"{normalized}.summary.json")


def load_session_summary(session_id: str) -> str:
    try:
        with open(_session_summary_path(session_id), "r", encoding="utf-8") as file:
            value = json.load(file)
        return value.get("summary", "") if isinstance(value, dict) else ""
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def save_session_summary(session_id: str, summary: str) -> None:
    _atomic_write(_session_summary_path(session_id), json.dumps({"summary": summary[:4000]}, ensure_ascii=False))


def reset_session_summary(session_id: str) -> None:
    try:
        os.unlink(_session_summary_path(session_id))
    except FileNotFoundError:
        pass


# ============================================================
# Chat Sessions
# ============================================================
def to_persistable_messages(messages: list) -> list[dict]:
    """只持久化 user/assistant 純文字及中斷標記。"""
    persisted: list[dict] = []
    for m in messages:
        role = get_msg_field(m, "role", "")
        if role not in {"user", "assistant"}:
            continue
        content = get_msg_field(m, "content", "")
        if isinstance(content, str) and content:
            item = {"role": role, "content": content}
            if role == "assistant" and m.get("status") == "interrupted":
                item["status"] = "interrupted"
            persisted.append(item)

    if len(persisted) > CHAT_PERSISTENCE_MAX_MESSAGES:
        persisted = persisted[-CHAT_PERSISTENCE_MAX_MESSAGES:]
    return persisted


def load_session_messages(session_id: str) -> list[dict]:
    """讀取指定 session 的對話歷史。"""
    path = os.path.join(CHAT_SESSION_DIR, f"{session_id}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        restored: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content:
                restored_item = {"role": role, "content": content}
                if role == "assistant" and item.get("status") == "interrupted":
                    restored_item["status"] = "interrupted"
                restored.append(restored_item)
        return restored
    except FileNotFoundError:
        return []
    except Exception as e:
        print(f"讀取 session 失敗 ({session_id}): {e}")
        return []


def save_session_messages(session_id: str, messages: list) -> None:
    """寫入指定 session 的對話歷史。"""
    try:
        os.makedirs(CHAT_SESSION_DIR, exist_ok=True)
        path = os.path.join(CHAT_SESSION_DIR, f"{session_id}.json")
        data = to_persistable_messages(messages)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"寫入 session 失敗 ({session_id}): {e}")
