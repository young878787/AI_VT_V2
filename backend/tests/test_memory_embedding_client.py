"""Tests for configurable OpenAI-compatible embedding input contracts."""

import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.memory_settings import load_memory_settings
from infrastructure.memory_embedding_client import MemoryEmbeddingClient


class MemoryEmbeddingClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_served_alias_and_query_document_prefixes_are_used(self):
        settings = load_memory_settings({
            "MEMORY_DATABASE_URL": "postgresql://localhost/memory",
            "MEMORY_DEFAULT_USER_ID": str(uuid4()),
            "MEMORY_DEFAULT_CHARACTER_ID": str(uuid4()),
            "MEMORY_DATABASE_SCHEMA": "ai_vt_memory",
            "MEMORY_AI_API_KEY": "test-key",
            "MEMORY_AI_BASE_URL": "https://memory.example/v1",
            "MEMORY_AI_MODEL": "test-memory-model",
            "EMBEDDING_AI_API_KEY": "local-vllm",
            "EMBEDDING_AI_BASE_URL": "http://127.0.0.1:18000/v1",
            "EMBEDDING_AI_MODEL": "jinaai/jina-embeddings-v5-text-small-retrieval",
            "EMBEDDING_AI_SERVING_MODEL": "jina-retrieval",
            "EMBEDDING_AI_DIMENSION": "1024",
            "EMBEDDING_AI_QUERY_PREFIX": "Query: ",
            "EMBEDDING_AI_DOCUMENT_PREFIX": "Document: ",
        })
        response = SimpleNamespace(model="jina-retrieval", data=[SimpleNamespace(embedding=[0.0] * 1023 + [1.0])])

        with patch("infrastructure.memory_embedding_client.AsyncOpenAI") as openai_factory:
            openai = openai_factory.return_value
            openai.embeddings.create = AsyncMock(return_value=response)
            client = MemoryEmbeddingClient(settings)
            client.diagnostic_writer = AsyncMock()

            await client.embed("find my preferences", purpose="retrieval_query", event_id=uuid4())
            await client.embed("I prefer tea", purpose="memory_document", event_id=uuid4())

        calls = openai.embeddings.create.await_args_list
        self.assertEqual(calls[0].kwargs, {
            "model": "jina-retrieval", "input": "Query: find my preferences",
        })
        self.assertEqual(calls[1].kwargs, {
            "model": "jina-retrieval", "input": "Document: I prefer tea",
        })
        diagnostics = [call.args[1] for call in client.diagnostic_writer.await_args_list]
        self.assertEqual([item["model"] for item in diagnostics], [
            "jinaai/jina-embeddings-v5-text-small-retrieval",
            "jinaai/jina-embeddings-v5-text-small-retrieval",
        ])
        self.assertEqual([item["serving_model"] for item in diagnostics], ["jina-retrieval", "jina-retrieval"])
        self.assertEqual([item["served_model"] for item in diagnostics], ["jina-retrieval", "jina-retrieval"])


if __name__ == "__main__":
    unittest.main()
