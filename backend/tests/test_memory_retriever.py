import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.memory.retriever import (MEMORY_CHAR_BUDGET, MemoryRetriever, RecallJudge, build_retrieval_query,
                                       _format_memory_projection)
from domain.jev_questions import build_recall_questions, question_hash
from domain.memory.scope import MemoryScope
from infrastructure.memory.repository import MemoryRepository, MIN_RETRIEVAL_SIMILARITY
from infrastructure.memory.repository import (CHAT_CARD_CANDIDATE_LIMIT, CHAT_CARD_INJECT_LIMIT, CHAT_CARD_MIN_SCORE,
                                              CHAT_RETRIEVAL_MIN_SIMILARITY)


def card_rows(rank, texts, *, score=.8, routes=("item",), domain="preference", title=None, summary=None,
              stale=True, mood=None, status="active", memory_type="preference", card_id=None):
    """locate_cards 的回傳形狀：每筆要點一列並帶所屬卡片欄位。"""
    card_id = card_id or uuid4()
    return [{"id": uuid4(), "group_id": uuid4(), "memory_type": memory_type, "status": status,
             "canonical_text": text, "subject_key": None, "similarity": score, "card_id": card_id,
             "card_domain": domain, "card_title": title or f"卡片{rank}", "card_summary": summary,
             "card_summary_stale": stale, "card_mood": mood, "card_active_count": len(texts),
             "card_score": score, "card_routes": list(routes), "card_rank": rank} for text in texts]


