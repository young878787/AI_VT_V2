import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.memory_retriever import MemoryRetriever
from domain.memory_scope import MemoryScope
from infrastructure.memory_repository import MemoryRepository, MIN_RETRIEVAL_SIMILARITY


class _EmptyCursor:
    description = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def fetchall(self):
        return []


class _RecordingConnection:
    def __init__(self):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def transaction(self):
        return self

    async def execute(self, query, args=()):
        self.calls.append((query, args))
        return _EmptyCursor()


class MemoryRetrieverTests(unittest.IsolatedAsyncioTestCase):
    async def test_repository_query_binds_similarity_gate(self):
        connection = _RecordingConnection()
        repository = MemoryRepository(
            SimpleNamespace(connection=lambda: connection),
            MemoryScope(uuid4(), uuid4(), "test_" + uuid4().hex),
            embedding_contract="same-contract",
        )
        await repository.related_items("無關主題", [1.0] + [0.0] * 1023)
        query, args = connection.calls[-1]
        statement = query.as_string()
        self.assertEqual(statement.count("%s"), len(args))
        self.assertIn("distance <= %s", statement)
        self.assertEqual(args[-2], 1 - MIN_RETRIEVAL_SIMILARITY)
        self.assertEqual(args[-3], "same-contract")

    async def test_no_candidates_returns_empty_memory_and_profile(self):
        repository = SimpleNamespace(related_items=AsyncMock(return_value=[]))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        profile, relevant = await MemoryRetriever(repository, embedding).retrieve("無關的新問題")
        self.assertEqual((profile, relevant), ({}, ""))
        repository.related_items.assert_awaited_once()

    async def test_only_matched_profile_and_memory_are_projected(self):
        profile_id, memory_id = uuid4(), uuid4()
        rows = [
            {"id": profile_id, "group_id": profile_id, "memory_type": "profile",
             "status": "active", "subject_key": "profile.recent_interests",
             "canonical_text": "喜歡天文", "similarity": 0.9, "exact_match": False},
            {"id": memory_id, "group_id": memory_id, "memory_type": "preference",
             "status": "active", "canonical_text": "喜歡拿鐵",
             "similarity": 0.8, "exact_match": False},
        ]
        repository = SimpleNamespace(related_items=AsyncMock(return_value=rows))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        profile, relevant = await MemoryRetriever(repository, embedding).retrieve("我的喜好")
        self.assertEqual(profile, {"recent_interests": ["喜歡天文"]})
        self.assertEqual(relevant, "喜歡拿鐵")

    async def test_trace_preserves_candidate_order_and_scalar_profile_overwrite(self):
        rows = [{"id": uuid4(), "group_id": uuid4(), "memory_type": "profile", "status": "active",
                 "subject_key": "profile.communication_style", "canonical_text": text,
                 "similarity": .9, "exact_match": False} for text in ("請簡短", "請詳細")]
        repository = SimpleNamespace(related_items=AsyncMock(return_value=rows))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        with patch("services.memory_retriever.trace") as trace:
            profile, relevant = await MemoryRetriever(repository, embedding).retrieve("回覆風格", uuid4())
        self.assertEqual(profile, {"communication_style": "請詳細"})
        self.assertEqual(relevant, "")
        evidence = trace.call_args.args[1]
        self.assertEqual([str(row["id"]) for row in evidence["candidates"]], [str(row["id"]) for row in rows])
        self.assertEqual([row["projection"] for row in evidence["candidates"]], ["profile_overwritten", "selected"])
        self.assertEqual([row["id"] for row in evidence["projections"]], [str(rows[-1]["id"])])
        self.assertEqual((evidence["limit"], evidence["memory_limit"], evidence["min_similarity"]), (20, 8, .75))


if __name__ == "__main__":
    unittest.main()
