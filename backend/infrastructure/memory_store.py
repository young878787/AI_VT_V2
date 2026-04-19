"""
記憶持久化：user_profile.json、memory.md、sessions/ 的 File I/O。
包含 In-Memory Cache 以減少磁碟讀取次數。
"""
import os
import json
from datetime import datetime

from core.config import (
    USER_PROFILE_PATH,
    MEMORY_MD_PATH,
    CHAT_SESSION_DIR,
    MEMORY_DIR,
    CHAT_PERSISTENCE_MAX_MESSAGES,
    JPAF_STATE_PATH,
)
from core.utils import get_msg_field

# ============================================================
# In-Memory Cache（減少每輪對話的磁碟 I/O）
# ============================================================
_profile_cache: dict | None = None
_memory_cache: str | None = None
_jpaf_state_cache: dict | None = None


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
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(USER_PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=2)
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
        # 跳過標題行，取最後 max_lines 條有效內容
        content_lines = [
            l.strip() for l in lines if l.strip() and not l.strip().startswith("# ")
        ]
        recent = (
            content_lines[-max_lines:]
            if len(content_lines) > max_lines
            else content_lines
        )
        _memory_cache = "\n".join(recent)
        return _memory_cache
    except FileNotFoundError:
        _memory_cache = ""
        return _memory_cache


def append_memory_note(note: str) -> None:
    """追加一條記憶到 memory.md，並使 cache 失效（下次重新讀取）"""
    global _memory_cache
    os.makedirs(MEMORY_DIR, exist_ok=True)
    date_prefix = datetime.now().strftime("[%m/%d %H:%M]")
    with open(MEMORY_MD_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n- {date_prefix} {note}")
    _memory_cache = None  # 使 cache 失效，下次重新讀取最新內容


# ============================================================
# JPAF State（jpaf_state.json）
# ============================================================
def load_jpaf_state() -> dict | None:
    """讀取 jpaf_state.json（優先從 cache）。回傳 None 表示尚未建立。"""
    global _jpaf_state_cache
    if _jpaf_state_cache is not None:
        return _jpaf_state_cache
    try:
        with open(JPAF_STATE_PATH, "r", encoding="utf-8") as f:
            _jpaf_state_cache = json.load(f)
            return _jpaf_state_cache
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_jpaf_state(state: dict) -> None:
    """寫入 jpaf_state.json，同步更新 cache。"""
    global _jpaf_state_cache
    os.makedirs(MEMORY_DIR, exist_ok=True)
    with open(JPAF_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    _jpaf_state_cache = state


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


def reset_jpaf_state() -> None:
    """還原 jpaf_state.json 為預設值（預設 persona），同步清除 cache。"""
    from domain.jpaf import JPAFSession
    default_session = JPAFSession()
    save_jpaf_state(default_session.to_dict())


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
