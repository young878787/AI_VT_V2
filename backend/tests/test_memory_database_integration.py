"""使用專用 DB 與每個測試獨立的 schema 驗證 Memory transaction。"""

import asyncio
import math
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
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

from domain.memory_scope import MemoryScope
from domain.memory_routing import MemoryRouting
from infrastructure.memory_database import check_schema, make_pool
from infrastructure.memory_repository import MemoryRepository
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
        from domain.memory_routing import instruction_policy
        sources = await self.repo.intake_sources(job, [])
        candidates = []
        for decision in decisions:
            candidate = {
                "canonical_text": decision.get("canonical_text", job["source_text"]),
                "memory_type": decision.get("memory_type", "event"),
                "importance": decision.get("importance", 0.7), "confidence": decision.get("confidence", 0.8),
                "retention_class": decision.get("retention_class", "normal"),
                "reason": decision["reason"], "source_ids": [str(job["id"])],
                "intent": "forget" if instruction_policy(job["source_text"]) == "forget" else "fact",
            }
            for key in ("subject_key", "valid_from", "valid_to", "expires_at"):
                if key in decision:
                    candidate[key] = decision[key]
            candidates.append(candidate)
        handed = await self.repo.complete_intake(job, {"route": "candidate", "candidates": candidates}, sources, [], {}, VECTOR)
        if not handed:
            return False
        reviewed = await self.repo.claim()
        applied = [{**candidate, **decision, "source_ids": candidate["source_ids"], "candidate_index": index}
                   for index, (candidate, decision) in enumerate(zip(candidates, decisions))]
        for decision in applied:
            decision.pop("intent", None)
        return await self.manager.apply(reviewed, applied, targets, embeddings, context_ids)

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
            buffer_id, MemoryRouting("needs_context", confidence=0.9), "可能喜歡茶", [], VECTOR,
        ))
        self.assertIsNone(await self.repo.claim())
        item = await self._create()
        buffers = await self.repo.related_context({"id": uuid4(), "conversation_id": uuid4(), "source_text": "茶"}, VECTOR)
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
        self.assertEqual(len(await self.repo.related_context(
            {"id": uuid4(), "conversation_id": uuid4(), "source_text": "茶"}, VECTOR)), 1)

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
                                                 "可能喜歡茶", [], VECTOR))
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
        event_id = await self.repo.accept("session", "complete-flow")
        self.assertTrue(await self.repo.route(
            event_id, MemoryRouting(None, confidence=0.9), "請記住我喜歡茶", [], VECTOR,
        ))
        decision = {
            "action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
            "target_memory_ids": [], "reason": "explicit statement", "importance": 0.8,
            "confidence": 0.9, "retention_class": "normal",
        }
        embedding = SimpleNamespace(embed=AsyncMock(return_value=VECTOR))
        candidate = {key: value for key, value in decision.items() if key not in {"action", "target_memory_ids"}}
        candidate.update(intent="fact", source_ids=[str(event_id)])
        intake = SimpleNamespace(review=AsyncMock(return_value=({"route": "candidate", "candidates": [candidate]}, {})))
        decision.update(source_ids=[str(event_id)], candidate_index=0)
        llm = SimpleNamespace(decide=AsyncMock(return_value=([decision], {})))
        workers = [MemoryWorker(self.repo, embedding, llm, self.manager, intake) for _ in range(2)]
        await asyncio.gather(*(worker.process_one() for worker in workers))
        await workers[0].process_one()
        intake.review.assert_awaited_once()
        llm.decide.assert_awaited_once()
        items = await self.repo.related_items("喜歡茶", VECTOR)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                diagnostic = await (await connection.execute(
                    "SELECT status, error FROM memory_jobs WHERE id = %s", (event_id,),
                )).fetchone()
        self.assertEqual(len(items), 1, diagnostic)
        profile, relevant = await MemoryRetriever(self.repo, embedding).retrieve("你記得我喜歡什麼茶嗎")
        self.assertEqual(profile, {})
        self.assertIn("使用者喜歡茶", relevant)
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                job_status = (await (await connection.execute(
                    "SELECT status FROM memory_jobs WHERE id = %s", (event_id,),
                )).fetchone())[0]
                audit_count = (await (await connection.execute("SELECT count(*) FROM memory_audit")).fetchone())[0]
        self.assertEqual((job_status, audit_count), ("done", 1))

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

    async def _query(self, query, args=()):
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                cursor = await connection.execute(query, args)
                return await cursor.fetchall() if cursor.description else []

    async def test_f1_unrelated_context_not_selected_and_not_consumed(self):
        held = await self.repo.accept("session", "held")
        await self.repo.route(held, MemoryRouting("needs_context"), "可能喜歡咖啡", [], [0., 1.] + [0.] * 1022)
        job = await self._job("unrelated", "我住在台北")
        self.assertEqual(await self.repo.related_context(job, VECTOR), [])
        sources = await self.repo.intake_sources(job, [])
        await self.repo.complete_intake(job, {"route": "none", "reason": "no fact"}, sources, [], {})
        self.assertEqual((await self._query("SELECT status, (SELECT raw_text FROM memory_sources WHERE id = memory_jobs.id) FROM memory_jobs WHERE id = %s", (held,)))[0],
                         ("buffered", "可能喜歡咖啡"))

    async def test_f2_adopted_context_preserves_actual_user_evidence(self):
        held = await self.repo.accept("session", "held")
        await self.repo.route(held, MemoryRouting("needs_context"), "我喜歡拿鐵", [], VECTOR)
        job = await self._job("remember", "幫我記住這件事")
        context = await self.repo.related_context(job, VECTOR)
        sources = await self.repo.intake_sources(job, context)
        candidate = {"canonical_text": "使用者喜歡拿鐵", "memory_type": "preference", "importance": .7,
                     "confidence": .9, "retention_class": "normal", "intent": "fact", "reason": "user evidence",
                     "source_ids": [str(held)]}
        self.assertTrue(await self.repo.complete_intake(job, {"route": "candidate", "candidates": [candidate]}, sources, context, {}))
        reviewed = await self.repo.claim()
        decision = {key: value for key, value in candidate.items() if key != "intent"}
        decision.update(action="CREATE", target_memory_ids=[], candidate_index=0)
        await self.manager.apply(reviewed, [decision], set(), {0: VECTOR}, tuple(reviewed["context_job_ids"]))
        raw = await self._query("SELECT s.raw_text FROM memory_sources s JOIN memory_evidence e ON e.source_id = s.id")
        self.assertTrue(raw)
        self.assertEqual({row[0] for row in raw}, {"我喜歡拿鐵"})
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (held,)))[0], ("discarded",))

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

    async def test_f5_context_embedding_repairs_contract(self):
        event = await self.repo.accept("session", "held")
        await self.repo.route(event, MemoryRouting("needs_context"), "喜歡拿鐵", [])
        job = (await self.repo.unembedded_context())[0]
        self.assertTrue(await self.repo.save_context_embedding(job, VECTOR))
        self.assertEqual((await self._query("SELECT embedding_model, embedding_contract FROM memory_jobs WHERE id = %s", (event,)))[0],
                         (EMBEDDING_MODEL, EMBEDDING_CONTRACT))
        self.assertEqual(len(await self.repo.related_context({"id": uuid4(), "conversation_id": uuid4(), "source_text": "拿鐵"}, VECTOR)), 1)

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
        intake = SimpleNamespace(review=AsyncMock())
        worker = MemoryWorker(self.repo, embedding, None, self.manager, intake)
        for _ in range(3):
            self.assertTrue(await worker.process_one())
        self.assertFalse(await worker.process_one())
        intake.review.assert_not_awaited()
        row = (await self._query("SELECT status, (SELECT raw_text FROM memory_sources WHERE id = memory_jobs.id), agent_diagnostics FROM memory_jobs WHERE id = %s", (event,)))[0]
        self.assertEqual(row[:2], ("failed", "我喜歡茶"))
        self.assertEqual(len([item for item in row[2] if item["role"] == "intake"]), 3)

    async def test_jev_interruption_recovers_after_deadline(self):
        event = await self.repo.accept("session", "interrupted")
        await self.repo.route(event, MemoryRouting(None), "我住台北", [], finalized=False)
        self.assertIsNone(await self.repo.claim())
        await self._query("UPDATE memory_jobs SET created_at = now() - interval '61 seconds' WHERE id = %s", (event,))
        restored = await self.repo.claim()
        self.assertEqual((restored["stage"], restored["error"], restored["source_text"]), ("intake", "jev_timeout", "我住台北"))

    async def test_context_expiry_never_promotes_and_removes_orphan_source(self):
        event = await self.repo.accept("session", "expire")
        await self.repo.route(event, MemoryRouting("needs_context"), "不確定的人", [])
        await self._query("UPDATE memory_jobs SET expires_at = now() - interval '1 second' WHERE id = %s", (event,))
        await self.repo.expire_context()
        self.assertIsNone(await self.repo.claim())
        self.assertEqual((await self._query("SELECT status, (SELECT raw_text FROM memory_sources WHERE id = memory_jobs.id) FROM memory_jobs WHERE id = %s", (event,)))[0], ("discarded", None))
        self.assertEqual((await self._query("SELECT count(*) FROM memory_sources"))[0][0], 0)

    async def _reviewed(self, turn, canonical="使用者喜歡茶", subject="preference.tea"):
        job = await self._job(turn, canonical)
        candidate = {"canonical_text": canonical, "subject_key": subject, "memory_type": "preference",
                     "importance": .7, "confidence": .9, "retention_class": "normal", "intent": "fact",
                     "reason": "user statement", "source_ids": [str(job["id"])]}
        sources = await self.repo.intake_sources(job, [])
        await self.repo.complete_intake(job, {"route": "candidate", "candidates": [candidate]}, sources, [], {})
        reviewed = await self.repo.claim()
        decision = {key: value for key, value in candidate.items() if key != "intent"}
        decision.update(action="CREATE", target_memory_ids=[], candidate_index=0)
        return reviewed, decision

    async def test_concurrent_identical_creates_serialize_and_reinforce(self):
        first, second = await self._reviewed("parallel1"), await self._reviewed("parallel2")
        results = await asyncio.gather(*(self.manager.apply(job, [decision], set(), {0: VECTOR})
                                         for job, decision in (second, first)))
        self.assertEqual(results, [True, True])
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 1)
        self.assertEqual((await self._query("SELECT count(DISTINCT source_id) FROM memory_evidence"))[0][0], 2)
        self.assertFalse(await self.manager.apply(first[0], [first[1]], set(), {0: VECTOR}))

    async def test_newer_fact_prevents_older_candidate_overwrite(self):
        old = await self._reviewed("old", "使用者喜歡茶", "preference.drink")
        new = await self._reviewed("new", "使用者現在喜歡咖啡", "preference.drink")
        await self.manager.apply(new[0], [new[1]], set(), {0: VECTOR})
        with self.assertRaisesRegex(ValueError, "較新的事實"):
            await self.manager.apply(old[0], [old[1]], set(), {0: VECTOR})
        self.assertEqual([row["canonical_text"] for row in await self.repo.related_items("咖啡", VECTOR)], ["使用者現在喜歡咖啡"])

    async def test_reset_rejects_librarian_already_running(self):
        job, decision = await self._reviewed("reset-ready")
        await self.repo.reset()
        self.assertFalse(await self.manager.apply(job, [decision], set(), {0: VECTOR}))
        self.assertEqual((await self._query("SELECT count(*) FROM memory_items"))[0][0], 0)

    async def test_forget_preserves_unrelated_waiting_input_and_handles_null_review(self):
        item = await self._create()
        waiting = await self.repo.accept("another", "held")
        await self.repo.route(waiting, MemoryRouting("needs_context"), "旅遊目的地尚未決定", [], VECTOR)
        dismissed = await self._job("dismissed", "你記得我嗎")
        await self.repo.complete_intake(dismissed, {"route": "none", "reason": "question"},
                                        await self.repo.intake_sources(dismissed, []), [], {})
        # 舊版接收曾保存 JSON null；遺忘不能因其他工作的內容型別失敗。
        await self._query("UPDATE memory_jobs SET reviewed_candidates = 'null'::jsonb WHERE id = %s", (dismissed["id"],))
        job = await self._job("forget-safe", "忘記我的茶偏好")
        await self._apply(job, [{"action": "FORGET", "target_memory_ids": [str(item["id"])], "reason": "request"}], {item["id"]}, {})
        self.assertEqual((await self._query("SELECT raw_text FROM memory_sources WHERE id = %s", (waiting,)))[0][0], "旅遊目的地尚未決定")
        self.assertEqual((await self._query("SELECT status FROM memory_jobs WHERE id = %s", (waiting,)))[0][0], "buffered")

    async def test_intake_role_cannot_commit_formal_facts(self):
        job = await self._job("unauthorized")
        decision = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
                    "target_memory_ids": [], "reason": "test", "importance": .7, "confidence": .8, "retention_class": "normal"}
        with self.assertRaisesRegex(ValueError, "持久化接收契約"):
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
                row = connection.execute(text(f'SELECT route,stage,source_ids FROM "{schema}".memory_jobs')).one()
                self.assertEqual((row[0], row[1], row[2]), (None, "intake", [event]))
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
        job, decision = await self._reviewed("restart-lease")
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

    async def test_forget_cancels_older_uncommitted_duplicate(self):
        job, decision = await self._reviewed("old-pending", "使用者喜歡茶", "preference.tea")
        saved, fact = await self._reviewed("new-saved", "使用者喜歡茶", "preference.tea")
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


if __name__ == "__main__":
    unittest.main()
