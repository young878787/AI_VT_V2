import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.memory_retriever import MemoryRetriever, build_retrieval_query
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
    def test_context_query_uses_only_bounded_user_sources(self):
        history = [dict(role="user", content="舊的私人話題"),
                   dict(role="user", content="今天替朋友挑生日蛋糕。"),
                   dict(role="assistant", content="猜測你喜歡草莓。"),
                   dict(role="user", content="今晚要早睡，避開咖啡因。")]
        query = build_retrieval_query("依照我的口味怎麼選？", history, "不要採用的摘要")
        self.assertIn("生日蛋糕", query.lexical_text)
        self.assertIn("咖啡因", query.semantic_text)
        self.assertNotIn("草莓", query.semantic_text)
        self.assertNotIn("私人", query.semantic_text)
        self.assertNotIn("摘要", query.semantic_text)
        movie = build_retrieval_query("我喜歡哪部電影？", history)
        self.assertEqual((movie.lexical_text, movie.semantic_text), ("我喜歡哪部電影？", "我喜歡哪部電影"))
        taste = build_retrieval_query("我的口味？")
        self.assertEqual((taste.lexical_text, taste.semantic_text), ("我的口味？", "使用者的口味"))
        bounded = build_retrieval_query("我的口味" + "字" * 5000,
                                        [dict(role="user", content="字" * 5000)])
        self.assertLessEqual(len(bounded.lexical_text), 4200)
        self.assertLessEqual(len(bounded.semantic_text), 4200)

    def test_semantic_query_removes_recall_wrapper_without_broadening_fact(self):
        cases = {
            "我比較偏好哪種甜點？": "使用者比較偏好哪種甜點",
            "我平常一直有玩的遊戲是什麼？": "使用者平常一直有玩的遊戲是什麼",
            "關於我的飲品偏好，你記得是什麼嗎？": "使用者的飲品偏好，是什麼",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                query = build_retrieval_query(raw)
                self.assertEqual(query.lexical_text, raw)
                self.assertEqual(query.semantic_text, expected)

    async def test_context_does_not_change_current_history_mode(self):
        repository = SimpleNamespace(related_items=AsyncMock(return_value=[]))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        await MemoryRetriever(repository, embedding).retrieve("依照我的口味怎麼選？",
            recent_dialogue=[dict(role="user", content="之前討論生日蛋糕")])
        self.assertEqual(repository.related_items.await_args.kwargs["mode"], "current")
        self.assertIn("生日蛋糕", repository.related_items.await_args.args[0])
        self.assertIn("相關使用者情境", embedding.embed.await_args.args[0])
        self.assertNotEqual(embedding.embed.await_args.args[0], repository.related_items.await_args.args[0])

    async def test_category_aliases_are_lexical_and_similarity_gate_unchanged(self):
        connection = _RecordingConnection()
        repository = MemoryRepository(SimpleNamespace(connection=lambda: connection),
            MemoryScope(uuid4(), uuid4(), "test_" + uuid4().hex), embedding_contract="same-contract")
        for query in ("我的飲品偏好", "今天挑生日蛋糕"):
            with self.subTest(query=query):
                await repository.related_items(query, [1.0] + [0.0] * 1023)
                sql, args = connection.calls[-1]
                self.assertEqual(sql.as_string().count("%s"), len(args))
                self.assertEqual(args[-2], 1 - MIN_RETRIEVAL_SIMILARITY)
                self.assertTrue(any("飲料" in str(arg) if "飲品" in query else "甜點" in str(arg) for arg in args))

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
        self.assertEqual(relevant,
                         "- fact=喜歡拿鐵（type=preference; subject_key=未標註; status=active）")

    async def test_projection_never_marks_partial_record_as_selected(self):
        rows = [{"id": uuid4(), "group_id": uuid4(), "memory_type": "project", "status": "active",
                 "subject_key": "project", "canonical_text": "事實" + str(index) + "字" * 130,
                 "similarity": .9, "exact_match": True} for index in range(8)]
        repository = SimpleNamespace(related_items=AsyncMock(return_value=rows))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        with patch("services.memory_retriever.trace") as trace:
            _, relevant = await MemoryRetriever(repository, embedding).retrieve("完整配置", uuid4())
        evidence = trace.call_args.args[1]
        selected = {projection["id"] for projection in evidence["projections"]}
        self.assertLessEqual(len(relevant), 800)
        self.assertTrue(all(projection["text"].endswith("status=active）")
                            for projection in evidence["projections"]))
        self.assertEqual(
            [candidate["projection"] for candidate in evidence["candidates"]],
            ["selected" if str(candidate["id"]) in selected else "memory_budget_exhausted"
             for candidate in evidence["candidates"]],
        )

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
