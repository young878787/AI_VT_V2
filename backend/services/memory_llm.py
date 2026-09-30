"""只處理已路由 PROCESS 的長期記憶語意決策。"""

import json
import re
from uuid import UUID

from openai import AsyncOpenAI, BadRequestError

from domain.memory_decisions import SUBMIT_MEMORY_DECISIONS_TOOL, validate_decisions
from domain.memory_settings import MemorySettings


_FORGET_REQUEST = re.compile(
    r"(?:請|幫我|麻煩)(?:把|將)?[^。！？]{0,40}?(?:忘記|刪除記憶)"
    r"|\b(?:please forget|forget about|delete my memory)\b", re.I,
)


class MemoryLLM:
    def __init__(self, settings: MemorySettings) -> None:
        self.client = AsyncOpenAI(base_url=settings.memory_base_url, api_key=settings.memory_api_key)
        self.model = settings.memory_model

    async def decide(self, job: dict, buffered: list[dict], related: list[dict]) -> list[dict]:
        related_payload = [
            {key: str(value) if isinstance(value, UUID) else value
             for key, value in item.items() if key in {
                 "id", "group_id", "memory_type", "canonical_text", "subject_key", "status",
                 "importance", "confidence", "retention_class",
             }}
            for item in related[:20]
        ]
        source = {
            "current_user_input": job["source_text"][:4000],
            "recent_dialogue": (job.get("recent_dialogue") or [])[-16:],
            "buffered_context": [
                {"id": str(item["id"]), "text": item["source_text"][:4000]}
                for item in buffered[:3] if item.get("source_text")
            ],
        }
        payload = {
            "source": source,
            "jev_hints": {
                "memory_type": job.get("memory_type_hint"),
                "importance": job.get("importance_hint"),
                "explicit_memory": job.get("explicit_memory"),
            },
            "related_existing_memories": related_payload,
        }
        request = dict(
            model=self.model,
            messages=[
                {"role": "system", "content": (
                    "You manage long-term memory. Return only the submit_memory_decisions tool call. "
                    "Use only supplied source and candidate IDs. Do not invent target IDs. "
                    "Use FORGET only for an explicit user request to erase memory; changes of fact use SUPERSEDE or ARCHIVE. "
                    "Use IGNORE for a question about recent dialogue when it adds no durable user fact. "
                    "For new or changed facts include canonical_text, memory_type, importance, confidence, and retention_class. "
                    "If retention_class is temporary, include expires_at as an ISO 8601 timestamp with a timezone. "
                    "Return zero or more atomic decisions."
                )},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
            tools=[SUBMIT_MEMORY_DECISIONS_TOOL],
            tool_choice={"type": "function", "function": {"name": "submit_memory_decisions"}},
        )
        try:
            response = await self.client.chat.completions.create(**request)
        except BadRequestError as error:
            if (getattr(error, "param", None) != "reasoning_effort"
                    or "set reasoning_effort to 'none'" not in str(error)):
                raise
            response = await self.client.chat.completions.create(**request, reasoning_effort="none")
        calls = response.choices[0].message.tool_calls if response.choices else None
        if not calls or len(calls) != 1 or calls[0].function.name != "submit_memory_decisions":
            raise ValueError("Memory LLM 未使用指定 tool")
        payload = json.loads(calls[0].function.arguments)
        return validate_decisions(
            payload, {item["id"] for item in related},
            explicit_forget=bool(_FORGET_REQUEST.search(job["source_text"])),
        )
