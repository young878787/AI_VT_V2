"""Qwen3 1024 維 embedding 的輸入與輸出契約。"""

import math

from domain.memory_settings import EMBEDDING_DIMENSION, RETRIEVAL_INSTRUCTION


EMBEDDING_PURPOSES = {
    "retrieval_query": "query",
    "memory_match_query": "query",
    "context_document": "document",
    "memory_document": "document",
}


def query_document(text: str) -> str:
    return RETRIEVAL_INSTRUCTION + text


def format_embedding_input(text: str, *, query: bool, query_prefix: str, document_prefix: str) -> str:
    return (query_prefix if query else document_prefix) + text


def normalize_embedding(values: list[float], dimension: int = EMBEDDING_DIMENSION) -> list[float]:
    if not isinstance(values, list) or len(values) != dimension:
        raise ValueError(f"Embedding 維度必須是 {dimension}")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise ValueError("Embedding 包含無效值")
    norm = math.hypot(*values)
    if norm == 0 or not math.isfinite(norm):
        raise ValueError("Embedding 範數無效")
    return [float(value) / norm for value in values]
