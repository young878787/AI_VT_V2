"""舊檔案記憶的一次性、唯讀解析。"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5


LEGACY_NAMESPACE = UUID("c5946705-dcd5-57d1-9a8c-917d7d2fa9dc")
_PROFILE_FIELDS = {"core_traits", "communication_style", "dislikes", "recent_interests", "custom_notes"}


@dataclass(frozen=True)
class LegacyEntry:
    id: UUID
    canonical_text: str
    memory_type: str
    subject_key: str | None
    importance: float
    status: str
    observed_at: datetime
    source_name: str


def _time(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is not None:
                return parsed
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _entry(source: str, key: str, text: str, memory_type: str, subject_key: str | None,
           importance: float, status: str, observed_at: datetime) -> LegacyEntry:
    return LegacyEntry(uuid5(LEGACY_NAMESPACE, f"{source}:{key}"), text[:2000], memory_type,
                       subject_key, importance, status, observed_at, source)


def read_legacy_entries(memory_dir: Path) -> list[LegacyEntry]:
    """依定案來源順序讀檔；不修改、備份或覆寫原檔。"""
    entries: list[LegacyEntry] = []
    profile_path = memory_dir / "user_profile.json"
    if profile_path.exists():
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        if not isinstance(profile, dict):
            raise ValueError("user_profile.json 格式無效")
        observed_at = _time(profile.get("updated_at"))
        for field in sorted(_PROFILE_FIELDS):
            value = profile.get(field)
            values = value if isinstance(value, list) else [value]
            for item in values:
                if not isinstance(item, str) or not item.strip():
                    continue
                cleaned = item.strip()
                stable_key = f"{field}:{cleaned}" if isinstance(value, list) else field
                entries.append(_entry("user_profile.json", stable_key, cleaned, "profile",
                                      f"profile.{field}", 0.7, "active", observed_at))

    records_path = memory_dir / "memory_records.json"
    if records_path.exists():
        records = json.loads(records_path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("memory_records.json 格式無效")
        for index, record in enumerate(records):
            if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                continue
            content = record["text"].strip()
            if not content:
                continue
            importance = record.get("importance", 0.6)
            if isinstance(importance, bool) or not isinstance(importance, (int, float)) or not 0 <= importance <= 1:
                importance = 0.6
            status = record.get("status") if record.get("status") in {"active", "archived"} else "archived"
            entries.append(_entry(
                "memory_records.json", str(record.get("id", index)), content,
                "special" if record.get("type") == "special" else "event", None,
                float(importance), status, _time(record.get("created_at")),
            ))
    else:
        notes_path = memory_dir / "memory.md"
        if notes_path.exists():
            for index, line in enumerate(notes_path.read_text(encoding="utf-8").splitlines()):
                if not line.startswith("- ") or "[對話摘要]" in line:
                    continue
                text = re.sub(r"<!-- op:[a-f0-9]+ -->", "", line[2:]).strip()
                text = re.sub(r"^\[\d{2}/\d{2} \d{2}:\d{2}\]\s*", "", text)
                if text:
                    entries.append(_entry("memory.md", str(index), text, "event", None,
                                          0.6, "active", datetime.now(timezone.utc)))
    return entries


def import_summary(entries: list[LegacyEntry]) -> dict:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry.source_name] = counts.get(entry.source_name, 0) + 1
    return {"total": len(entries), "sources": counts}
