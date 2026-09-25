"""獨立的 OpenAI-compatible embedding 路線。"""

from openai import AsyncOpenAI

from domain.memory_embedding import normalize_embedding, query_document
from domain.memory_settings import EMBEDDING_MODEL, MemorySettings


class MemoryEmbeddingClient:
    def __init__(self, settings: MemorySettings) -> None:
        self.client = AsyncOpenAI(base_url=settings.embedding_base_url, api_key=settings.embedding_api_key)

    async def embed(self, text: str, *, query: bool = False) -> list[float]:
        response = await self.client.embeddings.create(
            model=EMBEDDING_MODEL, input=query_document(text) if query else text,
        )
        if len(response.data) != 1:
            raise ValueError("Embedding API 回傳筆數無效")
        return normalize_embedding(response.data[0].embedding)
