"""合成前置資料、向量基準及外部來源隔離，不呼叫受測模型。"""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import dotenv_values
from sqlalchemy import text
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from domain.memory_settings import MemorySettings
from domain.memory_scope import MemoryScope
from services.ai_test_context import active_fixture, fixture_timestamp, reject_fixture_image
from services.context_tools import ContextTools
from tools.ai_test_fixtures import DocumentVectors, FixtureLoader

ROOT = Path(__file__).resolve().parents[2]
VECTOR = [1.0] + [0.0] * 1023


def settings():
    return MemorySettings("postgresql://localhost/fixture_test", MemoryScope(uuid4(), uuid4(), "test_" + uuid4().hex),
        "unit-secret", "https://fixture.invalid", "memory", "unit-secret", "https://fixture.invalid", "embedding",
        "serving-alias", 1024, "query: ", "document: ")


class VectorCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_documents_are_generated_once_then_reused_and_drift_fails(self):
        memories = [{"id": "background", "text": "合成背景", "subject_key": "project.test"}]
        client = SimpleNamespace(embed=AsyncMock(return_value=VECTOR), client=SimpleNamespace(close=AsyncMock()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vectors.json"
            config = settings()
            first = DocumentVectors(path, config, lambda _: client)
            self.assertEqual((await first.load(memories))["background"], VECTOR)
            client.embed.assert_awaited_once_with("合成背景", query=False)
            client.client.close.assert_awaited_once()
            second = DocumentVectors(path, config, lambda _: self.fail("cache hit must not create client"))
            await second.load(memories)
            self.assertEqual(second.cost["calls"], 0)
            self.assertEqual(second.cost["cache_hits"], 1)
            await second.load([{**memories[0], "valid_from_days": -14}])
            with self.assertRaisesRegex(ValueError, "fixture_invalid"):
                await second.load([{**memories[0], "text": "改變的背景"}])
            changed = settings()
            changed = SimpleNamespace(**{**changed.__dict__, "embedding_contract": "different"})
            with self.assertRaisesRegex(ValueError, "fixture_invalid"):
                await DocumentVectors(path, changed).load(memories)

    async def test_invalid_vector_cannot_be_used_as_fixed_document(self):
        memories = [{"id": "background", "text": "合成背景"}]
        client = SimpleNamespace(embed=AsyncMock(return_value=VECTOR), client=SimpleNamespace(close=AsyncMock()))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vectors.json"
            config = settings()
            await DocumentVectors(path, config, lambda _: client).load(memories)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["documents"]["background"]["vector"][0] = 2.0
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "normalization"):
                await DocumentVectors(path, config).load(memories)


class RuntimeFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "fixture.json"
        self.schema = "test_" + uuid4().hex
        self.reference = datetime.now(ZoneInfo("Asia/Taipei")).replace(microsecond=0).isoformat()
        self.environment = patch.dict(os.environ, {"AI_VT_TEST_MODE": "true",
            "MEMORY_DATABASE_SCHEMA": self.schema, "AI_VT_TEST_FIXTURE_PATH": str(self.path)})
        self.environment.start()
        self.write_manifest()

    async def asyncTearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def write_manifest(self, **kwargs):
        manifest = {"version": 1, "schema": self.schema, "suite": "tools", "case_id": "to_005",
                    "reference_time": self.reference, "tools": {}, "snapshot": None, "vision_rejection": False}
        manifest.update(kwargs)
        self.path.write_text(json.dumps(manifest), encoding="utf-8")

    async def test_production_ignores_manifest_and_integration_reads_live(self):
        with patch.dict(os.environ, {"AI_VT_TEST_MODE": "false"}):
            self.assertIsNone(active_fixture())
            self.assertEqual(fixture_timestamp(123), 123)
        self.write_manifest(suite="integration", case_id="it_010")
        reader = SimpleNamespace(read=AsyncMock(return_value={"status": "unsupported", "reason": "windows_required"}))
        receipt = await ContextTools(desktop_allowed=True, reader=reader)._execute("capture_screenshot", {})
        self.assertEqual(receipt["reason"], "windows_required")
        reader.read.assert_awaited_once_with("screenshot", timeout=2, target="foreground")
        self.write_manifest(suite="integration", tools={"get_datetime": {"status": "ok", "data": {}}})
        with self.assertRaisesRegex(ValueError, "requires live"):
            active_fixture()

    async def test_fixture_snapshot_keeps_title_out_and_missing_tool_never_reads_private_screen(self):
        self.write_manifest(snapshot={"status": "ok", "data": {
            "foreground": {"status": "ok", "app": "editor", "title": "synthetic title"},
            "open_apps": {"status": "ok", "apps": ["editor"], "truncated": False}}})
        reader = SimpleNamespace(read=AsyncMock(side_effect=AssertionError("private source called")))
        tools = ContextTools(desktop_allowed=True, reader=reader)
        snapshot = await tools.snapshot("turn", fixture_timestamp(123))
        self.assertNotIn("title", snapshot["foreground"])
        self.assertEqual(snapshot["message_time"], self.reference)
        receipt = await tools.execute("capture_screenshot", "{}")
        self.assertEqual(receipt["reason"], "fixture_tool_unavailable")
        reader.read.assert_not_awaited()
        denied = ContextTools(desktop_allowed=False, reader=reader)
        self.assertEqual((await denied.execute("capture_screenshot", "{}"))["reason"], "unknown_tool")

    async def test_synthetic_image_receipt_and_report_asset_share_actual_payload_hash(self):
        self.write_manifest(tools={"capture_screenshot": {"status": "ok",
            "asset": "backend/tools/ai_test_cases/assets/desktop.png"}})
        receipt = await ContextTools(desktop_allowed=True).execute("capture_screenshot", "{}")
        self.assertEqual(receipt["status"], "ok")
        asset = self.path.parent / receipt["data"]["source"]["report_asset"]
        self.assertEqual(asset.read_bytes(), receipt["image"])
        self.assertEqual(hashlib.sha256(asset.read_bytes()).hexdigest(), receipt["data"]["image_sha256"])
        self.assertEqual(receipt["data"]["source"]["kind"], "fixture_image")

    async def test_rejection_is_test_only_and_runs_existing_receipt_replacement(self):
        from services.chat_service import stream_agent_a
        from backend.tests.test_context_tools import native, selection, FakeStream
        self.write_manifest(vision_rejection=True, case_id="to_007", tools={"capture_screenshot": {
            "status": "ok", "asset": "backend/tools/ai_test_cases/assets/desktop.png"}})
        events = []
        model = AsyncMock(side_effect=[selection([native("capture_screenshot")], finish="tool_calls"),
                                      FakeStream("本輪圖片無法判讀")])
        with patch("services.chat_service.trace", side_effect=lambda stage, data: events.append((stage, data))), \
                patch("services.chat_service.chat_create_with_fallback", model):
            reply = await stream_agent_a([{"role": "system", "content": "角色"},
                                          {"role": "user", "content": "再看一次"}], AsyncMock(),
                                         context_tools=ContextTools(desktop_allowed=True))
        self.assertIn("無法判讀", reply)
        self.assertEqual(model.await_count, 2)
        sent = model.await_args_list[-1].kwargs["messages"]
        self.assertTrue(any(m.get("role") == "tool" and "vision_not_supported" in m["content"] for m in sent))
        self.assertFalse(reject_fixture_image(sent))
        self.assertTrue(any(stage == "context_test_fault" for stage, _ in events))
        delivery = next(data for stage, data in events if stage == "context_tool_delivery")
        self.assertFalse(delivery["image_accepted"])


class LoaderIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_loader_requires_created_owned_test_schema_and_preserves_dependencies(self):
        with self.assertRaisesRegex(ValueError, "owned test schema"):
            FixtureLoader(SimpleNamespace(schema="public", created=True))
        store = SimpleNamespace(schema="test_" + uuid4().hex, created=True,
                                owner={"user_id": uuid4(), "character_id": uuid4()})
        loader = FixtureLoader(store)
        reference = datetime.now(ZoneInfo("Asia/Taipei")).isoformat()
        dataset = {"suite": "memory", "reference_time": reference, "memories": []}
        context = {"scenario": "pair", "memory_ids": []}
        with patch.object(loader, "_seed_memories") as seed, patch.object(loader, "_seed_session") as session:
            first = await loader.prepare({"case_id": "mem_003", "session": "A", "context": context}, dataset)
            second = await loader.prepare({"case_id": "mem_005", "session": "A", "context": context}, dataset)
            self.assertEqual(first["session_id"], second["session_id"])
            self.assertEqual(seed.call_count, 1)
            self.assertEqual(session.call_count, 1)
            await loader.prepare({"case_id": "mem_006", "session": "B", "context": context}, dataset)
            self.assertEqual(seed.call_count, 1)
            self.assertEqual(session.call_count, 2)
            await loader.prepare({"case_id": "mem_007", "session": "C", "context": {"scenario": "next"}}, dataset)
            with self.assertRaisesRegex(ValueError, "earlier scenario"):
                await loader.prepare({"case_id": "mem_003", "session": "A", "context": context}, dataset)


_env = dotenv_values(ROOT / ".env")
_test_url = os.getenv("MEMORY_TEST_DATABASE_URL", _env.get("MEMORY_TEST_DATABASE_URL")) or ""
_production_url = os.getenv("MEMORY_DATABASE_URL", _env.get("MEMORY_DATABASE_URL")) or ""
_safe_database = bool(_test_url and _production_url and
                      make_url(_test_url).database != make_url(_production_url).database)


@unittest.skipUnless(_safe_database, "需要與正式 DB 不同的 MEMORY_TEST_DATABASE_URL")
class FixtureDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixed_memory_scenarios_seed_legal_complete_provenance(self):
        from tools.chat_test_cli import MemoryRunStore
        from tools.ai_testset import load_suite
        store = MemoryRunStore(_test_url, "test_" + uuid4().hex, uuid4(), uuid4())
        store.open()
        try:
            dataset = load_suite("memory")
            loader = FixtureLoader(store, settings=settings())
            # 只驗證fixture/DB契約；此固定向量不代表模型召回能力。
            loader._vectors = {item["id"]: VECTOR for item in dataset["memories"]}
            for case in dataset["cases"]:
                await loader.prepare(case, dataset)
            with store.engine.connect() as connection:
                rows = connection.execute(text(f'SELECT count(*) FROM "{store.schema}".memory_items i '
                    f'WHERE NOT EXISTS (SELECT 1 FROM "{store.schema}".memory_evidence e '
                    'WHERE (e.memory_id,e.user_id,e.character_id)=(i.id,i.user_id,i.character_id))')).scalar_one()
                self.assertEqual(rows, 0)
                bad_source = connection.execute(text(f'SELECT count(*) FROM "{store.schema}".memory_sources '
                    "WHERE speaker <> 'user' OR raw_text IS NULL")).scalar_one()
                self.assertEqual(bad_source, 0)
                future_observed = connection.execute(text(f'SELECT count(*) FROM "{store.schema}".memory_items '
                    'WHERE observed_at > now()')).scalar_one()
                self.assertEqual(future_observed, 0)
            self.assertTrue(loader.other_owner_state()["memory_items"])
        finally:
            store.close()

    async def test_seed_rows_follow_schema_and_dependency_recall_does_not_restore_deleted_target(self):
        from tools.chat_test_cli import MemoryRunStore
        store = MemoryRunStore(_test_url, "test_" + uuid4().hex, uuid4(), uuid4())
        store.open()
        client = SimpleNamespace(embed=AsyncMock(return_value=VECTOR), client=SimpleNamespace(close=AsyncMock()))
        memories = [
            {"id": "old", "text": "以前飲料偏好是無糖茶", "subject_key": "preference.drink",
             "memory_type": "preference", "status": "superseded", "owner": "self", "valid_to_days": -30},
            {"id": "new", "text": "目前飲料偏好是低甜咖啡", "subject_key": "preference.drink",
             "memory_type": "preference", "status": "active", "owner": "self", "supersedes_id": "old"},
            {"id": "other", "text": "他人偏好是甜茶", "subject_key": "preference.drink",
             "memory_type": "preference", "status": "active", "owner": "other"},
        ]
        dataset = {"suite": "memory", "reference_time": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
                   "memories": memories}
        try:
            with tempfile.TemporaryDirectory() as directory:
                loader = FixtureLoader(store, vector_path=Path(directory) / "vectors.json", settings=settings(),
                                       client_factory=lambda _: client)
                case = {"case_id": "mem_007", "session": "A", "context": {"scenario": "forget",
                    "memory_ids": ["old", "new", "other"], "summary": "固定合成摘要",
                    "history": [{"role": "user", "content": "這是測試前置背景"},
                                {"role": "assistant", "content": "收到合成背景"}]}}
                manifest = await loader.prepare(case, dataset)
                before_other = loader.other_owner_state()
                self.assertEqual(len(before_other["memory_items"]), 1)
                state = store.case_state()
                self.assertEqual(len(state["items"]), 2)
                self.assertEqual(len(state["evidence"]), 2)
                self.assertEqual(len(state["sources"]), 2)
                self.assertEqual(len(state["relations"]), 1)
                self.assertTrue(all(a["action"] == "FIXTURE_CREATE" for a in state["audit"]))
                with store.engine.begin() as connection:
                    messages = connection.execute(text(f'SELECT role,content,sequence,turn_state,memory_source '
                        f'FROM "{store.schema}".chat_messages WHERE session_id=:session ORDER BY sequence'),
                        {"session": manifest["session_id"]}).mappings().all()
                    self.assertEqual([row["role"] for row in messages], ["user", "assistant"])
                    self.assertEqual([row["sequence"] for row in messages], [0, 1])
                    self.assertEqual([row["turn_state"] for row in messages], ["completed", None])
                    self.assertTrue(all(row["memory_source"] is None for row in messages))
                    connection.execute(text(f'DELETE FROM "{store.schema}".memory_items WHERE id=:target'),
                                       {"target": loader.memory_ids["new"]})
                after_operation = store.case_state()
                await loader.prepare({"case_id": "mem_008", "session": "B", "context": {
                    "scenario": "forget", "memory_ids": ["old", "new", "other"]}}, dataset)
                self.assertEqual(store.case_state(), after_operation)
                self.assertEqual(loader.other_owner_state(), before_other)
                self.assertNotIn(loader.memory_ids["new"], store.snapshot())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
