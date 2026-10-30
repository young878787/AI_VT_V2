import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.memory_retriever import MemoryRetriever, build_retrieval_query, _format_memory_projection
from domain.memory_scope import MemoryScope
from infrastructure.memory_repository import MemoryRepository, MIN_RETRIEVAL_SIMILARITY
from infrastructure.memory_repository import CHAT_RETRIEVAL_MIN_SIMILARITY, CHAT_RETRIEVAL_LIMIT


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

    async def test_time_words_do_not_exclude_current_facts(self):
        repository = SimpleNamespace(related_items=AsyncMock(return_value=[]))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        for question in ("上個月聚餐訂哪間餐廳？", "明天照我的習慣怎麼安排？", "依照我的口味怎麼選？"):
            await MemoryRetriever(repository, embedding).retrieve(question,
                recent_dialogue=[dict(role="user", content="之前討論生日蛋糕")])
            self.assertEqual(repository.related_items.await_args.kwargs["mode"], "chat")
            self.assertEqual(repository.related_items.await_args.kwargs["limit"], CHAT_RETRIEVAL_LIMIT)
        self.assertIn("生日蛋糕", repository.related_items.await_args.args[0])
        self.assertNotIn("當前對話情境", repository.related_items.await_args.args[0])
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

    async def test_alias_anchors_survive_context_truncation_without_reversing_categories(self):
        connection = _RecordingConnection()
        repository = MemoryRepository(SimpleNamespace(connection=lambda: connection),
            MemoryScope(uuid4(), uuid4(), "test_" + uuid4().hex))
        query = build_retrieval_query("依照我的喜好和今晚的安排，現在喝什麼好？",
            [dict(role="user", content="我今晚要早睡，今天暫時避開咖啡因。")])
        for text, expected, absent in (
            (query.lexical_text, {"咖啡", "coffee"}, set()),
            ("我平常喜歡COFFEE嗎？", {"咖啡", "coffee"}, set()),
            ("coffeetable", {"coffeetable"}, {"咖啡", "coffee"}),
            ("甜點", {"甜點", "dessert"}, {"蛋糕"}),
        ):
            with self.subTest(query=text):
                await repository.related_items(text, None)
                statement, args = connection.calls[-1]
                terms = next(arg for arg in args if isinstance(arg, list))
                self.assertTrue(expected <= set(terms))
                self.assertFalse(absent & set(terms))
                self.assertLessEqual(len(terms), 24)
                self.assertEqual(len(terms), len(set(terms)))
                self.assertEqual(statement.as_string().count("%s"), len(args))

    async def test_no_candidates_returns_empty_memory_and_profile(self):
        repository = SimpleNamespace(related_items=AsyncMock(return_value=[]))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        profile, relevant = await MemoryRetriever(repository, embedding).retrieve("無關的新問題")
        self.assertEqual((profile, relevant), ({}, ""))
        repository.related_items.assert_awaited_once()

    async def test_cancelled_embedding_does_not_continue_to_repository(self):
        import asyncio
        started = asyncio.Event()
        async def slow_embed(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()
        repo = SimpleNamespace(related_items=AsyncMock(return_value=[]))
        retriever = MemoryRetriever(repo, SimpleNamespace(embed=slow_embed))
        task = asyncio.create_task(retriever.retrieve("問題"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        repo.related_items.assert_not_awaited()

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
                         "- 喜歡拿鐵（status=active）")

    async def test_projection_never_marks_partial_record_as_selected(self):
        rows = [{"id": uuid4(), "group_id": uuid4(), "memory_type": "project", "status": "active",
                 "subject_key": "project", "canonical_text": "事實" + str(index) + "字" * 250,
                 "similarity": .9, "exact_match": True} for index in range(5)]
        repository = SimpleNamespace(related_items=AsyncMock(return_value=rows))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        with patch("services.memory_retriever.trace") as trace:
            _, relevant = await MemoryRetriever(repository, embedding).retrieve("完整配置", uuid4())
        evidence = trace.call_args.args[1]
        selected = {projection["id"] for projection in evidence["projections"]}
        self.assertLessEqual(len(relevant), 800)
        self.assertTrue(all(projection["text"].endswith("status=active）")
                            for projection in evidence["projections"]))
        self.assertLess(len(selected), len(rows))
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
        self.assertEqual((evidence["limit"], evidence["memory_limit"], evidence["min_similarity"]),
                         (CHAT_RETRIEVAL_LIMIT, 5, CHAT_RETRIEVAL_MIN_SIMILARITY))

    async def test_five_complete_facts_reach_single_long_term_prompt_section(self):
        from datetime import datetime, timezone
        from domain.agent_a_prompts import build_agent_a_prompt
        from domain.emotion_state import NEUTRAL_EMOTION_STATE
        from services.chat_service import build_chat_context
        facts = ["星舟助手使用 3D Avatar 顯示角色。", "星舟助手由 Emotion Engine 維護情緒。",
                 "星舟助手由 Emotion Engine 負責即時、規劃中與結束時的動作。",
                 "星舟助手的核心對話由 Dialogue Model 產生。", "星舟助手的 Recall Store 在背景運行。"]
        rows = [{"id": uuid4(), "memory_type": "project", "status": "active",
                 "subject_key": "xingzhou_virtual_assistant", "canonical_text": fact,
                 "valid_from": datetime(2026, 10, 14, 23, 50, 1, 123456, tzinfo=timezone.utc),
                 "valid_to": None, "similarity": .8, "exact_match": True} for fact in facts]
        repo = SimpleNamespace(related_items=AsyncMock(return_value=rows))
        with patch("services.memory_retriever.trace") as trace:
            profile, notes = await MemoryRetriever(repo, SimpleNamespace(embed=AsyncMock(return_value=[1.0]))).retrieve("整體架構")
        self.assertEqual(repo.related_items.await_args.kwargs["limit"], 5)
        self.assertEqual(len(trace.call_args.args[1]["projections"]), 5)
        self.assertLess(len(notes), 600)
        for noise in ("type=", "subject_key=", "None", ".123456", "fact="):
            self.assertNotIn(noise, notes)
        prompt = build_agent_a_prompt(emotion_state=NEUTRAL_EMOTION_STATE, user_profile=profile, memory_notes=notes)
        messages = build_chat_context(prompt, [], "整體架構", budget=8192)
        self.assertEqual(prompt.count("\n長期記憶：\n"), 1)
        self.assertLess(prompt.index("長期記憶："), prompt.index("使用者資料："))
        for fact in facts:
            self.assertIn(fact, messages[0]["content"])

    def test_temporal_and_uncertain_metadata_remains_without_internal_fields(self):
        from datetime import datetime, timedelta, timezone
        start = datetime(2026, 10, 14, 9, 0, tzinfo=timezone(timedelta(hours=8)))
        for status in ("active", "superseded", "expired", "archived", "conflict"):
            with self.subTest(status=status):
                text = _format_memory_projection({"canonical_text": "使用者的朋友偏好茶。",
                    "status": status, "valid_from": start, "valid_to": start + timedelta(hours=1),
                    "has_conflict": True, "pending_change": True}, "chat")
                self.assertIn("使用者的朋友偏好茶。", text)
                self.assertIn(f"status={status}", text)
                self.assertIn("valid_from=2026-10-14T09:00:00+08:00", text)
                self.assertIn("valid_to=2026-10-14T10:00:00+08:00", text)
                self.assertIn("conflict=true", text)
                self.assertIn("pending_change=true", text)

    async def test_chat_policy_keeps_owner_contract_and_retention_filter(self):
        connection = _RecordingConnection()
        scope = MemoryScope(uuid4(), uuid4(), "test_" + uuid4().hex)
        repo = MemoryRepository(SimpleNamespace(connection=lambda: connection), scope, embedding_contract="contract")
        await repo.related_items("主題", [1.0] + [0.0] * 1023, limit=3, mode="chat")
        query, args = connection.calls[-1]
        statement = query.as_string()
        self.assertEqual(statement.count("%s"), len(args))
        self.assertIn("ORDER BY distance ASC", statement)
        self.assertIn("expires_at > now()", statement)
        self.assertNotIn("valid_from > now()", statement)
        self.assertIn(scope.user_id, args)
        self.assertIn(scope.character_id, args)
        self.assertEqual(args[-3:], ("contract", 1 - CHAT_RETRIEVAL_MIN_SIMILARITY, 3))
        with self.assertRaises(ValueError):
            await repo.related_items("主題", None, mode="management", min_similarity=.5)

    async def test_future_profile_is_timestamped_memory_not_current_profile(self):
        from datetime import datetime, timedelta, timezone
        row = {"id": uuid4(), "memory_type": "profile", "status": "active",
               "subject_key": "profile.communication_style", "canonical_text": "下週開始請詳細回答",
               "valid_from": datetime.now(timezone.utc) + timedelta(days=7), "valid_to": None,
               "similarity": .8, "exact_match": True}
        repo = SimpleNamespace(related_items=AsyncMock(return_value=[row]))
        profile, notes = await MemoryRetriever(repo, SimpleNamespace(embed=AsyncMock(return_value=[1.0]))).retrieve("回覆習慣")
        self.assertEqual(profile, {})
        self.assertIn(row["canonical_text"], notes)
        self.assertIn("valid_from=", notes)


if __name__ == "__main__":
    unittest.main()