def judge_with(choices, confidence=.9):
    async def ask(context, questions):
        return {key: {"choice": choice, "confidence": confidence} for key, choice in zip(questions, choices)}
    return RecallJudge(ask=AsyncMock(side_effect=ask))


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
        repository = SimpleNamespace(locate_cards=AsyncMock(return_value=[]))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        for question in ("上個月聚餐訂哪間餐廳？", "明天照我的習慣怎麼安排？", "依照我的口味怎麼選？"):
            await MemoryRetriever(repository, embedding).retrieve(question,
                recent_dialogue=[dict(role="user", content="之前討論生日蛋糕")])
            self.assertEqual(repository.locate_cards.await_args.kwargs["limit"], CHAT_CARD_CANDIDATE_LIMIT)
            self.assertEqual(repository.locate_cards.await_args.kwargs["min_score"], CHAT_CARD_MIN_SCORE)
        self.assertIn("生日蛋糕", repository.locate_cards.await_args.args[0])
        self.assertNotIn("當前對話情境", repository.locate_cards.await_args.args[0])
        self.assertIn("相關使用者情境", embedding.embed.await_args.args[0])
        self.assertNotEqual(embedding.embed.await_args.args[0], repository.locate_cards.await_args.args[0])

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
        repository = SimpleNamespace(locate_cards=AsyncMock(return_value=[]))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        judge = judge_with([])
        profile, relevant = await MemoryRetriever(repository, embedding).retrieve("無關的新問題", judge=judge)
        self.assertEqual((profile, relevant), ({}, ""))
        repository.locate_cards.assert_awaited_once()
        judge.ask.assert_not_awaited()  # 沒有候選卡時不呼叫召回判斷

    async def test_cancelled_embedding_does_not_continue_to_repository(self):
        import asyncio
        started = asyncio.Event()
        async def slow_embed(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()
        repo = SimpleNamespace(locate_cards=AsyncMock(return_value=[]))
        retriever = MemoryRetriever(repo, SimpleNamespace(embed=slow_embed))
        task = asyncio.create_task(retriever.retrieve("問題"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        repo.locate_cards.assert_not_awaited()

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
        repository = SimpleNamespace(locate_cards=AsyncMock(return_value=rows))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        profile, relevant = await MemoryRetriever(repository, embedding).retrieve("我的喜好")
        self.assertEqual(profile, {"recent_interests": ["喜歡天文"]})
        # 未歸卡要點歸在「其他」標題下。
        self.assertEqual(relevant,
                         "【其他／未分類】\n- 喜歡拿鐵（status=active）")

    async def test_projection_never_marks_partial_record_as_selected(self):
        rows = card_rows(1, ["事實" + str(index) + "字" * 250 for index in range(8)], score=.9,
                         memory_type="project")
        repository = SimpleNamespace(locate_cards=AsyncMock(return_value=rows))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        with patch("services.memory.retriever.trace") as trace:
            _, relevant = await MemoryRetriever(repository, embedding).retrieve("完整配置", uuid4())
        evidence = trace.call_args.args[1]
        selected = {projection["id"] for projection in evidence["projections"]}
        self.assertLessEqual(len(relevant), MEMORY_CHAR_BUDGET)
        self.assertTrue(all(projection["text"].endswith("status=active）")
                            for projection in evidence["projections"]))
        self.assertLess(len(selected), len(rows))
        self.assertIn(f"（此卡另有 {len(rows) - len(selected)} 筆未列出）", relevant)
        self.assertEqual(evidence["candidate_cards"][0]["injection"], "partial")
        self.assertEqual(
            [candidate["projection"] for candidate in evidence["candidates"]],
            ["selected" if str(candidate["id"]) in selected else "not_projected"
             for candidate in evidence["candidates"]],
        )

    async def test_trace_preserves_candidate_order_and_scalar_profile_overwrite(self):
        rows = [{"id": uuid4(), "group_id": uuid4(), "memory_type": "profile", "status": "active",
                 "subject_key": "profile.communication_style", "canonical_text": text,
                 "similarity": .9, "exact_match": False} for text in ("請簡短", "請詳細")]
        repository = SimpleNamespace(locate_cards=AsyncMock(return_value=rows))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        with patch("services.memory.retriever.trace") as trace:
            profile, relevant = await MemoryRetriever(repository, embedding).retrieve("回覆風格", uuid4())
        self.assertEqual(profile, {"communication_style": "請詳細"})
        self.assertEqual(relevant, "")
        evidence = trace.call_args.args[1]
        self.assertEqual([str(row["id"]) for row in evidence["candidates"]], [str(row["id"]) for row in rows])
        self.assertEqual([row["projection"] for row in evidence["candidates"]], ["not_projected", "selected"])
        self.assertEqual([row["id"] for row in evidence["projections"]], [str(rows[-1]["id"])])
        self.assertEqual((evidence["limit"], evidence["inject_limit"], evidence["min_score"],
                          evidence["fallback_min_similarity"]),
                         (CHAT_CARD_CANDIDATE_LIMIT, CHAT_CARD_INJECT_LIMIT, CHAT_CARD_MIN_SCORE,
                          CHAT_RETRIEVAL_MIN_SIMILARITY))

    async def test_five_complete_facts_reach_single_long_term_prompt_section(self):
        from datetime import datetime, timezone
        from domain.agent_a_prompts import build_agent_a_prompt
        from domain.emotion_state import NEUTRAL_EMOTION_STATE
        from services.chat.service import build_chat_context
        facts = ["星舟助手使用 3D Avatar 顯示角色。", "星舟助手由 Emotion Engine 維護情緒。",
                 "星舟助手由 Emotion Engine 負責即時、規劃中與結束時的動作。",
                 "星舟助手的核心對話由 Dialogue Model 產生。", "星舟助手的 Recall Store 在背景運行。"]
        rows = [{"id": uuid4(), "memory_type": "project", "status": "active",
                 "subject_key": "xingzhou_virtual_assistant", "canonical_text": fact,
                 "valid_from": datetime(2026, 10, 14, 23, 50, 1, 123456, tzinfo=timezone.utc),
                 "valid_to": None, "similarity": .8, "exact_match": True} for fact in facts]
        repo = SimpleNamespace(locate_cards=AsyncMock(return_value=rows))
        with patch("services.memory.retriever.trace") as trace:
            profile, notes = await MemoryRetriever(repo, SimpleNamespace(embed=AsyncMock(return_value=[1.0]))).retrieve("整體架構")
        self.assertEqual(repo.locate_cards.await_args.kwargs["limit"], CHAT_CARD_CANDIDATE_LIMIT)
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
        previous = uuid4()
        await repo.locate_cards("小岑喜歡什麼", [1.0] + [0.0] * 1023, limit=3, previous_card_ids=(previous,))
        query, params = connection.calls[-1]
        statement = query.as_string()
        self.assertIn("expires_at > now()", statement)
        self.assertNotIn("valid_from > now()", statement)
        # 四路與連結／session 補卡都限定 owner；每種向量各自比對 contract。
        for alias in ("i", "s", "c", "e1", "m"):
            self.assertIn(f"{alias}.user_id = %(user)s AND {alias}.character_id = %(character)s", statement)
        for alias in ("i", "s", "c"):
            self.assertIn(f"{alias}.embedding_contract = %(contract)s", statement)
        self.assertNotIn("keywords", statement)  # 詞面 bigram 不再是召回資格
        self.assertEqual((params["user"], params["character"], params["contract"]),
                         (scope.user_id, scope.character_id, "contract"))
        self.assertEqual((params["min_score"], params["limit"], params["previous"], params["name_text"]),
                         (CHAT_CARD_MIN_SCORE, 3, [previous], "小岑喜歡什麼"))
        with self.assertRaises(ValueError):
            await repo.related_items("主題", None, mode="chat")

    async def test_future_profile_is_timestamped_memory_not_current_profile(self):
        from datetime import datetime, timedelta, timezone
        row = {"id": uuid4(), "memory_type": "profile", "status": "active",
               "subject_key": "profile.communication_style", "canonical_text": "下週開始請詳細回答",
               "valid_from": datetime.now(timezone.utc) + timedelta(days=7), "valid_to": None,
               "similarity": .8, "exact_match": True}
        repo = SimpleNamespace(locate_cards=AsyncMock(return_value=[row]))
        profile, notes = await MemoryRetriever(repo, SimpleNamespace(embed=AsyncMock(return_value=[1.0]))).retrieve("回覆習慣")
        self.assertEqual(profile, {})
        self.assertIn(row["canonical_text"], notes)
        self.assertIn("valid_from=", notes)


class CardRecallTests(unittest.IsolatedAsyncioTestCase):
    """A-04：候選卡、JEV 判斷與分組投影。"""

    async def recall(self, rows, judge=None, text="問題"):
        repository = SimpleNamespace(locate_cards=AsyncMock(return_value=rows))
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0]))
        with patch("services.memory.retriever.trace") as trace:
            profile, memory = await MemoryRetriever(repository, embedding).retrieve(text, uuid4(), judge=judge)
        return profile, memory, trace.call_args.args[1], repository

    async def test_key_cards_precede_assist_and_none_is_not_injected(self):
        first = card_rows(1, ["使用者喜歡烏龍茶"], title="茶飲")
        second = card_rows(2, ["使用者的朋友小岑喜歡巧克力"], domain="relation", title="小岑", mood="pleased")
        third = card_rows(3, ["使用者養了一隻貓"], domain="daily", title="寵物")
        judge = judge_with(["assist", "key", "none"])
        _, memory, evidence, _ = await self.recall(first + second + third, judge)
        self.assertEqual(memory.splitlines(), [
            "【人際關係／小岑】（露西亞當時的心情：開心）", "- 使用者的朋友小岑喜歡巧克力（status=active）",
            "【喜好與興趣／茶飲】", "- 使用者喜歡烏龍茶（status=active）"])
        self.assertNotIn("貓", memory)
        self.assertEqual([card["selection"] for card in evidence["candidate_cards"]], ["assist", "key", "none"])
        self.assertEqual(judge.selected_card_ids, [first[0]["card_id"], second[0]["card_id"]])
        self.assertFalse(evidence["fallback"])

    async def test_projection_offsets_point_to_each_verbatim_fact(self):
        rows = card_rows(1, ["事實甲", "事實乙"]) + card_rows(2, ["事實丙"], domain="event")
        _, memory, evidence, _ = await self.recall(rows, judge_with(["key", "assist"]))
        memories = [item for item in evidence["projections"] if item["destination"] == "memory"]
        self.assertEqual(len(memories), 3)
        for item in memories:
            self.assertEqual(memory[item["offset"]:item["offset"] + len(item["text"])], item["text"])
            self.assertIn(rows[[str(row["id"]) for row in rows].index(item["id"])]["canonical_text"], item["text"])

    async def test_inject_limit_and_char_budget(self):
        rows = [row for rank in range(1, 8) for row in card_rows(rank, [f"第{rank}張卡的要點"])]
        _, memory, evidence, _ = await self.recall(rows, judge_with(["key"] * 7))
        self.assertEqual(memory.count("【"), CHAT_CARD_INJECT_LIMIT)
        self.assertEqual(evidence["timing"]["injected_card_count"], CHAT_CARD_INJECT_LIMIT)
        self.assertEqual([card["injection"] for card in evidence["candidate_cards"]][-2:],
                         ["inject_limit", "inject_limit"])
        long_rows = [row for rank in range(1, 4) for row in card_rows(rank, ["長" * 700])]
        _, memory, evidence, _ = await self.recall(long_rows, judge_with(["key"] * 3))
        self.assertLessEqual(len(memory), MEMORY_CHAR_BUDGET)
        self.assertEqual(memory.count("【"), 2)
        self.assertEqual(evidence["candidate_cards"][2]["injection"], "memory_budget_exhausted")

    async def test_old_versions_are_marked(self):
        from datetime import datetime, timezone
        card_id = uuid4()
        rows = (card_rows(1, ["使用者現在喝拿鐵"], card_id=card_id)
                + card_rows(1, ["使用者以前喝美式"], card_id=card_id, status="superseded"))
        rows[1]["valid_to"] = datetime(2026, 10, 1, tzinfo=timezone.utc)
        _, memory, _, _ = await self.recall(rows, judge_with(["key"]))
        self.assertIn("- 使用者以前喝美式（status=superseded; valid_to=2026-10-01T00:00:00+00:00）", memory)
        self.assertLess(memory.index("拿鐵"), memory.index("美式"))

    async def test_jev_sees_summary_or_first_points_when_stale(self):
        fresh = card_rows(1, ["要點一", "要點二", "要點三"], summary="新鮮概要", stale=False)
        stale = card_rows(2, ["舊要點一", "舊要點二", "舊要點三"], summary="過期概要", stale=True)
        judge = judge_with(["key", "key"])
        await self.recall(fresh + stale, judge)
        context, questions = judge.ask.await_args.args
        cards = context["candidate_cards"]
        self.assertEqual(cards[0]["summary"], "新鮮概要")
        self.assertNotIn("points", cards[0])
        self.assertEqual(cards[1]["points"], ["舊要點一", "舊要點二"])
        self.assertNotIn("summary", cards[1])
        self.assertEqual(set(context), {"current_user_input", "recent_dialogue", "candidate_cards"})
        self.assertEqual(list(questions), ["card_1", "card_2"])
        # 題目不含卡片內容：hash 只隨候選數變動。
        self.assertEqual(question_hash(questions), question_hash(build_recall_questions(2)))
        self.assertNotIn("舊要點一", str(questions))
        self.assertNotIn("新鮮概要", str(questions))

    async def test_jev_failure_falls_back_to_score_or_name_hit(self):
        strong = card_rows(1, ["高分要點"], score=.6)
        named = card_rows(2, ["名稱命中要點"], score=.3, routes=("item", "name"))
        weak = card_rows(3, ["低分要點"], score=.4)
        judge = RecallJudge(ask=AsyncMock(return_value=None))
        _, memory, evidence, _ = await self.recall(strong + named + weak, judge)
        self.assertIn("高分要點", memory)
        self.assertIn("名稱命中要點", memory)
        self.assertNotIn("低分要點", memory)
        self.assertTrue(evidence["fallback"])
        self.assertEqual(evidence["recall_jev"]["error"], "jev_call_failed")
        raising = RecallJudge(ask=AsyncMock(side_effect=RuntimeError("timeout")))
        _, memory, _, _ = await self.recall(strong + weak, raising)
        self.assertEqual(memory.splitlines()[1:], ["- 高分要點（status=active）"])

    async def test_low_confidence_or_invalid_answer_is_not_injected(self):
        rows = card_rows(1, ["低信心要點"]) + card_rows(2, ["無效答案要點"])

        async def ask(context, questions):
            return {"card_1": {"choice": "key", "confidence": .3}, "card_2": {"choice": "maybe", "confidence": .9}}
        _, memory, evidence, _ = await self.recall(rows, RecallJudge(ask=ask))
        self.assertEqual(memory, "")
        self.assertEqual([card["judge_reason"] for card in evidence["candidate_cards"]],
                         ["low_confidence", "invalid_choice"])

    async def test_profile_comes_only_from_selected_cards(self):
        chosen = card_rows(1, ["喜歡天文"], memory_type="profile")
        chosen[0]["subject_key"] = "profile.recent_interests"
        dropped = card_rows(2, ["喜歡登山"], memory_type="profile")
        dropped[0]["subject_key"] = "profile.recent_interests"
        profile, memory, _, _ = await self.recall(chosen + dropped, judge_with(["key", "none"]))
        self.assertEqual(profile, {"recent_interests": ["喜歡天文"]})
        self.assertEqual(memory, "")

    async def test_previous_cards_are_passed_and_timing_recorded(self):
        previous = uuid4()
        judge = judge_with(["key"])
        judge.previous_card_ids = (previous,)
        _, _, evidence, repository = await self.recall(card_rows(1, ["要點"]), judge)
        self.assertEqual(repository.locate_cards.await_args.kwargs["previous_card_ids"], (previous,))
        timing = evidence["timing"]
        for key in ("embedding_ms", "locate_ms", "jev_ms", "project_ms", "total_ms"):
            self.assertIsNotNone(timing[key], key)
        self.assertEqual((timing["candidate_card_count"], timing["injected_card_count"]), (1, 1))

    async def test_embedding_failure_returns_empty_projection(self):
        repository = SimpleNamespace(locate_cards=AsyncMock(return_value=card_rows(1, ["要點"])))
        embedding = SimpleNamespace(embed=AsyncMock(side_effect=RuntimeError("offline")))
        judge = judge_with(["key"])
        with patch("services.memory.retriever.trace") as trace:
            result = await MemoryRetriever(repository, embedding).retrieve("問題", uuid4(), judge=judge)
        self.assertEqual(result, ({}, ""))
        repository.locate_cards.assert_not_awaited()
        judge.ask.assert_not_awaited()
        self.assertEqual(trace.call_args.args[1]["errors"], [{"stage": "embedding", "error": "RuntimeError"}])


if __name__ == "__main__":
    unittest.main()
