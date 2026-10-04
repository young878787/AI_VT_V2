"""使用專用 DB 與每個測試獨立的 schema 驗證 Memory transaction。"""

import asyncio
import math
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from psycopg import sql

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
load_dotenv(BACKEND_ROOT.parent / ".env", override=False)

from domain.memory_scope import MemoryScope, conversation_id, message_id
from domain.memory_routing import MemoryRouting
from domain.memory_source import MemoryEventConflict, MemoryEventReplay, build_user_message
from infrastructure.memory_database import check_schema, make_pool
from infrastructure.memory_repository import MemoryRepository
from infrastructure.memory_store import load_session_messages, save_session_messages
from services.memory_db_manager import MemoryDBManager
from services.memory_import import read_legacy_entries
from services.memory_retriever import MemoryRetriever
from services.memory_worker import MemoryWorker
from tools.chat_test_cli import MemoryRunStore, wait_memory_job


TEST_URL = os.getenv("MEMORY_TEST_DATABASE_URL", "")
PRODUCTION_URL = os.getenv("MEMORY_DATABASE_URL", "")
EMBEDDING_MODEL = "jinaai/jina-embeddings-v5-text-small-retrieval"
EMBEDDING_CONTRACT = "jina-v5-test-contract"
VALID_TEST_DATABASE = (
    bool(TEST_URL and PRODUCTION_URL)
    and make_url(TEST_URL).database != make_url(PRODUCTION_URL).database
)
VECTOR = [1.0] + [0.0] * 1023


