"""將 PostgreSQL 長期記憶投影成既有 Chat prompt 可使用的有界資料。"""

import json
import re
from uuid import UUID

from infrastructure.memory_embedding_client import MemoryEmbeddingClient
from infrastructure.memory_repository import MemoryRepository


_HISTORY = re.compile(r"以前|之前|曾經|過去|歷史|原本|before|previously|used to", re.I)
_FUTURE = re.compile(r"未來|下週|下個月|明天|將要|future|next week|tomorrow", re.I)
_PROFILE_LIST_FIELDS = {"core_traits", "dislikes", "recent_interests", "custom_notes"}


class MemoryRetriever:
    def __init__(self, repository: MemoryRepository, embedding: MemoryEmbeddingClient) -> None:
        self.repository = repository
        self.embedding = embedding

    async def retrieve(self, user_text: str, event_id: UUID | None = None) -> tuple[dict, str]:
        mode = "history" if _HISTORY.search(user_text) else "future" if _FUTURE.search(user_text) else "current"
        try:
            query_embedding = await self.embedding.embed(
                user_text[:4000], query=True,
                purpose="retrieval_query" if event_id is not None else None,
                event_id=event_id, stage="chat_retrieval",
            )
        except Exception as exc:
            print(f"[Memory] embedding query fallback: {type(exc).__name__}")
            query_embedding = None
        try:
            rows = await self.repository.related_items(user_text, query_embedding, limit=20, mode=mode)
        except Exception as exc:
            print(f"[Memory] retrieval fallback: {type(exc).__name__}")
            return {}, ""
        profile: dict = {}
        injected = []
        for row in rows:
            if row["memory_type"] != "profile" or row["status"] != "active" or row.get("has_conflict") or row.get("pending_change"):
                continue
            key = row.get("subject_key")
            text = row.get("canonical_text")
            if not isinstance(key, str) or not key.startswith("profile.") or not isinstance(text, str):
                continue
            field = key.removeprefix("profile.")
            if field in _PROFILE_LIST_FIELDS:
                profile.setdefault(field, []).append(text[:300])
            elif field == "communication_style":
                profile[field] = text[:300]
            else:
                continue
            injected.append(row)
        selected = []
        seen_groups = set()
        remaining = 800
        for row in rows:
            if row in injected:
                continue
            if len(selected) >= 8 or remaining <= 0:
                break
            label = f"[{row['status']}] " if mode != "current" or row["status"] == "conflict" else ""
            if row.get("has_conflict"):
                label += "[存在衝突，尚未確認] "
            if row.get("pending_change"):
                label += "[更正或遺忘尚未完成，勿視為確定現況] "
            if mode in {"history", "future"}:
                label += f"[{row.get('valid_from')} ~ {row.get('valid_to')}] "
            text = (label + row["canonical_text"])[:remaining]
            selected.append(text)
            remaining -= len(text)
            seen_groups.add(row["group_id"])
            injected.append(row)
        if event_id is not None:
            print("[Memory] retrieval candidates: " + json.dumps({
                "event_id": str(event_id),
                "candidates": [
                    {"id": str(row["id"]), "similarity": row["similarity"],
                     "exact_match": row["exact_match"]}
                    for row in injected
                ],
            }, separators=(",", ":")))
        return profile, "\n".join(selected)
