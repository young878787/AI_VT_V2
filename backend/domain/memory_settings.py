"""長期記憶啟用時所需的固定設定。"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID

from domain.memory_scope import MemoryScope


EMBEDDING_DIMENSION = 1024
RETRIEVAL_INSTRUCTION = (
    "Given a user's current message, retrieve memories about the same person, preference, "
    "project, event, or correction that are relevant to answering the message.\n"
)


@dataclass(frozen=True)
class MemorySettings:
    database_url: str
    scope: MemoryScope
    memory_api_key: str
    memory_base_url: str
    memory_model: str
    embedding_api_key: str
    embedding_base_url: str
    embedding_model: str
    embedding_serving_model: str
    embedding_dimension: int
    embedding_query_prefix: str
    embedding_document_prefix: str

    @property
    def embedding_contract(self) -> str:
        contract = json.dumps(
            [self.embedding_model, self.embedding_dimension, self.embedding_query_prefix,
             self.embedding_document_prefix, "l2-v1"],
            ensure_ascii=False, separators=(",", ":"),
        )
        return hashlib.sha256(contract.encode("utf-8")).hexdigest()


def load_memory_settings(environment: dict[str, str] | None = None) -> MemorySettings:
    env = os.environ if environment is None else environment

    def required(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise RuntimeError(f"{name} 未設定")
        return value

    database_url = required("MEMORY_DATABASE_URL")
    if urlparse(database_url).scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("MEMORY_DATABASE_URL 必須是 PostgreSQL URL")
    try:
        scope = MemoryScope(
            UUID(required("MEMORY_DEFAULT_USER_ID")),
            UUID(required("MEMORY_DEFAULT_CHARACTER_ID")),
            required("MEMORY_DATABASE_SCHEMA"),
        )
    except ValueError as exc:
        raise RuntimeError("MemoryScope 設定無效") from exc
    if env.get("AI_VT_TEST_MODE", "").lower() in {"1", "true", "yes", "on"}:
        if not re.fullmatch(r"test_[0-9a-f]{32}", scope.schema_name):
            raise RuntimeError("測試模式必須使用 test_<32 lowercase hex> schema")
        if database_url != required("MEMORY_TEST_DATABASE_URL"):
            raise RuntimeError("測試模式只能連接 MEMORY_TEST_DATABASE_URL")
    try:
        dimension = int(required("EMBEDDING_AI_DIMENSION"))
    except ValueError as exc:
        raise RuntimeError("EMBEDDING_AI_DIMENSION 必須是 1024") from exc
    if dimension != EMBEDDING_DIMENSION:
        raise RuntimeError("EMBEDDING_AI_DIMENSION 必須是 1024")
    urls = (required("MEMORY_AI_BASE_URL"), required("EMBEDDING_AI_BASE_URL"))
    if any(urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname for url in urls):
        raise RuntimeError("Memory AI URL 必須是 HTTP URL")
    return MemorySettings(
        database_url, scope,
        required("MEMORY_AI_API_KEY"), urls[0], required("MEMORY_AI_MODEL"),
        required("EMBEDDING_AI_API_KEY"), urls[1], required("EMBEDDING_AI_MODEL"),
        env.get("EMBEDDING_AI_SERVING_MODEL", "").strip() or required("EMBEDDING_AI_MODEL"),
        dimension,
        env.get("EMBEDDING_AI_QUERY_PREFIX", RETRIEVAL_INSTRUCTION),
        env.get("EMBEDDING_AI_DOCUMENT_PREFIX", ""),
    )