@unittest.skipUnless(VALID_TEST_DATABASE, "需要與正式 DB 不同的 MEMORY_TEST_DATABASE_URL")
class MemoryDatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scope = MemoryScope(uuid4(), uuid4(), "test_" + uuid4().hex)
        self.engine = create_engine(make_url(TEST_URL).set(drivername="postgresql+psycopg"))
        with self.engine.begin() as connection:
            config = Config(str(BACKEND_ROOT / "alembic.ini"))
            config.attributes.update(connection=connection, schema=self.scope.schema_name)
            command.upgrade(config, "head")
        self.pool = await make_pool(TEST_URL)
        await check_schema(self.pool, self.scope)
        self.repo = MemoryRepository(self.pool, self.scope, EMBEDDING_MODEL, EMBEDDING_CONTRACT)
        self.manager = MemoryDBManager(
            self.pool, self.scope, "test-model", EMBEDDING_MODEL, EMBEDDING_CONTRACT,
        )

    async def asyncTearDown(self):
        await self.pool.close()
        with self.engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{self.scope.schema_name}" CASCADE')
        self.engine.dispose()

    async def _job(self, turn, text="請記住我喜歡茶"):
        event_id = await self.repo.accept("session", turn)
        await self.repo.route(event_id, MemoryRouting(None, confidence=0.9), text, [])
        return await self.repo.claim()

    async def _apply(self, job, decisions, targets, embeddings, context_ids=()):
        applied = []
        for decision in decisions:
            item = {**decision, "source_ids": decision.get("source_ids", [str(job["id"])])}
            if item["action"] in {"CREATE", "SUPERSEDE", "CONTRADICT"}:
                item.setdefault("importance", .7)
                item.setdefault("confidence", .8)
                item.setdefault("retention_class", "normal")
            if item["action"] == "FORGET":
                from domain.memory_routing import forget_scope
                item["forget_scope"] = forget_scope(job["source_text"])
            applied.append(item)
        snapshots = {row["id"]: row for row in await self.repo.read_memories(targets)}
        return await self.manager.apply(job, applied, targets, embeddings, context_ids, target_snapshots=snapshots)

    async def _create(self, turn="turn1"):
        job = await self._job(turn)
        decision = {
            "action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
            "target_memory_ids": [], "reason": "explicit user statement",
            "importance": 0.75, "confidence": 0.8, "retention_class": "normal",
        }
        self.assertTrue(await self._apply(job, [decision], set(), {0: VECTOR}))
        items = await self.repo.related_items("茶", VECTOR)
        self.assertEqual(len(items), 1)
        return items[0]

    async def test_none_buffer_and_create_owner_isolation(self):
        none_id = await self.repo.accept("session", "none")
        self.assertTrue(await self.repo.route(none_id, MemoryRouting("none", confidence=0.7), "private text", []))
        buffer_id = await self.repo.accept("session", "buffer")
        self.assertTrue(await self.repo.route(
            buffer_id, MemoryRouting("needs_context", confidence=0.9), "可能喜歡茶", [],
        ))
        self.assertIsNone(await self.repo.claim())
        item = await self._create()
        buffers = await self.repo.context_jobs({"id": uuid4(), "conversation_id": conversation_id("session"), "generation": 0})
        self.assertEqual(len(buffers), 1)
        other = MemoryRepository(self.pool, MemoryScope(uuid4(), self.scope.character_id, self.scope.schema_name))
        self.assertEqual(await other.accept("session", "turn1"), await self.repo.accept("session", "turn1"))
        self.assertEqual(await other.related_items("茶", VECTOR), [])
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                row = await (await connection.execute("SELECT (SELECT raw_text FROM memory_sources WHERE id = memory_jobs.id), recent_dialogue FROM memory_jobs WHERE id = %s", (none_id,))).fetchone()
                self.assertEqual(row, (None, None))
        self.assertEqual(item["status"], "active")

        # 單純讀取待補內容不會消耗來源。
        self.assertEqual(len(await self.repo.context_jobs(
            {"id": uuid4(), "conversation_id": conversation_id("session"), "generation": 0})), 1)

    async def test_resending_same_event_does_not_reopen_route_or_duplicate_sources(self):
        event = await self.repo.accept("session", "same-event")
        self.assertTrue(await self.repo.route(
            event, MemoryRouting(None), "我喜歡茶", [],
        ))
        self.assertEqual(await self.repo.accept("session", "same-event"), event)
        self.assertFalse(await self.repo.route(
            event, MemoryRouting(None), "我喜歡茶", [],
        ))
        self.assertEqual(
            (await self._query("SELECT count(*) FROM memory_sources WHERE id = %s", (event,)))[0][0],
            1,
        )

    async def test_no_store_policy_overrides_incorrect_repository_route(self):
        event = await self.repo.accept("session", "no-store-wrong-route")
        self.assertTrue(await self.repo.route(
            event, MemoryRouting(None), "甲" * 4001 + "不要記住這件事", [], finalized=False,
        ))
        self.assertEqual(await self._query(
            "SELECT route, status, instruction, route_finalized, recent_dialogue FROM memory_jobs WHERE id = %s",
            (event,),
        ), [("none", "ignored", "no_store", True, None)])
        self.assertEqual(await self._query(
            "SELECT count(*) FROM memory_sources WHERE id = %s", (event,),
        ), [(0,)])
        self.assertIsNone(await self.repo.claim())

    async def test_cross_connection_replay_requires_the_same_persisted_content(self):
        event, _ = await self.repo.accept_event("session", "cross-connection", "我喜歡茶")
        self.assertTrue(await self.repo.route(event, MemoryRouting(None), "我喜歡茶", []))
        with self.assertRaises(MemoryEventReplay):
            await self.repo.accept_event("session", "cross-connection", "我喜歡茶")
        with self.assertRaises(MemoryEventConflict):
            await self.repo.accept_event("session", "cross-connection", "我喜歡咖啡")
        self.assertEqual(
            (await self._query("SELECT raw_text FROM memory_sources WHERE id = %s", (event,)))[0][0],
            "我喜歡茶",
        )

    async def test_reloaded_history_keeps_source_time_and_excludes_no_store_tail(self):
        allowed_text = "茶" * 600
        forbidden_text = "甲" * 4001 + "不要記住這件事"
        history = []
        original_time = datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        for turn, content in (("history-allowed", allowed_text), ("history-forbidden", forbidden_text)):
            event, generation = await self.repo.accept_event("session", turn, content)
            await self.repo.route(event, MemoryRouting("none"), content, [])
            history.append(build_user_message(
                "session", turn, content, timestamp=original_time.timestamp(), generation=generation,
            ))
        with tempfile.TemporaryDirectory() as directory, patch(
            "infrastructure.memory_store.CHAT_SESSION_DIR", directory
        ):
            save_session_messages("session", history)
            history = load_session_messages("session")
        current = await self.repo.accept("session", "history-current")
        self.assertTrue(await self.repo.route(current, MemoryRouting(None), "本輪新內容", history))
        self.assertEqual(await self._query(
            "SELECT message_id, raw_text, occurred_at FROM memory_sources WHERE id = %s",
            (message_id("session", "history-allowed"),),
        ), [(message_id("session", "history-allowed"), allowed_text, original_time)])
        self.assertEqual(await self._query(
            "SELECT count(*) FROM memory_sources WHERE id = %s",
            (message_id("session", "history-forbidden"),),
        ), [(0,)])

    async def test_reset_generation_rejects_sources_retained_by_another_connection(self):
        old_text = "請記住我喜歡茶"
        old = await self.repo.accept("session", "before-reset")
        self.assertTrue(await self.repo.route(old, MemoryRouting(None), old_text, []))
        stale_message = build_user_message(
            "session", "before-reset", old_text, timestamp=1_700_000_000, generation=0,
        )

        await self.repo.reset()
        current = await self.repo.accept("session", "after-reset")
        self.assertTrue(await self.repo.route(
            current, MemoryRouting(None), "本輪新內容", [stale_message],
        ))
        self.assertEqual(
            (await self._query("SELECT count(*) FROM memory_sources WHERE id = %s", (old,)))[0][0],
            0,
        )

    async def test_forget_cancelled_source_cannot_be_reintroduced_from_session_history(self):
        old_text = "請記住我喜歡茶"
        item = await self._create("forgotten-source")
        old = message_id("session", "forgotten-source")
        stale_message = build_user_message(
            "session", "forgotten-source", old_text, timestamp=1_700_000_000, generation=0,
        )
        forget = await self._job("forget-source", "忘記我的茶偏好")
        await self._apply(
            forget,
            [{"action": "FORGET", "target_memory_ids": [str(item["id"])], "reason": "request"}],
            {item["id"]},
            {},
        )

        current = await self.repo.accept("session", "after-forget")
        self.assertTrue(await self.repo.route(
            current, MemoryRouting(None), "本輪新內容", [stale_message],
        ))
        self.assertEqual(
            (await self._query("SELECT count(*) FROM memory_sources WHERE id = %s", (old,)))[0][0],
            0,
        )

    async def test_configured_embedding_model_is_saved(self):
        item = await self._create()
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                model = await (await connection.execute(
                    "SELECT embedding_model, embedding_contract FROM memory_items WHERE id = %s", (item["id"],),
                )).fetchone()
        self.assertEqual(model, (EMBEDDING_MODEL, EMBEDDING_CONTRACT))

    async def test_vector_search_ignores_a_different_embedding_contract(self):
        item = await self._create()
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    "UPDATE memory_items SET embedding_contract = 'legacy-contract' WHERE id = %s",
                    (item["id"],),
                )
        matches = await self.repo.related_items("unrelated terminology", VECTOR)
        self.assertEqual(matches, [])

    async def test_chat_retrieval_requires_similarity_or_specific_lexical_match(self):
        item = await self._create()
        unrelated = [0.0, 1.0] + [0.0] * 1022
        below = [0.74, math.sqrt(1 - 0.74 ** 2)] + [0.0] * 1022
        above = [0.76, math.sqrt(1 - 0.76 ** 2)] + [0.0] * 1022
        self.assertEqual(await self.repo.related_items("無關主題", unrelated), [])
        self.assertEqual(await self.repo.related_items("無關主題", below), [])
        matches = await self.repo.related_items("無關主題", above)
        self.assertEqual([row["id"] for row in matches], [item["id"]])
        self.assertAlmostEqual(matches[0]["similarity"], 0.76, places=5)
        self.assertFalse(matches[0]["exact_match"])
        lexical = await self.repo.related_items("喜歡茶", None)
        self.assertEqual([row["id"] for row in lexical], [item["id"]])
        self.assertIsNone(lexical[0]["similarity"])

    async def test_rejected_retrieval_trace_keeps_gate_contract_and_owner(self):
        import json
        from core.prompt_logger import trace_event
        item = await self._create()
        below = [.5, math.sqrt(.75)] + [0.0] * 1022
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "AI_VT_TEST_MODE": "true", "AI_VT_MEMORY_DIR": directory,
        }):
            token = trace_event.set(uuid4())
            try:
                self.assertEqual(await self.repo.related_items("無關主題", below), [])
                await self._query("UPDATE memory_items SET embedding_contract = 'old-contract' WHERE id = %s", (item["id"],))
                self.assertEqual(await self.repo.related_items("無關主題", VECTOR), [])
                other = MemoryRepository(self.pool, MemoryScope(uuid4(), self.scope.character_id,
                    self.scope.schema_name), EMBEDDING_MODEL, EMBEDDING_CONTRACT)
                self.assertEqual(await other.related_items("無關主題", VECTOR), [])
            finally:
                trace_event.reset(token)
            traces = [json.loads(line) for line in (pathlib.Path(directory) / "trace.jsonl").read_text().splitlines()]
        rejected = traces[0]["excluded_candidates"]
        self.assertEqual([row["id"] for row in rejected], [str(item["id"])])
        self.assertAlmostEqual(rejected[0]["similarity"], .5)
        self.assertFalse(rejected[0]["exact_match"])
        self.assertEqual(rejected[0]["rejection_reason"], "below_similarity_threshold")
        self.assertEqual(traces[0]["min_similarity"], .75)
        self.assertEqual(traces[1]["excluded_candidates"][0]["rejection_reason"], "embedding_contract_mismatch")
        self.assertIsNone(traces[1]["excluded_candidates"][0]["similarity"])
        self.assertEqual(traces[2]["excluded_candidates"], [])

    async def test_chinese_category_query_matches_ascii_subject_and_rejects_unrelated_facts(self):
        for turn, canonical, subject in (
            ("diet", "飲食計畫以高蛋白質為主", "health.diet_plan"),
            ("fitness", "重量訓練作為健身計畫的主軸", "health.fitness_plan"),
            ("computer", "電腦自動進入睡眠模式", "project.computer.sleep"),
        ):
            job = await self._job(turn, canonical)
            self.assertTrue(await self._apply(job, [{"action": "CREATE", "canonical_text": canonical,
                "memory_type": "project", "subject_key": subject, "target_memory_ids": [],
                "reason": "user statement"}], set(), {0: VECTOR}))
        orthogonal = [0.0, 1.0] + [0.0] * 1022
        rows = await self.repo.related_items("我們的健康管理方案包含什麼？", orthogonal)
        self.assertEqual({row["subject_key"] for row in rows}, {"health.diet_plan", "health.fitness_plan"})
        self.assertTrue(all(row["exact_match"] and row["similarity"] == 0 for row in rows))
        self.assertEqual(await self.repo.related_items("我最喜歡哪部電影？", orthogonal), [])

    async def test_fresh_session_retrieval_evidence_comes_from_db_candidates(self):
        item = await self._create()
        fresh_event = await self.repo.accept("fresh-session-with-no-history", "turn-1")
        embedding = SimpleNamespace(embed=AsyncMock(return_value=VECTOR))
        profile, relevant = await MemoryRetriever(self.repo, embedding).retrieve(
            "你還記得我喜歡什麼茶嗎", event_id=fresh_event,
        )
        self.assertEqual(profile, {})
        self.assertIn(item["canonical_text"], relevant)
        embedding.embed.assert_awaited_once()
        self.assertEqual(embedding.embed.await_args.kwargs["event_id"], fresh_event)

    async def test_unrelated_profile_is_not_injected(self):
        item = await self._create()
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    "UPDATE memory_items SET memory_type = 'profile', subject_key = 'profile.core_traits' "
                    "WHERE id = %s", (item["id"],),
                )
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[0.0, 1.0] + [0.0] * 1022))
        profile, relevant = await MemoryRetriever(self.repo, embedding).retrieve("完全無關主題")
        self.assertEqual((profile, relevant), ({}, ""))

    async def test_cli_store_waits_for_route_and_cleans_isolated_schema(self):
        run_scope = MemoryScope(uuid4(), uuid4(), "test_" + uuid4().hex)
        store = MemoryRunStore(TEST_URL, run_scope.schema_name, run_scope.user_id, run_scope.character_id)
        await asyncio.to_thread(store.open)
        try:
            self.assertEqual(await asyncio.to_thread(store.snapshot), {})
            run_repo = MemoryRepository(self.pool, run_scope)
            event_id = await run_repo.accept("run-session", "turn-1")
            self.assertFalse((await asyncio.to_thread(store.job, str(event_id)))["route_finalized"])
            self.assertTrue(await run_repo.route(event_id, MemoryRouting("needs_context", confidence=0.9),
                                                 "可能喜歡茶", []))
            job = await wait_memory_job(store, str(event_id), timeout=2)
            self.assertEqual((job["route"], job["status"]), ("needs_context", "buffered"))
            self.assertEqual(await asyncio.to_thread(store.audit, str(event_id)), [])
            async with self.pool.connection() as connection:
                async with connection.transaction():
                    await connection.execute(
                        sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(run_scope.schema_name))
                    )
                    await connection.execute(
                        "INSERT INTO memory_audit (id, user_id, character_id, operation_key, action, reason_class) "
                        "VALUES (%s, %s, %s, %s, 'CREATE', 'test')",
                        (uuid4(), run_scope.user_id, run_scope.character_id, f"{event_id}:0"),
                    )
            audit = await asyncio.to_thread(store.audit, event_id.hex)
            self.assertEqual(len(audit), 1)
            self.assertEqual({key: audit[0][key] for key in ("action", "target_id", "reason", "deleted_count")},
                             {"action": "CREATE", "target_id": None, "reason": "test", "deleted_count": None})
            self.assertEqual(audit[0]["operation_key"], f"{event_id}:0")
            self.assertIsNone(audit[0]["decision"])
            self.assertIsNotNone(audit[0]["created_at"])
            self.assertEqual(await self.repo.related_items("茶", VECTOR), [])
        finally:
            await asyncio.to_thread(store.close)
        with self.engine.connect() as connection:
            exists = connection.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :schema"),
                                        {"schema": run_scope.schema_name}).scalar()
        self.assertIsNone(exists)

    async def test_lexical_retrieval_works_without_embedding(self):
        item = await self._create()
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                await connection.execute(
                    "UPDATE memory_items SET canonical_text = 'unrelated', subject_key = 'project.dune', "
                    "keywords = ARRAY['arrakis'] WHERE id = %s AND user_id = %s AND character_id = %s",
                    (item["id"], self.scope.user_id, self.scope.character_id),
                )
        self.assertEqual((await self.repo.related_items("dune", None))[0]["id"], item["id"])
        self.assertEqual((await self.repo.related_items("arrakis", None))[0]["id"], item["id"])

    async def test_legacy_import_is_idempotent_and_owner_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            memory_dir = pathlib.Path(directory)
            (memory_dir / "user_profile.json").write_text(
                '{"recent_interests": ["天文", "攝影"]}', encoding="utf-8",
            )
            (memory_dir / "memory_records.json").write_text(
                '[{"id": "a", "text": "正在規劃旅行", "importance": 0.8, "status": "active"}]',
                encoding="utf-8",
            )
            entries = read_legacy_entries(memory_dir)
        vectors = {entry.id: VECTOR for entry in entries}
        self.assertEqual(await self.manager.import_legacy(entries, vectors), 3)
        self.assertEqual(await self.manager.import_legacy(entries, vectors), 0)
        self.assertEqual(len([row for row in await self.repo.related_items("天文", None)
                              if row["memory_type"] == "profile"]), 1)
        other = MemoryRepository(self.pool, MemoryScope(uuid4(), self.scope.character_id, self.scope.schema_name))
        self.assertEqual(await other.related_items("天文", None), [])
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                counts = await (await connection.execute(
                    "SELECT (SELECT count(*) FROM memory_sources), (SELECT count(*) FROM memory_audit)"
                )).fetchone()
        self.assertEqual(counts, (3, 3))

    async def test_worker_to_retriever_flow_is_idempotent(self):
        event = await self.repo.accept("session", "complete-flow")
        await self.repo.route(event, MemoryRouting(None), "請記住我喜歡茶", [])
        decision = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
                    "target_memory_ids": [], "source_ids": [str(event)], "reason": "user statement",
                    "importance": .8, "confidence": .9, "retention_class": "normal"}
        embedding = SimpleNamespace(embed=AsyncMock(return_value=VECTOR))
        llm = SimpleNamespace(decide=AsyncMock(return_value={"outcome": "complete", "reason": "done",
                            "decisions": [decision], "targets": {}}))
        workers = [MemoryWorker(self.repo, embedding, llm, self.manager) for _ in range(2)]
        await asyncio.gather(*(worker.process_one() for worker in workers))
        self.assertFalse(await workers[0].process_one())
        llm.decide.assert_awaited_once()
        self.assertEqual(embedding.embed.await_count, 2)
        profile, relevant = await MemoryRetriever(self.repo, embedding).retrieve("你記得我喜歡什麼茶嗎")
        self.assertEqual(profile, {})
        self.assertIn("使用者喜歡茶", relevant)
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (event,)))[0][0], "done")
        self.assertEqual((await self._query("SELECT count(*) FROM memory_audit"))[0][0], 1)

    async def test_worker_matches_replaced_entity_from_user_source(self):
        original = await self._job("latte", "請記住我最喜歡的飲料是拿鐵")
        await self._apply(original, [{"action": "CREATE", "canonical_text": "最喜歡的飲料是拿鐵",
            "memory_type": "preference", "subject_key": "preference.drink", "target_memory_ids": [],
            "reason": "user statement"}], set(), {0: VECTOR})
        old = (await self.repo.related_items("拿鐵", None))[0]
        event = await self.repo.accept("session", "coffee-change")
        await self.repo.route(event, MemoryRouting(None), "更正：我不喝拿鐵了，現在只喝美式咖啡", [])
        async def decide(job, sources, related, search, read, diagnostic):
            self.assertEqual([row["id"] for row in related], [old["id"]])
            return {"outcome": "complete", "reason": "done", "targets": {row["id"]: row for row in related},
                "decisions": [{"action": "SUPERSEDE", "canonical_text": "使用者現在只喝美式咖啡",
                    "memory_type": "preference", "subject_key": "preference.coffee", "importance": .7,
                    "confidence": .9, "retention_class": "normal", "reason": "replacement",
                    "source_ids": [str(event)], "target_memory_ids": [str(old["id"])]}]}
        worker = MemoryWorker(self.repo, SimpleNamespace(embed=AsyncMock(return_value=[0.0,1.0]+[0.0]*1022)),
                             SimpleNamespace(decide=decide), self.manager)
        await worker.process_one()
        current = await self.repo.related_items("咖啡", None)
        self.assertEqual([row["canonical_text"] for row in current], ["使用者現在只喝美式咖啡"])
        self.assertEqual(current[0]["group_id"], old["group_id"])
        self.assertEqual((await self._query("SELECT status FROM memory_items WHERE id = %s", (old["id"],)))[0][0], "superseded")

    async def test_forget_scrubs_source_job_and_audit(self):
        item = await self._create()
        job = await self._job("forget", "請忘記我喜歡茶這件事")
        decision = {"action": "FORGET", "target_memory_ids": [str(item["id"])], "reason": "user request"}
        self.assertTrue(await self._apply(job, [decision], {item["id"]}, {}))
        self.assertEqual(await self.repo.related_items("茶", VECTOR), [])
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                source_count = (await (await connection.execute("SELECT count(*) FROM memory_sources")).fetchone())[0]
                raw_jobs = (await (await connection.execute("SELECT count(*) FROM memory_jobs j JOIN memory_sources s ON j.id = s.id WHERE s.raw_text IS NOT NULL")).fetchone())[0]
                raw_audits = (await (await connection.execute("SELECT count(*) FROM memory_audit WHERE decision IS NOT NULL")).fetchone())[0]
                self.assertEqual((source_count, raw_jobs, raw_audits), (0, 0, 0))

    async def test_reset_rejects_inflight_job(self):
        job = await self._job("race")
        await self.repo.reset()
        decision = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference", "target_memory_ids": [], "reason": "test", "importance": 0.75, "confidence": 0.8, "retention_class": "normal"}
        self.assertFalse(await self._apply(job, [decision], set(), {0: VECTOR}))
        self.assertEqual(await self.repo.related_items("茶", VECTOR), [])

    async def test_reinforce_supersede_merge_contradict_archive(self):
        first = await self._create()
        target = str(first["id"])
        job = await self._job("reinforce")
        decision = {"action": "REINFORCE", "target_memory_ids": [target], "reason": "confirmed"}
        self.assertTrue(await self._apply(job, [decision], {first["id"]}, {}))

        job = await self._job("supersede")
        decision = {
            "action": "SUPERSEDE", "target_memory_ids": [target], "canonical_text": "使用者現在喜歡咖啡",
            "memory_type": "preference", "reason": "new preference", "importance": 0.8,
            "confidence": 0.8, "retention_class": "normal",
        }
        self.assertTrue(await self._apply(job, [decision], {first["id"]}, {0: VECTOR}))
        current = await self.repo.related_items("咖啡", VECTOR)
        self.assertEqual(len(current), 1)
        winner = current[0]
        self.assertEqual(winner["group_id"], first["group_id"])

        job = await self._job("duplicate")
        decision = {
            "action": "CREATE", "target_memory_ids": [], "canonical_text": "使用者喜歡咖啡",
            "memory_type": "preference", "reason": "duplicate", "importance": 0.7,
            "confidence": 0.8, "retention_class": "normal",
        }
        self.assertTrue(await self._apply(job, [decision], set(), {0: VECTOR}))
        duplicate = next(item for item in await self.repo.related_items("咖啡", VECTOR) if item["id"] != winner["id"])
        job = await self._job("merge")
        decision = {"action": "MERGE", "target_memory_ids": [str(winner["id"]), str(duplicate["id"])], "reason": "same fact"}
        self.assertTrue(await self._apply(job, [decision], {winner["id"], duplicate["id"]}, {}))
        self.assertEqual(len(await self.repo.related_items("咖啡", VECTOR)), 1)

        job = await self._job("contradict")
        decision = {
            "action": "CONTRADICT", "target_memory_ids": [str(winner["id"])],
            "canonical_text": "使用者不喜歡咖啡", "memory_type": "preference", "reason": "ambiguous conflict",
            "importance": 0.6, "confidence": 0.6, "retention_class": "normal",
        }
        self.assertTrue(await self._apply(job, [decision], {winner["id"]}, {0: VECTOR}))
        self.assertEqual(len(await self.repo.related_items("咖啡", VECTOR)), 2)

        job = await self._job("archive")
        decision = {"action": "ARCHIVE", "target_memory_ids": [str(winner["id"])], "reason": "no longer current"}
        self.assertTrue(await self._apply(job, [decision], {winner["id"]}, {}))
        self.assertEqual({row["status"] for row in await self.repo.related_items("咖啡", VECTOR)}, {"conflict"})
        history = await self.repo.related_items("咖啡", VECTOR, mode="history")
        self.assertEqual({item["status"] for item in history}, {"superseded", "archived", "conflict"})

    async def test_merge_links_current_user_source_to_winner_evidence(self):
        winner = await self._create("merge-winner")
        duplicate_job = await self._job("merge-duplicate", "請記住我也偏好烏龍茶")
        duplicate_decision = {
            "action": "CREATE", "target_memory_ids": [], "canonical_text": "使用者也偏好烏龍茶",
            "memory_type": "preference", "reason": "separate duplicate", "importance": 0.7,
            "confidence": 0.8, "retention_class": "normal",
        }
        self.assertTrue(await self._apply(duplicate_job, [duplicate_decision], set(), {0: VECTOR}))
        duplicate = next(
            item for item in await self.repo.related_items("烏龍茶", VECTOR)
            if item["id"] != winner["id"]
        )

        merge_job = await self._job("merge-current-source", "這兩筆其實是同一個茶偏好")
        merge_decision = {
            "action": "MERGE",
            "target_memory_ids": [str(winner["id"]), str(duplicate["id"])],
            "reason": "same preference",
        }
        self.assertTrue(await self._apply(
            merge_job, [merge_decision], {winner["id"], duplicate["id"]}, {},
        ))
        evidence = await self._query(
            """SELECT e.kind, s.raw_text FROM memory_evidence e
            JOIN memory_sources s ON s.id = e.source_id
            AND s.user_id = e.user_id AND s.character_id = e.character_id
            WHERE e.memory_id = %s AND e.source_id = %s""",
            (winner["id"], merge_job["id"]),
        )
        self.assertEqual(evidence, [("supports", "這兩筆其實是同一個茶偏好")])

        # 本輪合併來源也必須受到 FORGET 屏障保護；重連後的舊 metadata
        # 不能把已清除的合併 evidence 重新建回來源表。
        stale_message = build_user_message(
            "session", "merge-current-source", "這兩筆其實是同一個茶偏好",
            timestamp=1_700_000_000, generation=merge_job["generation"],
        )
        forget_job = await self._job("forget-merged", "忘記我的茶偏好")
        self.assertTrue(await self._apply(
            forget_job,
            [{"action": "FORGET", "target_memory_ids": [str(winner["id"])], "reason": "request"}],
            {winner["id"]}, {},
        ))
        current = await self.repo.accept("session", "after-forget-merged")
        self.assertTrue(await self.repo.route(current, MemoryRouting(None), "本輪新內容", [stale_message]))
        self.assertEqual(await self._query(
            "SELECT raw_text FROM memory_sources WHERE id = %s", (merge_job["id"],),
        ), [])

    async def _query(self, query, args=()):
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(query, args)
                return await cursor.fetchall() if cursor.description else []

    async def test_f1_unrelated_context_is_not_consumed_by_ignore(self):
        held = await self.repo.accept("session", "held")
        await self.repo.route(held, MemoryRouting("needs_context"), "可能喜歡咖啡", [])
        job = await self._job("unrelated", "天氣如何")
        context = await self.repo.context_jobs(job)
        self.assertEqual([row["id"] for row in context], [held])
        await self.repo.finish(job, "ignored")
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (held,)))[0][0], "buffered")

    async def test_f2_adopted_context_preserves_actual_user_evidence(self):
        held = await self.repo.accept("session", "held")
        await self.repo.route(held, MemoryRouting("needs_context"), "我喜歡拿鐵", [])
        job = await self._job("remember", "幫我記住這件事")
        context = await self.repo.context_jobs(job)
        self.assertEqual([row["id"] for row in context], [held])
        decision = {"action": "CREATE", "canonical_text": "使用者喜歡拿鐵", "memory_type": "preference",
                    "target_memory_ids": [], "source_ids": [str(held)], "reason": "user evidence"}
        await self._apply(job, [decision], set(), {0: VECTOR}, (held,))
        raw = await self._query("SELECT s.raw_text FROM memory_sources s JOIN memory_evidence e ON e.source_id = s.id")
        self.assertEqual({row[0] for row in raw}, {"我喜歡拿鐵"})
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (held,)))[0][0], "discarded")

    async def test_f3_unknown_profile_key_has_general_projection(self):
        item = await self._create()
        await self._query("UPDATE memory_items SET memory_type = 'profile', subject_key = 'profile.location', canonical_text = '台北' WHERE id = %s", (item["id"],))
        profile, relevant = await MemoryRetriever(self.repo, SimpleNamespace(embed=AsyncMock(return_value=VECTOR))).retrieve("居住地")
        self.assertEqual(profile, {})
        self.assertIn("台北", relevant)

    async def test_f4_future_is_not_current(self):
        item = await self._create()
        await self._query("UPDATE memory_items SET valid_from = now() + interval '30 days' WHERE id = %s", (item["id"],))
        self.assertEqual(await self.repo.related_items("茶", VECTOR), [])
        self.assertEqual(len(await self.repo.related_items("茶", VECTOR, mode="future")), 1)

    async def test_f5_context_requires_same_session_without_embedding(self):
        event = await self.repo.accept("session", "held")
        await self.repo.route(event, MemoryRouting("needs_context"), "喜歡拿鐵", [])
        same = await self._job("same", "記住那個")
        self.assertEqual([row["id"] for row in await self.repo.context_jobs(same)], [event])
        other = {**same, "conversation_id": uuid4()}
        self.assertEqual(await self.repo.context_jobs(other), [])
        columns = await self._query("SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = 'memory_jobs'", (self.scope.schema_name,))
        self.assertFalse({"embedding", "reviewed_candidates", "stage", "decisions"} & {row[0] for row in columns})

    async def test_f6_distinct_jobs_same_fact_are_idempotent(self):
        first = await self._create("first")
        second = await self._create("second")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual((await self._query("SELECT count(DISTINCT source_id) FROM memory_evidence"))[0][0], 2)

    async def test_f7_forget_includes_superseded_history(self):
        first = await self._create()
        job = await self._job("change", "我現在喜歡咖啡")
        decision = {"action": "SUPERSEDE", "canonical_text": "使用者現在喜歡咖啡", "memory_type": "preference",
                    "importance": .7, "confidence": .9, "retention_class": "normal", "reason": "explicit change",
                    "target_memory_ids": [str(first["id"])]}
        await self._apply(job, [decision], {first["id"]}, {0: VECTOR})
        latest = (await self.repo.related_items("咖啡", VECTOR))[0]
        job = await self._job("forget", "忘記我的飲料偏好")
        await self._apply(job, [{"action": "FORGET", "target_memory_ids": [str(latest["id"])], "reason": "request"}], {latest["id"]}, {})
        self.assertEqual(await self.repo.related_items("飲料", VECTOR, mode="history"), [])
        self.assertEqual((await self._query("SELECT count(*) FROM memory_sources"))[0][0], 0)
        self.assertEqual((await self._query("SELECT count(*) FROM memory_forget_barriers"))[0][0], 2)

    async def test_failure_retries_are_bounded_and_preserve_sources(self):
        event = await self.repo.accept("session", "retry")
        await self.repo.route(event, MemoryRouting(None), "我喜歡茶", [])
        embedding = SimpleNamespace(embed=AsyncMock(side_effect=RuntimeError("offline")))
        decision = {"action": "CREATE", "canonical_text": "喜歡茶", "memory_type": "preference",
                    "source_ids": [str(event)], "target_memory_ids": [], "reason": "test", "importance": .7,
                    "confidence": .8, "retention_class": "normal"}
        llm = SimpleNamespace(decide=AsyncMock(return_value={"outcome": "complete", "reason": "done", "decisions": [decision], "targets": {}}))
        worker = MemoryWorker(self.repo, embedding, llm, self.manager)
        for _ in range(3):
            await self._query("UPDATE memory_jobs SET updated_at = now() - interval '5 seconds' WHERE id = %s", (event,))
            self.assertTrue(await worker.process_one())
        self.assertFalse(await worker.process_one())
        row = (await self._query("SELECT status, (SELECT raw_text FROM memory_sources WHERE id = memory_jobs.id), attempts, agent_diagnostics FROM memory_jobs WHERE id = %s", (event,)))[0]
        self.assertEqual(row[:3], ("failed", "我喜歡茶", 3))
        self.assertEqual(row[3]["attempt"], 3)
        self.assertEqual(row[3]["failures"], 3)
        self.assertTrue(row[3]["retry_exhausted"])
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 0)

    async def test_retry_backoff_does_not_block_new_pending_work(self):
        job = await self._job("backoff")
        await self.repo.finish(job, "retry", error="offline")
        self.assertIsNone(await self.repo.claim())
        newer = await self._job("newer")
        self.assertNotEqual(newer["id"], job["id"])
        await self.repo.finish(newer, "ignored")
        await self._query("UPDATE memory_jobs SET updated_at = now() - interval '3 seconds' WHERE id = %s", (job["id"],))
        second = await self.repo.claim()
        self.assertEqual(second["attempts"], 2)
        await self.repo.finish(second, "retry")
        await self._query("UPDATE memory_jobs SET updated_at = now() - interval '3 seconds' WHERE id = %s", (job["id"],))
        self.assertIsNone(await self.repo.claim())
        await self._query("UPDATE memory_jobs SET updated_at = now() - interval '5 seconds' WHERE id = %s", (job["id"],))
        self.assertEqual((await self.repo.claim())["attempts"], 3)

    async def test_lease_exhaustion_has_diagnostics_and_owner_queue_metrics(self):
        job = await self._job("lease-exhausted")
        await self._query("UPDATE memory_jobs SET attempts = 3, lease_until = now() - interval '2 seconds', "
                          "agent_diagnostics = '{\"calls\":4}'::jsonb WHERE id = %s", (job["id"],))
        health = await self.repo.queue_health()
        self.assertEqual((health["active_jobs"], health["expired_leases"]), (1, 1))
        other = MemoryRepository(self.pool, MemoryScope(uuid4(), uuid4(), self.scope.schema_name))
        self.assertEqual((await other.queue_health())["active_jobs"], 0)
        await self.repo.expire_context()
        self.assertIsNone(await self.repo.claim())
        row = (await self._query("SELECT status, error, agent_diagnostics FROM memory_jobs WHERE id = %s", (job["id"],)))[0]
        self.assertEqual(row[:2], ("failed", "lease_exhausted"))
        self.assertEqual(row[2]["calls"], 4)
        self.assertTrue(row[2]["retry_exhausted"])
        self.assertEqual((await self.repo.queue_health())["retry_exhausted_jobs"], 1)

    async def test_timeout_after_accepted_proposal_retries_without_partial_write(self):
        from services import memory_worker
        from services.memory_llm import MemoryLLM
        from backend.tests.test_memory_agents import tool_response
        event = await self.repo.accept("session", "timeout-after-proposal")
        await self.repo.route(event, MemoryRouting(None), "我喜歡茶", [])
        operation = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
                     "source_ids": [str(event)], "importance": .7, "confidence": .9, "reason": "user statement",
                     "search_terms": ["茶", "飲品偏好"]}
        agent = object.__new__(MemoryLLM)
        calls = 0
        async def call(*args):
            nonlocal calls
            calls += 1
            if calls == 1:
                return tool_response("propose_operation", {"operation": operation})
            await asyncio.Event().wait()
        agent.call = call
        worker = MemoryWorker(self.repo, SimpleNamespace(embed=AsyncMock(return_value=VECTOR)), agent, self.manager)
        with patch.object(memory_worker, "ATTEMPT_TIMEOUT_SEC", .2):
            await worker.process_one()
        row = (await self._query("SELECT status, error, agent_diagnostics FROM memory_jobs WHERE id = %s", (event,)))[0]
        self.assertEqual(row[:2], ("retry", "TimeoutError"))
        self.assertEqual((row[2]["logical_steps"], row[2]["proposal_count"]), (2, 1))
        self.assertEqual((await self._query("SELECT count(*) FROM memory_audit"))[0][0], 0)
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 0)
        await self._query("UPDATE memory_jobs SET updated_at = now() - interval '3 seconds' WHERE id = %s", (event,))
        agent.call = AsyncMock(side_effect=[tool_response("propose_operation", {"operation": operation}),
                                           tool_response("finish", {"outcome": "complete", "reason": "done"})])
        await worker.process_one()
        self.assertFalse(await worker.process_one())
        row = (await self._query("SELECT status, attempts, agent_diagnostics FROM memory_jobs WHERE id = %s", (event,)))[0]
        self.assertEqual(row[:2], ("done", 2))
        self.assertEqual(row[2]["logical_steps"], 4)
        self.assertEqual(row[2]["timeouts"], 1)
        self.assertEqual((await self._query("SELECT count(*) FROM memory_audit"))[0][0], 1)
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 1)

    async def test_cancelled_worker_recovers_lease_and_commits_only_once(self):
        from services.memory_llm import MemoryLLM
        from backend.tests.test_memory_agents import tool_response
        event = await self.repo.accept("session", "cancel-agent")
        await self.repo.route(event, MemoryRouting(None), "我喜歡茶", [])
        operation = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
                     "source_ids": [str(event)], "importance": .7, "confidence": .9, "reason": "user statement",
                     "search_terms": ["茶", "飲品偏好"]}
        agent = object.__new__(MemoryLLM)
        entered = asyncio.Event()
        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()
        agent.call = blocked
        worker = MemoryWorker(self.repo, SimpleNamespace(embed=AsyncMock(return_value=VECTOR)), agent, self.manager)
        task = asyncio.create_task(worker.process_one())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (event,)))[0][0], "running")
        await self._query("UPDATE memory_jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (event,))
        agent.call = AsyncMock(side_effect=[tool_response("propose_operation", {"operation": operation}),
                                           tool_response("finish", {"outcome": "complete", "reason": "done"})])
        await worker.process_one()
        row = (await self._query("SELECT status, attempts, agent_diagnostics FROM memory_jobs WHERE id = %s", (event,)))[0]
        self.assertEqual(row[:2], ("done", 2))
        self.assertGreaterEqual(row[2]["recovered_lease_age_sec"], 1)
        self.assertLess(row[2]["recovered_lease_age_sec"], 5)
        self.assertEqual((await self._query("SELECT count(*) FROM memory_audit"))[0][0], 1)

    async def test_commit_rejection_after_reset_keeps_cancelled_generation(self):
        event = await self.repo.accept("session", "reset-agent")
        await self.repo.route(event, MemoryRouting(None), "我喜歡茶", [])
        decision = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
                    "source_ids": [str(event)], "target_memory_ids": [], "importance": .7,
                    "confidence": .9, "retention_class": "normal", "reason": "user statement"}
        async def reset_then_complete(*args):
            await self.repo.reset()
            return {"outcome": "complete", "decisions": [decision], "targets": {}}
        worker = MemoryWorker(self.repo, SimpleNamespace(embed=AsyncMock(return_value=VECTOR)),
                              SimpleNamespace(decide=reset_then_complete), self.manager)
        await worker.process_one()
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (event,)))[0][0], "cancelled")
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 0)

    async def test_validation_retry_receives_error_and_commits_corrected_result(self):
        from services.memory_llm import MemoryLLM
        from backend.tests.test_memory_agents import tool_response
        event = await self.repo.accept("session", "invalid-key")
        await self.repo.route(event, MemoryRouting(None), "我的飲食計畫以高蛋白質為主", [])
        operation = {"action": "CREATE", "canonical_text": "健康管理：飲食計畫以高蛋白質為主",
                     "memory_type": "project", "subject_key": "health.diet_plan", "importance": .7,
                     "confidence": .9, "source_ids": [str(event)], "reason": "user statement",
                     "search_terms": ["健康管理", "飲食計畫", "高蛋白質"]}
        agent = object.__new__(MemoryLLM)
        agent.call = AsyncMock(side_effect=[tool_response("propose_operation", {"operation": {**operation, "subject_key": "健康管理.飲食"}}),
                                           tool_response("propose_operation", {"operation": operation}),
                                           tool_response("finish", {"outcome": "complete", "reason": "done"})])
        worker = MemoryWorker(self.repo, SimpleNamespace(embed=AsyncMock(return_value=VECTOR)), agent, self.manager)
        await worker.process_one()
        self.assertIn("subject_key", agent.call.call_args_list[1].args[0][-1]["content"])
        row = (await self._query("SELECT status, attempts, agent_diagnostics FROM memory_jobs WHERE id = %s", (event,)))[0]
        self.assertEqual(row[:2], ("done", 1))
        self.assertEqual(row[2]["corrections"], 1)
        self.assertEqual(len(await self.repo.related_items("健康管理", None)), 1)

    async def test_jev_interruption_recovers_after_deadline(self):
        event = await self.repo.accept("session", "interrupted")
        await self.repo.route(event, MemoryRouting(None), "我住台北", [], finalized=False)
        self.assertIsNone(await self.repo.claim())
        await self._query("UPDATE memory_jobs SET created_at = now() - interval '61 seconds' WHERE id = %s", (event,))
        restored = await self.repo.claim()
        self.assertEqual((restored["route"], restored["error"], restored["source_text"]), ("process", "jev_timeout", "我住台北"))

    async def test_context_expiry_never_promotes_and_removes_orphan_source(self):
        event = await self.repo.accept("session", "expire")
        await self.repo.route(event, MemoryRouting("needs_context"), "不確定的人", [])
        await self._query("UPDATE memory_jobs SET expires_at = now() - interval '1 second' WHERE id = %s", (event,))
        await self.repo.expire_context()
        self.assertIsNone(await self.repo.claim())
        self.assertEqual((await self._query("SELECT status, (SELECT raw_text FROM memory_sources WHERE id = memory_jobs.id) FROM memory_jobs WHERE id = %s", (event,)))[0], ("discarded", None))
        self.assertEqual((await self._query("SELECT count(*) FROM memory_sources"))[0][0], 0)

    async def _prepared(self, turn, canonical="使用者喜歡茶", subject="preference.tea"):
        job = await self._job(turn, canonical)
        decision = {"canonical_text": canonical, "subject_key": subject, "memory_type": "preference",
                    "importance": .7, "confidence": .9, "retention_class": "normal", "reason": "user statement",
                    "source_ids": [str(job["id"])], "action": "CREATE", "target_memory_ids": []}
        return job, decision

    async def test_concurrent_identical_creates_serialize_and_reinforce(self):
        first, second = await self._prepared("parallel1"), await self._prepared("parallel2")
        results = await asyncio.gather(*(self.manager.apply(job, [decision], set(), {0: VECTOR})
                                         for job, decision in (second, first)))
        self.assertEqual(results, [True, True])
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 1)
        self.assertEqual((await self._query("SELECT count(DISTINCT source_id) FROM memory_evidence"))[0][0], 2)
        self.assertFalse(await self.manager.apply(first[0], [first[1]], set(), {0: VECTOR}))

    async def test_newer_fact_prevents_older_candidate_overwrite(self):
        old = await self._prepared("old", "使用者喜歡茶", "preference.drink")
        new = await self._prepared("new", "使用者現在喜歡咖啡", "preference.drink")
        await self.manager.apply(new[0], [new[1]], set(), {0: VECTOR})
        with self.assertRaisesRegex(ValueError, "較新的事實"):
            await self.manager.apply(old[0], [old[1]], set(), {0: VECTOR})
        self.assertEqual([row["canonical_text"] for row in await self.repo.related_items("咖啡", VECTOR)], ["使用者現在喜歡咖啡"])

    async def test_reset_rejects_agent_already_running(self):
        job, decision = await self._prepared("reset-ready")
        await self.repo.reset()
        self.assertFalse(await self.manager.apply(job, [decision], set(), {0: VECTOR}))
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 0)

    async def test_forget_preserves_unrelated_waiting_input(self):
        item = await self._create()
        waiting = await self.repo.accept("another", "held")
        await self.repo.route(waiting, MemoryRouting("needs_context"), "旅遊目的地尚未決定", [])
        dismissed = await self._job("dismissed", "你記得我嗎")
        await self.repo.finish(dismissed, "ignored")
        job = await self._job("forget-safe", "忘記我的茶偏好")
        await self._apply(job, [{"action": "FORGET", "target_memory_ids": [str(item["id"])], "reason": "request"}], {item["id"]}, {})
        self.assertEqual((await self._query("SELECT raw_text FROM memory_sources WHERE id = %s", (waiting,)))[0][0], "旅遊目的地尚未決定")
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (waiting,)))[0][0], "buffered")

    async def test_agent_cannot_cite_unrelated_owner_source(self):
        unrelated = await self._job("unrelated")
        job = await self._job("unauthorized")
        decision = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
                    "source_ids": [str(unrelated["id"])], "target_memory_ids": [], "reason": "test",
                    "importance": .7, "confidence": .8, "retention_class": "normal"}
        with self.assertRaisesRegex(ValueError, "未授權"):
            await self.manager.apply(job, [decision], set(), {0: VECTOR})
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 0)

    async def test_migration_preserves_pending_legacy_source(self):
        schema = "test_" + uuid4().hex
        event = uuid4()
        try:
            with self.engine.begin() as connection:
                config = Config(str(BACKEND_ROOT / "alembic.ini"))
                config.attributes.update(connection=connection, schema=schema)
                command.upgrade(config, "0004_embedding_model")
                connection.execute(text(f"""INSERT INTO "{schema}".memory_jobs
                    (id,user_id,character_id,generation,conversation_id,message_id,route,status,source_text,route_finalized)
                    VALUES (:id,:user,:character,0,:id,:id,'process','pending','legacy user evidence',true)"""),
                    {"id": event, "user": self.scope.user_id, "character": self.scope.character_id})
                command.upgrade(config, "head")
                row = connection.execute(text(f'SELECT route,source_ids FROM "{schema}".memory_jobs')).one()
                self.assertEqual((row[0], row[1]), ("process", [event]))
                self.assertEqual(connection.execute(text(f'SELECT raw_text FROM "{schema}".memory_sources')).scalar_one(), "legacy user evidence")
        finally:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    async def test_future_correction_keeps_old_version_current_until_effective(self):
        from datetime import datetime, timedelta, timezone
        first = await self._create()
        job = await self._job("future-change", "下個月改喝咖啡")
        decision = {"action": "SUPERSEDE", "canonical_text": "使用者將改喝咖啡", "memory_type": "preference",
                    "target_memory_ids": [str(first["id"])], "reason": "future change", "importance": .7,
                    "confidence": .9, "retention_class": "normal",
                    "valid_from": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}
        await self._apply(job, [decision], {first["id"]}, {0: VECTOR})
        self.assertEqual([row["id"] for row in await self.repo.related_items("飲料", VECTOR)], [first["id"]])
        self.assertEqual(len(await self.repo.related_items("飲料", VECTOR, mode="future")), 1)

    async def test_temporary_expiry_removes_current_projection_but_preserves_history(self):
        item = await self._create()
        await self._query("UPDATE memory_items SET retention_class = 'temporary', "
                          "expires_at = now() - interval '1 second' WHERE id = %s", (item["id"],))
        self.assertEqual(await self.repo.related_items("茶", VECTOR), [])
        await self.repo.expire_temporary()
        rows = await self.repo.related_items("茶", VECTOR, mode="history")
        self.assertEqual([(row["id"], row["status"]) for row in rows], [(item["id"], "expired")])

    async def test_expired_lease_reclaimed_after_pool_restart_rejects_old_attempt(self):
        job, decision = await self._prepared("restart-lease")
        await self._query("UPDATE memory_jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job["id"],))
        await self.pool.close()
        self.pool = await make_pool(TEST_URL)
        self.repo = MemoryRepository(self.pool, self.scope, EMBEDDING_MODEL, EMBEDDING_CONTRACT)
        self.manager = MemoryDBManager(self.pool, self.scope, "test-model", EMBEDDING_MODEL, EMBEDDING_CONTRACT)
        recovered = await self.repo.claim()
        self.assertEqual(recovered["id"], job["id"])
        self.assertGreater(recovered["attempts"], job["attempts"])
        self.assertFalse(await self.manager.apply(job, [decision], set(), {0: VECTOR}))
        self.assertTrue(await self.manager.apply(recovered, [decision], set(), {0: VECTOR}))
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 1)

    async def test_terminated_process_claim_is_recovered_without_duplicate_mutation(self):
        event = await self.repo.accept("session", "terminated-process")
        await self.repo.route(event, MemoryRouting(None), "我喜歡茶", [])
        code = """
import asyncio, os, sys
from uuid import UUID
sys.path.insert(0, 'backend')
from domain.memory_scope import MemoryScope
from infrastructure.memory_database import make_pool
from infrastructure.memory_repository import MemoryRepository
async def main():
    scope = MemoryScope(UUID(sys.argv[1]), UUID(sys.argv[2]), sys.argv[3])
    pool = await make_pool(os.environ['MEMORY_TEST_DATABASE_URL'])
    job = await MemoryRepository(pool, scope).claim()
    if job is None:
        raise RuntimeError('Expected isolated test job')
    print('claimed', flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", code,
            str(self.scope.user_id), str(self.scope.character_id), self.scope.schema_name,
            cwd=BACKEND_ROOT.parent, env={**os.environ, "MEMORY_TEST_DATABASE_URL": TEST_URL},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 5), b"claimed\n")
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), 5)
        attempts, generation = (await self._query("SELECT attempts, generation FROM memory_jobs WHERE id = %s", (event,)))[0]
        old = {"id": event, "attempts": attempts, "generation": generation, "source_text": "我喜歡茶"}
        await self._query("UPDATE memory_jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (event,))
        recovered = await self.repo.claim()
        self.assertEqual((recovered["id"], recovered["attempts"]), (event, 2))
        decision = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
                    "target_memory_ids": [], "reason": "user statement"}
        self.assertFalse(await self._apply(old, [decision], set(), {0: VECTOR}))
        self.assertTrue(await self._apply(recovered, [decision], set(), {0: VECTOR}))
        self.assertEqual((await self._query("SELECT count(*) FROM memory_audit"))[0][0], 1)
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 1)

    async def test_forget_cancels_older_uncommitted_duplicate(self):
        job, decision = await self._prepared("old-pending", "使用者喜歡茶", "preference.tea")
        saved, fact = await self._prepared("new-saved", "使用者喜歡茶", "preference.tea")
        await self.manager.apply(saved, [fact], set(), {0: VECTOR})
        item = (await self.repo.related_items("茶", VECTOR))[0]
        forget = await self._job("forget-pending", "忘記我的茶偏好")
        await self._apply(forget, [{"action": "FORGET", "target_memory_ids": [str(item["id"])], "reason": "request"}], {item["id"]}, {})
        self.assertFalse(await self.manager.apply(job, [decision], set(), {0: VECTOR}))
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 0)
        self.assertEqual((await self._query("SELECT count(*) FROM memory_sources WHERE raw_text IS NOT NULL"))[0][0], 0)

    async def test_forget_single_version_keeps_authorized_history_boundary(self):
        first = await self._create()
        job = await self._job("version-change", "我現在喜歡咖啡")
        decision = {"action": "SUPERSEDE", "canonical_text": "使用者現在喜歡咖啡", "memory_type": "preference",
                    "target_memory_ids": [str(first["id"])], "reason": "change", "importance": .7,
                    "confidence": .9, "retention_class": "normal"}
        await self._apply(job, [decision], {first["id"]}, {0: VECTOR})
        latest = (await self.repo.related_items("咖啡", VECTOR))[0]
        job = await self._job("forget-version", "請只忘記咖啡偏好的最新版本")
        await self._apply(job, [{"action": "FORGET", "target_memory_ids": [str(latest["id"])], "reason": "one version only"}], {latest["id"]}, {})
        history = await self.repo.related_items("茶", VECTOR, mode="history")
        self.assertEqual([row["id"] for row in history], [first["id"]])

    async def test_target_changed_after_delivery_rejects_atomic_batch(self):
        item = await self._create()
        job = await self._job("stale", "我的計畫變了")
        snapshots = {row["id"]: row for row in await self.repo.read_memories({item["id"]})}
        await self._query("UPDATE memory_items SET canonical_text = '使用者喜歡無糖茶', updated_at = now() WHERE id = %s", (item["id"],))
        decisions = [
            {"action": "CREATE", "canonical_text": "使用者使用 Linux", "memory_type": "profile", "importance": .7,
             "confidence": .9, "retention_class": "normal", "target_memory_ids": [], "source_ids": [str(job["id"])], "reason": "test"},
            {"action": "ARCHIVE", "target_memory_ids": [str(item["id"])], "source_ids": [str(job["id"])], "reason": "test"}]
        with self.assertRaisesRegex(ValueError, "target 已在檢索後變更"):
            await self.manager.apply(job, decisions, {item["id"]}, {0: VECTOR}, target_snapshots=snapshots)
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 1)
        self.assertEqual((await self._query("SELECT count(*) FROM memory_audit WHERE source_event_id = %s", (job["id"],)))[0][0], 0)
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (job["id"],)))[0][0], "running")

    async def test_context_from_other_session_is_not_authorized(self):
        held = await self.repo.accept("other-session", "held")
        await self.repo.route(held, MemoryRouting("needs_context"), "我喜歡茶", [])
        job = await self._job("current", "記住那個")
        decision = {"action": "CREATE", "canonical_text": "喜歡茶", "memory_type": "preference",
                    "target_memory_ids": [], "source_ids": [str(held)], "reason": "test"}
        with self.assertRaisesRegex(ValueError, "buffer promotion"):
            await self._apply(job, [decision], set(), {0: VECTOR}, (held,))

    async def test_worker_query_reused_during_focused_search(self):
        from services.memory_llm import MemoryLLM
        from backend.tests.test_memory_agents import tool_response
        item = await self._create()
        event = await self.repo.accept("session", "cache")
        source = "我仍然喜歡茶"
        await self.repo.route(event, MemoryRouting(None), source, [])
        operation = {"action": "REINFORCE", "target_memory_ids": [str(item["id"])], "source_ids": [str(event)], "reason": "support"}
        agent = object.__new__(MemoryLLM)
        agent.call = AsyncMock(side_effect=[tool_response("search_memories", {"query": source}),
            tool_response("search_memories", {"query": source}), tool_response("propose_operation", {"operation": operation}),
            tool_response("finish", {"outcome": "complete", "reason": "done"})])
        embedding = SimpleNamespace(embed=AsyncMock(return_value=VECTOR))
        await MemoryWorker(self.repo, embedding, agent, self.manager).process_one()
        embedding.embed.assert_awaited_once()
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (event,)))[0][0], "done")

    async def test_implicit_overall_design_uses_authorized_context_to_find_project(self):
        job = await self._job("project-cloud", "圖書館系統雲端同步使用 AWS")
        decision = {"action": "CREATE", "canonical_text": "圖書館系統雲端同步使用 AWS", "memory_type": "project",
                    "target_memory_ids": [], "reason": "user fact"}
        self.assertTrue(await self._apply(job, [decision], set(), {0: VECTOR}))
        expected = (await self.repo.related_items("AWS", None))[0]
        context_event = await self.repo.accept("session", "project-context")
        self.assertTrue(await self.repo.route(
            context_event, MemoryRouting("needs_context"), "雲端同步使用 AWS", [],
        ))
        event = await self.repo.accept("session", "project-goal")
        await self.repo.route(event, MemoryRouting(None), "整體設計追求低功耗",
                              [build_user_message(
                                  "session", "project-context", "雲端同步使用 AWS",
                                  timestamp=1_700_000_000, generation=0,
                              )])
        embedding = SimpleNamespace(embed=AsyncMock(return_value=[0.0, 1.0] + [0.0] * 1022))
        agent = SimpleNamespace(decide=AsyncMock(return_value={"outcome": "ignore", "reason": "candidate inspection"}))
        self.assertTrue(await MemoryWorker(self.repo, embedding, agent, self.manager).process_one())
        query = embedding.embed.await_args.args[0]
        self.assertIn("AWS", query)
        self.assertIn("整體設計追求低功耗", query)
        candidates = agent.decide.await_args.args[2]
        self.assertEqual([row["id"] for row in candidates], [expected["id"]])
        self.assertTrue(candidates[0]["exact_match"])
        self.assertAlmostEqual(candidates[0]["similarity"], 0)

    async def test_0005_migration_replays_sources_and_invalidates_old_claim(self):
        schema = "test_" + uuid4().hex
        events = {name: uuid4() for name in ("pending", "running", "buffered", "done", "missing")}
        owner = {"user": self.scope.user_id, "character": self.scope.character_id}
        try:
            with self.engine.begin() as connection:
                config = Config(str(BACKEND_ROOT / "alembic.ini"))
                config.attributes.update(connection=connection, schema=schema)
                command.upgrade(config, "0005_memory_agents")
                connection.execute(text(f'INSERT INTO "{schema}".memory_scope_state (user_id,character_id) VALUES (:user,:character)'), owner)
                for name, event in events.items():
                    status = "pending" if name == "missing" else name
                    connection.execute(text(f"""INSERT INTO "{schema}".memory_jobs
                        (id,user_id,character_id,generation,conversation_id,message_id,route,status,stage,attempts,
                         intake_attempts,librarian_attempts,source_ids,route_finalized,lease_until)
                        VALUES (:id,:user,:character,0,:id,:id,'candidate',:status,'librarian',6,3,3,ARRAY[:id]::uuid[],true,now()+interval '120 seconds')"""),
                        {**owner, "id": event, "status": status})
                    if name != "missing":
                        connection.execute(text(f"""INSERT INTO "{schema}".memory_sources
                            (id,user_id,character_id,conversation_id,message_id,speaker,raw_text,occurred_at)
                            VALUES (:id,:user,:character,:id,:id,'user',:raw,now())"""),
                            {**owner, "id": event, "raw": "請記住我喜歡茶"})
                command.upgrade(config, "head")
                rows = {row["id"]: row for row in connection.execute(text(f'SELECT id,status,route,generation,attempts,source_ids FROM "{schema}".memory_jobs')).mappings()}
                self.assertEqual(rows[events["running"]]["generation"], 1)
                for name in ("pending", "running"):
                    self.assertEqual((rows[events[name]]["status"], rows[events[name]]["route"], rows[events[name]]["attempts"]), ("pending", "process", 0))
                    self.assertEqual(rows[events[name]]["source_ids"], [events[name]])
                self.assertEqual(rows[events["buffered"]]["status"], "buffered")
                self.assertEqual(rows[events["done"]]["status"], "done")
                self.assertEqual(rows[events["missing"]]["status"], "failed")
                self.assertEqual(connection.execute(text(f'SELECT count(*) FROM "{schema}".memory_sources')).scalar_one(), 4)
            repo = MemoryRepository(self.pool, MemoryScope(self.scope.user_id,self.scope.character_id,schema), EMBEDDING_MODEL, EMBEDDING_CONTRACT)
            restored = await repo.claim()
            self.assertEqual(restored["instruction"], "remember")
        finally:
            with self.engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


if __name__ == "__main__":
    unittest.main()
