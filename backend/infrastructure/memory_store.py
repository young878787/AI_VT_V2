"""
記憶持久化：user_profile.json、memory.md、sessions/ 的 File I/O。
包含 In-Memory Cache 以減少磁碟讀取次數。
"""
import os
import json
import tempfile
import threading
from datetime import datetime

from core.config import (
    USER_PROFILE_PATH,
    MEMORY_MD_PATH,
    CHAT_SESSION_DIR,
    MEMORY_DIR,
    CHAT_PERSISTENCE_MAX_MESSAGES,
    EMOTION_STATE_DIR,
)
from core.utils import get_msg_field
from core.utils import normalize_session_id
from domain.emotion_state import validate_emotion_state

# ============================================================
# In-Memory Cache（減少每輪對話的磁碟 I/O）
# ============================================================
_profile_cache: dict | None = None
_memory_cache: str | None = None
_write_lock = threading.RLock()


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
# User Profile
# ============================================================
def load_user_profile() -> dict:
    """讀取 user_profile.json（優先從 cache，減少磁碟 I/O）"""
    global _profile_cache
    if _profile_cache is not None:
        return _profile_cache
    try:
        with open(USER_PROFILE_PATH, "r", encoding="utf-8") as f:
            _profile_cache = json.load(f)
            return _profile_cache
    except (FileNotFoundError, json.JSONDecodeError):
        _profile_cache = {
            "updated_at": "",
            "core_traits": [],
            "communication_style": "",
            "dislikes": [],
            "recent_interests": [],
            "custom_notes": [],
        }
        return _profile_cache


def save_user_profile(profile: dict) -> None:
    """寫入 user_profile.json，同步更新 cache"""
    global _profile_cache
    profile["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _atomic_write(USER_PROFILE_PATH, json.dumps(profile, ensure_ascii=False, indent=2))
    _profile_cache = profile


# ============================================================
# Memory Notes（memory.md）
# ============================================================
def load_memory_notes(max_lines: int = 50) -> str:
    """讀取 memory.md 最後 N 行（優先從 cache，減少磁碟 I/O）"""
    global _memory_cache
    if _memory_cache is not None:
        return _memory_cache
    try:
        with open(MEMORY_MD_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        content_lines = [
            l.strip() for l in lines if l.strip() and not l.strip().startswith("# ")
        ]
        _memory_cache = "\n".join(content_lines[-max_lines:])
        return _memory_cache
    except FileNotFoundError:
        _memory_cache = ""
        return _memory_cache


def append_memory_note(note: str) -> None:
    """追加一條記憶到 memory.md，並使 cache 失效（下次重新讀取）"""
    global _memory_cache
    with _write_lock:
        try:
            with open(MEMORY_MD_PATH, "r", encoding="utf-8") as file:
                existing = file.read()
        except FileNotFoundError:
            existing = "# Memory Notes\n"
        date_prefix = datetime.now().strftime("[%m/%d %H:%M]")
        _atomic_write(MEMORY_MD_PATH, existing + f"\n- {date_prefix} {note}")
        _memory_cache = None


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


# ============================================================
# 還原（Reset）
# ============================================================
def reset_user_profile() -> None:
    """還原 user_profile.json 為預設值，同步清除 cache。"""
    default_profile = {
        "updated_at": "",
        "core_traits": [],
        "communication_style": "",
        "dislikes": [],
        "recent_interests": [],
        "custom_notes": [],
    }
    save_user_profile(default_profile)


def reset_memory_notes() -> None:
    """清空 memory.md，同步清除 cache。"""
    global _memory_cache
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(MEMORY_MD_PATH, "w", encoding="utf-8") as f:
        f.write("# Memory Notes\n")
    _memory_cache = None


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
    """只持久化 user/assistant 純文字，避免儲存動態 system prompt 與 tool 訊息。"""
    persisted: list[dict] = []
    for m in messages:
        role = get_msg_field(m, "role", "")
        if role not in {"user", "assistant"}:
            continue
        content = get_msg_field(m, "content", "")
        if isinstance(content, str) and content:
            persisted.append({"role": role, "content": content})

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
                restored.append({"role": role, "content": content})
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
