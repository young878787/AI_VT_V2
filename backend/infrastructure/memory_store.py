"""Legacy JSON helpers kept only for explicit import and historical tests.

Runtime Chat state is stored by :mod:`chat_session_repository`; no production
request path may use this module as a fallback.
"""
import os
import json
import tempfile
from datetime import datetime

from core.config import CHAT_SESSION_MAX_MESSAGES, MEMORY_DIR
from core.utils import get_msg_field
from core.utils import normalize_session_id
from domain.emotion_state import validate_emotion_state
from domain.memory_source import MEMORY_SOURCE_FIELD, read_memory_source

CHAT_SESSION_DIR = os.path.join(MEMORY_DIR, "sessions")
EMOTION_STATE_DIR = os.path.join(MEMORY_DIR, "emotion_states")

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
    """持久化可見文字；user 來源 metadata 僅在完整有效時保留。"""
    persisted: list[dict] = []
    for m in messages:
        role = get_msg_field(m, "role", "")
        if role not in {"user", "assistant"}:
            continue
        content = get_msg_field(m, "content", "")
        if isinstance(content, str) and content:
            item = {"role": role, "content": content}
            if role == "user" and read_memory_source(m) is not None:
                item[MEMORY_SOURCE_FIELD] = dict(m[MEMORY_SOURCE_FIELD])
            if role == "assistant" and m.get("status") == "interrupted":
                item["status"] = "interrupted"
            persisted.append(item)

    if len(persisted) > CHAT_SESSION_MAX_MESSAGES:
        persisted = persisted[-CHAT_SESSION_MAX_MESSAGES:]
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
                if role == "user" and read_memory_source(item) is not None:
                    restored_item[MEMORY_SOURCE_FIELD] = dict(item[MEMORY_SOURCE_FIELD])
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
        _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        print(f"寫入 session 失敗 ({session_id}): {e}")


def _session_file(session_id: str) -> str:
    normalized = normalize_session_id(session_id)
    if not normalized or normalized != session_id:
        raise ValueError("無效的 session_id")
    return os.path.join(CHAT_SESSION_DIR, f"{normalized}.json")


def list_session_records() -> list[dict]:
    """列出目前短期持久化目錄中的 session，不讀取長期記憶。"""
    try:
        names = os.listdir(CHAT_SESSION_DIR)
    except FileNotFoundError:
        return []
    records = []
    for name in names:
        if not name.endswith(".json") or name.endswith(".summary.json"):
            continue
        session_id = name[:-5]
        if normalize_session_id(session_id) != session_id:
            continue
        path = os.path.join(CHAT_SESSION_DIR, name)
        try:
            messages = load_session_messages(session_id)
            modified_at = datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat()
            summary_path = _session_summary_path(session_id)
            emotion_path = _emotion_state_path(session_id)
            first_user = next((item["content"] for item in messages if item.get("role") == "user"), "")
            records.append({
                "session_id": session_id,
                "message_count": len(messages),
                "preview": first_user[:160],
                "updated_at": modified_at,
                "has_summary": os.path.exists(summary_path),
                "has_emotion_state": os.path.exists(emotion_path),
            })
        except (OSError, ValueError):
            continue
    return sorted(records, key=lambda item: item["updated_at"], reverse=True)


def get_session_record(session_id: str) -> dict | None:
    """回傳指定 session 的短期資料；不存在時回 None。"""
    normalized = normalize_session_id(session_id)
    if not normalized or normalized != session_id:
        raise ValueError("無效的 session_id")
    path = _session_file(normalized)
    if not os.path.exists(path):
        return None
    messages = load_session_messages(normalized)
    return {
        "session_id": normalized,
        "messages": messages,
        "summary": load_session_summary(normalized),
        "emotion_state": load_session_emotion_state(normalized),
        "updated_at": datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat(),
    }


def delete_session(session_id: str) -> dict:
    """刪除指定 session 的訊息、摘要與情緒檔案。"""
    normalized = normalize_session_id(session_id)
    if not normalized or normalized != session_id:
        raise ValueError("無效的 session_id")
    paths = (
        _session_file(normalized),
        _session_summary_path(normalized),
        _emotion_state_path(normalized),
    )
    deleted = []
    for path in paths:
        try:
            os.unlink(path)
            deleted.append(path)
        except FileNotFoundError:
            pass
    return {"session_id": normalized, "deleted_files": len(deleted)}


def delete_all_sessions() -> dict:
    """刪除所有可辨識的短期 session 檔案。"""
    session_ids = [item["session_id"] for item in list_session_records()]
    deleted_files = sum(delete_session(session_id)["deleted_files"] for session_id in session_ids)
    return {"session_count": len(session_ids), "deleted_files": deleted_files}
