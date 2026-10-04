"""將 PostgreSQL 長期記憶投影成既有 Chat prompt 可使用的有界資料。"""

import json
import re
import time
from uuid import UUID

from infrastructure.memory_embedding_client import MemoryEmbeddingClient
from infrastructure.memory_repository import MemoryRepository
from infrastructure.memory_repository import MIN_RETRIEVAL_SIMILARITY
from core.prompt_logger import trace


_HISTORY = re.compile(r"以前|之前|曾經|過去|歷史|原本|before|previously|used to", re.I)
_FUTURE = re.compile(r"未來|下週|下個月|明天|將要|future|next week|tomorrow", re.I)
_PROFILE_LIST_FIELDS = {"core_traits", "dislikes", "recent_interests", "custom_notes"}


_CONTEXT_REFERENCE = re.compile(r"那個|那部分|那件事|這個|這部分|這件事|它|剛才|剛剛|我的口味|我的喜好|今晚的安排|\b(it|that|my taste)\b", re.I)


def _format_memory_projection(row: dict, mode: str, remaining: int) -> str:
    """把候選的來源欄位一起交給 Chat，避免只剩脫離上下文的 canonical 句子。"""
    memory_type = str(row.get("memory_type") or "unknown")
    subject_key = str(row.get("subject_key") or "未標註")
    status = str(row.get("status") or "unknown")
    metadata = [f"type={memory_type}", f"subject_key={subject_key}", f"status={status}", "actor=未標註"]
    if row.get("has_conflict"):
        metadata.append("conflict=true")
    if row.get("pending_change"):
        metadata.append("pending_change=true")
    if mode in {"history", "future"}:
        metadata.append(f"valid_from={row.get('valid_from')}")
        metadata.append(f"valid_to={row.get('valid_to')}")
    prefix = "- 記憶資料（" + "; ".join(metadata) + "）：fact="
    return (prefix + str(row.get("canonical_text") or ""))[:remaining]


def build_retrieval_query(user_text: str, recent_dialogue: list[dict] | None = None, summary: str = "") -> str:
    """指代型查詢使用有界 user 上下文，必要時沿用既有 summary。"""
    if not _CONTEXT_REFERENCE.search(user_text):
        return user_text[:4000]
    user_context = [m["content"][:400] for m in (recent_dialogue or [])[-16:]
                    if m.get("role") == "user" and isinstance(m.get("content"), str)][-2:]
    context = "\n".join(user_context) if user_context else summary[:800]
    return user_text[:3000] + ("\n當前對話情境：\n" + context if context else "")


class MemoryRetriever:
    def __init__(self, repository: MemoryRepository, embedding: MemoryEmbeddingClient) -> None:
        self.repository = repository
        self.embedding = embedding

    async def retrieve(self, user_text: str, event_id: UUID | None = None,
                       recent_dialogue: list[dict] | None = None, summary: str = "") -> tuple[dict, str]:
        started = time.monotonic()
        mode = "history" if _HISTORY.search(user_text) else "future" if _FUTURE.search(user_text) else "current"
        query = build_retrieval_query(user_text, recent_dialogue, summary)
        errors = []
        try:
            query_embedding = await self.embedding.embed(
                query, query=True,
                purpose="retrieval_query" if event_id is not None else None,
                event_id=event_id, stage="chat_retrieval",
            )
        except Exception as exc:
            errors.append({"stage": "embedding", "error": type(exc).__name__})
            print(f"[Memory] embedding query fallback: {type(exc).__name__}")
            query_embedding = None
        try:
            rows = await self.repository.related_items(query, query_embedding, limit=20, mode=mode)
        except Exception as exc:
            print(f"[Memory] retrieval fallback: {type(exc).__name__}")
            trace("retrieval", {"query": query, "user_query": user_text, "mode": mode, "limit": 20,
                "min_similarity": MIN_RETRIEVAL_SIMILARITY, "candidates": [], "projections": [],
                "errors": [*errors, {"stage": "repository", "error": type(exc).__name__}],
                "duration_sec": round(time.monotonic() - started, 4)}, event_id)
            return {}, ""
        profile: dict = {}
        injected = []
        projections = []
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
            if field == "communication_style":
                projections = [item for item in projections if item.get("field") != field]
            projections.append({"id": str(row["id"]), "destination": "profile", "field": field,
                                "text": text[:300]})
        selected = []
        remaining = 800
        for row in rows:
            if row in injected:
                continue
            if len(selected) >= 8 or remaining <= 0:
                break
            text = _format_memory_projection(row, mode, remaining)
            selected.append(text)
            remaining -= len(text)
            injected.append(row)
            projections.append({"id": str(row["id"]), "destination": "memory", "text": text})
        trace("retrieval", {"query": query, "user_query": user_text, "mode": mode, "limit": 20,
            "min_similarity": MIN_RETRIEVAL_SIMILARITY, "memory_limit": 8, "memory_char_budget": 800,
            "profile_char_limit": 300, "embedding_available": query_embedding is not None,
            "candidates": [{"rank": index + 1,
                **{key: row.get(key) for key in ("id", "group_id", "canonical_text", "memory_type",
                    "subject_key", "similarity", "exact_match", "status", "valid_from", "valid_to",
                    "expires_at", "has_conflict", "pending_change")},
                "projection": ("selected" if any(item["id"] == str(row["id"]) for item in projections)
                               else "profile_overwritten" if row in injected else "memory_budget_exhausted")}
                for index, row in enumerate(rows)], "projections": projections,
            "errors": errors, "duration_sec": round(time.monotonic() - started, 4)}, event_id)
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
