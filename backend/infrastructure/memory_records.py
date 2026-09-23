"""可回復的長期記憶紀錄；舊 memory.md 僅作匯入與相容檢視。"""
import json
import os
import re
import shutil
from datetime import datetime, timedelta, timezone

from core.config import MEMORY_DIR, MEMORY_MD_PATH
from infrastructure.memory_store import _atomic_write, _write_lock
from infrastructure import memory_store

RECORDS_PATH = os.path.join(MEMORY_DIR, "memory_records.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _record(record_id: str, text: str, source_turn_id: str | None = None, classification: dict | None = None) -> dict:
    classification = classification or {}
    special = classification.get("memory_type") == "special" or any(word in text for word in ("生日", "紀念日", "承諾", "第一次見面"))
    importance = classification.get("importance", 1.0 if special else 0.6)
    if not isinstance(importance, (int, float)) or isinstance(importance, bool) or not 0 <= importance <= 1:
        importance = 1.0 if special else 0.6
    ttl = classification.get("ttl")
    created = _now().isoformat()
    return {
        "id": record_id, "type": "special" if special else "long_term",
        "text": text[:1000], "importance": 1.0 if special else float(importance),
        "created_at": created, "last_accessed_at": created, "access_count": 0,
        "expires_at": (_now() + timedelta(days=365)).isoformat() if ttl == "long" and not special else None,
        "protected": special,
        "source_turn_id": source_turn_id, "status": "active",
    }


def load_records() -> list[dict]:
    with _write_lock:
        try:
            with open(RECORDS_PATH, "r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, list) else []
        except FileNotFoundError:
            pass
        try:
            with open(MEMORY_MD_PATH, "r", encoding="utf-8") as file:
                old = file.read()
        except FileNotFoundError:
            old = ""
        records = []
        for index, line in enumerate(old.splitlines()):
            if not line.startswith("- ") or "[對話摘要]" in line:
                continue
            cleaned = re.sub(r"<!-- op:[a-f0-9]+ -->", "", line[2:]).strip()
            if cleaned:
                records.append(_record(f"legacy-{index}", cleaned))
        if old:
            backup = MEMORY_MD_PATH + ".pre-records.bak"
            if not os.path.exists(backup):
                shutil.copy2(MEMORY_MD_PATH, backup)
        _atomic_write(RECORDS_PATH, json.dumps(records, ensure_ascii=False, indent=2))
        return records


def _save(records: list[dict]) -> None:
    _atomic_write(RECORDS_PATH, json.dumps(records, ensure_ascii=False, indent=2))


def _refresh_legacy_view(records: list[dict]) -> None:
    lines = ["# Memory Notes", *[f"- {item['text']}" for item in records if item["status"] == "active"]]
    _atomic_write(MEMORY_MD_PATH, "\n".join(lines) + "\n")
    memory_store._memory_cache = None


def append_record_once(text: str, operation_id: str, turn_id: str, classification: dict | None = None) -> bool:
    with _write_lock:
        if classification and (classification.get("ttl") == "session" or classification.get("memory_type") == "short_term"):
            return False
        records = load_records()
        if any(item["id"] == operation_id for item in records):
            _refresh_legacy_view(records)
            return False
        if any(item["status"] == "active" and item["text"].strip() == text.strip() for item in records):
            return False
        records.append(_record(operation_id, text, turn_id, classification))
        _save(records)
        _refresh_legacy_view(records)
        return True


def search_relevant_records(query: str, max_items: int = 5, max_chars: int = 800) -> str:
    """中文雙字片段／英文詞命中；讀取時記錄使用頻率供封存計分。"""
    def terms(value: str) -> set[str]:
        words = set(re.findall(r"[a-z0-9]+", value.lower()))
        for run in re.findall(r"[\u3400-\u9fff]+", value):
            words.update(run[index:index + 2] for index in range(len(run) - 1))
        return words

    with _write_lock:
        records = load_records()
        query_terms = terms(query)
        ranked = sorted(
            (item for item in records if item["status"] == "active" and query_terms & terms(item["text"])),
            key=lambda item: (len(query_terms & terms(item["text"])), item["created_at"]),
            reverse=True,
        )
        selected = []
        remaining = max_chars
        for item in ranked:
            if len(selected) >= max_items or remaining <= 0:
                break
            text = item["text"][:remaining]
            if text:
                selected.append(text)
                remaining -= len(text)
                item["access_count"] += 1
                item["last_accessed_at"] = _now().isoformat()
        if selected:
            _save(records)
        return "\n".join(selected)


def archive_expired_records() -> int:
    """只封存到期或低保留分數的普通紀錄，不刪除內容。"""
    with _write_lock:
        records = load_records()
        now = _now()
        changed = 0
        for item in records:
            if item["status"] != "active" or item["protected"]:
                continue
            created = datetime.fromisoformat(item["created_at"])
            age_days = max(0, (now - created).total_seconds() / 86400)
            score = 0.7 * item["importance"] + 0.2 * 2 ** (-age_days / 30) + 0.1 * min(item["access_count"] / 5, 1)
            expires = item["expires_at"] and datetime.fromisoformat(item["expires_at"]) <= now
            if expires or (age_days >= 30 and score < 0.25):
                item["status"] = "archived"
                changed += 1
        if changed:
            _save(records)
            _refresh_legacy_view(records)
        return changed


def reset_records() -> None:
    with _write_lock:
        _save([])
        _refresh_legacy_view([])
