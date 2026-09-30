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
        await self.repo.route(event_id, MemoryRouting("process", "preference", 3, 0.9, 0.9), text, [])
        return await self.repo.claim()

    async def _create(self, turn="turn1"):
        job = await self._job(turn)
        decision = {
            "action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
            "target_memory_ids": [], "reason": "explicit user statement",
            "importance": 0.75, "confidence": 0.8, "retention_class": "normal",
        }
        self.assertTrue(await self.manager.apply(job, [decision], set(), {0: VECTOR}))
        items = await self.repo.related_items("茶", VECTOR)
        self.assertEqual(len(items), 1)
        return items[0]

    async def test_none_buffer_and_create_owner_isolation(self):
        none_id = await self.repo.accept("session", "none")
        self.assertTrue(await self.repo.route(none_id, MemoryRouting("none", confidence=0.7), "private text", []))
        buffer_id = await self.repo.accept("session", "buffer")
        self.assertTrue(await self.repo.route(
            buffer_id, MemoryRouting("buffer", "preference", 2, 0.1, 0.8), "可能喜歡茶", [], VECTOR,
        ))
        self.assertIsNone(await self.repo.claim())
        item = await self._create()
        buffers = await self.repo.related_buffers("preference", VECTOR)
        self.assertEqual(len(buffers), 1)
        other = MemoryRepository(self.pool, MemoryScope(uuid4(), self.scope.character_id, self.scope.schema_name))
        self.assertEqual(await other.accept("session", "turn1"), await self.repo.accept("session", "turn1"))
        self.assertEqual(await other.related_items("茶", VECTOR), [])
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                row = await (await connection.execute("SELECT source_text, recent_dialogue FROM memory_jobs WHERE id = %s", (none_id,))).fetchone()
                self.assertEqual(row, (None, None))
        self.assertEqual(item["status"], "active")

        job = await self._job("promotion")
        decision = {"action": "IGNORE", "target_memory_ids": [], "reason": "buffer consumed"}
        self.assertTrue(await self.manager.apply(job, [decision], set(), {}, (buffers[0]["id"],)))
        self.assertEqual(await self.repo.related_buffers("preference", VECTOR), [])

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
            self.assertTrue(await run_repo.route(event_id, MemoryRouting("buffer", "preference", 2, 0.6, 0.8),
                                                 "可能喜歡茶", [], VECTOR))
            job = await wait_memory_job(store, str(event_id), timeout=2)
            self.assertEqual((job["route"], job["status"]), ("buffer", "buffered"))
            self.assertEqual(await asyncio.to_thread(store.audit, str(event_id)), [])
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
            event_id, MemoryRouting("process", "preference", 3, 0.9, 0.9), "請記住我喜歡茶", [], VECTOR,
        ))
        decision = {
            "action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference",
            "target_memory_ids": [], "reason": "explicit statement", "importance": 0.8,
            "confidence": 0.9, "retention_class": "normal",
        }
        embedding = SimpleNamespace(embed=AsyncMock(return_value=VECTOR))
        llm = SimpleNamespace(decide=AsyncMock(return_value=[decision]))
        workers = [MemoryWorker(self.repo, embedding, llm, self.manager) for _ in range(2)]
        worked = await asyncio.gather(*(worker.process_one() for worker in workers))
        self.assertEqual(worked.count(True), 1)
        self.assertEqual(worked.count(False), 1)
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
        self.assertTrue(await self.manager.apply(job, [decision], {item["id"]}, {}))
        self.assertEqual(await self.repo.related_items("茶", VECTOR), [])
        async with self.pool.connection() as connection:
            async with connection.transaction():
                await connection.execute(sql.SQL("SET LOCAL search_path TO {}, public").format(sql.Identifier(self.scope.schema_name)))
                source_count = (await (await connection.execute("SELECT count(*) FROM memory_sources")).fetchone())[0]
                raw_jobs = (await (await connection.execute("SELECT count(*) FROM memory_jobs WHERE source_text IS NOT NULL")).fetchone())[0]
                raw_audits = (await (await connection.execute("SELECT count(*) FROM memory_audit WHERE decision IS NOT NULL")).fetchone())[0]
                self.assertEqual((source_count, raw_jobs, raw_audits), (0, 0, 0))

    async def test_reset_rejects_inflight_job(self):
        job = await self._job("race")
        await self.repo.reset()
        decision = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "memory_type": "preference", "target_memory_ids": [], "reason": "test", "importance": 0.75, "confidence": 0.8, "retention_class": "normal"}
        self.assertFalse(await self.manager.apply(job, [decision], set(), {0: VECTOR}))
        self.assertEqual(await self.repo.related_items("茶", VECTOR), [])

    async def test_reinforce_supersede_merge_contradict_archive(self):
        first = await self._create()
        target = str(first["id"])
        job = await self._job("reinforce")
        decision = {"action": "REINFORCE", "target_memory_ids": [target], "reason": "confirmed"}
        self.assertTrue(await self.manager.apply(job, [decision], {first["id"]}, {}))

        job = await self._job("supersede")
        decision = {
            "action": "SUPERSEDE", "target_memory_ids": [target], "canonical_text": "使用者現在喜歡咖啡",
            "memory_type": "preference", "reason": "new preference", "importance": 0.8,
            "confidence": 0.8, "retention_class": "normal",
        }
        self.assertTrue(await self.manager.apply(job, [decision], {first["id"]}, {0: VECTOR}))
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
        self.assertTrue(await self.manager.apply(job, [decision], set(), {0: VECTOR}))
        duplicate = next(item for item in await self.repo.related_items("咖啡", VECTOR) if item["id"] != winner["id"])
        job = await self._job("merge")
        decision = {"action": "MERGE", "target_memory_ids": [str(winner["id"]), str(duplicate["id"])], "reason": "same fact"}
        self.assertTrue(await self.manager.apply(job, [decision], {winner["id"], duplicate["id"]}, {}))
        self.assertEqual(len(await self.repo.related_items("咖啡", VECTOR)), 1)

        job = await self._job("contradict")
        decision = {
            "action": "CONTRADICT", "target_memory_ids": [str(winner["id"])],
            "canonical_text": "使用者不喜歡咖啡", "memory_type": "preference", "reason": "ambiguous conflict",
            "importance": 0.6, "confidence": 0.6, "retention_class": "normal",
        }
        self.assertTrue(await self.manager.apply(job, [decision], {winner["id"]}, {0: VECTOR}))
        self.assertEqual(len(await self.repo.related_items("咖啡", VECTOR)), 1)

        job = await self._job("archive")
        decision = {"action": "ARCHIVE", "target_memory_ids": [str(winner["id"])], "reason": "no longer current"}
        self.assertTrue(await self.manager.apply(job, [decision], {winner["id"]}, {}))
        self.assertEqual(await self.repo.related_items("咖啡", VECTOR), [])
        history = await self.repo.related_items("咖啡", VECTOR, mode="history")
        self.assertEqual({item["status"] for item in history}, {"superseded", "archived"})


if __name__ == "__main__":
    unittest.main()
