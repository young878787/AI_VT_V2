"""Qwen3 1024 維 embedding 的輸入與輸出契約。"""

import math

from domain.memory_settings import EMBEDDING_DIMENSION, RETRIEVAL_INSTRUCTION


def query_document(text: str) -> str:
    return RETRIEVAL_INSTRUCTION + text


def normalize_embedding(values: list[float]) -> list[float]:
    if not isinstance(values, list) or len(values) != EMBEDDING_DIMENSION:
        raise ValueError("Embedding 維度必須是 1024")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
        raise ValueError("Embedding 包含無效值")
    norm = math.hypot(*values)
    if norm == 0 or not math.isfinite(norm):
        raise ValueError("Embedding 範數無效")
    return [float(value) / norm for value in values]
