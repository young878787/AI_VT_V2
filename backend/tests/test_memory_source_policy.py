import pathlib
import copy
import json
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4


BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.memory_routing import MemoryRouting
from domain.memory_scope import MemoryScope
from domain.memory_source import build_user_message, read_memory_source
from domain.jev_questions import build_emotion_context
from infrastructure.memory_repository import MemoryRepository
from infrastructure.memory_store import load_session_messages, save_session_messages
from services.chat_service import build_chat_context
from services.memory_runtime import MemoryRuntime


class _RecordingConnection:
    def __init__(self, conversation, created_at):
        self.conversation = conversation
        self.created_at = created_at
        self.source_insertions = []

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, statement, params=None):
        statement = str(statement)
        row = None
        rows = []
        if "SELECT conversation_id, created_at" in statement:
            row = (self.conversation, self.created_at, 0)
        if "SELECT job.id FROM memory_jobs" in statement:
            rows = [(source_id,) for source_id in params[-1]]
        if "INSERT INTO memory_sources" in statement:
            self.source_insertions.append(params)
        return SimpleNamespace(
            rowcount=1,
            fetchone=AsyncMock(return_value=row),
            fetchall=AsyncMock(return_value=rows),
        )


def _repository(connection):
    @asynccontextmanager
    async def connect():
        yield connection

    return MemoryRepository(
        SimpleNamespace(connection=connect),
        MemoryScope(uuid4(), uuid4(), "test_source_policy"),
    )


class MemorySourcePolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_corrupt_metadata_keeps_short_term_text_without_source_authority(self):
        message = build_user_message("session_1", "turn_1", "仍可作為短期文字", generation=0)
        invalid_fields = (
            ("version", True), ("policy", []), ("policy", {}),
            ("policy", "remember"), ("policy_version", "unknown"),
            ("source_id", []), ("occurred_at", {}),
            ("generation", True), ("content_sha256", "invalid"),
        )
        for field, value in invalid_fields:
            with self.subTest(field=field, value=value):
                corrupted = copy.deepcopy(message)
                corrupted["memory_source"][field] = value
                self.assertIsNone(read_memory_source(corrupted))
                with tempfile.TemporaryDirectory() as directory, patch(
                    "infrastructure.memory_store.CHAT_SESSION_DIR", directory
                ):
                    # 同時驗證已在磁碟中的損壞 metadata 與再次持久化；
                    # 不能因單筆來源失效而丟掉整份短期歷史。
                    pathlib.Path(directory, "session_1.json").write_text(
                        json.dumps([corrupted, {"role": "assistant", "content": "回覆"}]),
                        encoding="utf-8",
                    )
                    expected = [
                        {"role": "user", "content": message["content"]},
                        {"role": "assistant", "content": "回覆"},
                    ]
                    self.assertEqual(load_session_messages("session_1"), expected)
                    save_session_messages("session_1", [corrupted, expected[1]])
                    self.assertEqual(load_session_messages("session_1"), expected)

    async def test_repository_cannot_route_no_store_text_as_a_writable_source(self):
        connection = _RecordingConnection(uuid4(), datetime.now(timezone.utc))
        self.assertTrue(await _repository(connection).route(
            uuid4(), MemoryRouting(None), "甲" * 4001 + "不要記住這件事", [],
        ))
        self.assertEqual(connection.source_insertions, [])

    async def test_runtime_binds_database_generation_before_routing(self):
        event_id = uuid4()
        repository = SimpleNamespace(
            accept_event=AsyncMock(return_value=(event_id, 7)),
            route=AsyncMock(return_value=True),
        )
        runtime = MemoryRuntime(
            None, repository, None, None, None, SimpleNamespace(wake=Mock()),
        )
        message = build_user_message("session_1", "turn_1", "我喜歡茶", timestamp=1_700_000_000)

        self.assertEqual(
            await runtime.accept("session_1", "turn_1", "我喜歡茶", [], message),
            event_id,
        )
        self.assertEqual(read_memory_source(message).generation, 7)
        repository.route.assert_awaited_once()

    async def test_no_store_policy_survives_truncation_and_session_reload(self):
        for boundary in (499, 500, 501, 4000, 4001, 4002):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory, patch(
                "infrastructure.memory_store.CHAT_SESSION_DIR", directory
            ):
                text = "甲" * boundary + "不要記住這件事"
                message = build_user_message(
                    "session_1", f"turn_{boundary}", text,
                    timestamp=1_700_000_000, generation=0,
                )
                self.assertEqual(read_memory_source(message).policy, "no_store")
                save_session_messages("session_1", [message])
                restored = load_session_messages("session_1")
                self.assertEqual(restored, [message])

                created_at = datetime(2026, 10, 4, tzinfo=timezone.utc)
                connection = _RecordingConnection(uuid4(), created_at)
                event_id = uuid4()
                await _repository(connection).route(
                    event_id,
                    MemoryRouting(None, confidence=0.9),
                    "本輪可保存內容",
                    restored,
                )
                self.assertEqual(len(connection.source_insertions), 1)
                self.assertEqual(connection.source_insertions[0][0], event_id)

    async def test_history_requires_complete_metadata_and_keeps_original_identity_and_time(self):
        occurred_at = datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
        trusted = build_user_message(
            "session_1", "trusted_turn", "可信的完整歷史內容",
            timestamp=occurred_at.timestamp(), generation=0,
        )
        metadata = read_memory_source(trusted)
        old_session_message = {"role": "user", "content": "舊 session 沒有 metadata"}
        tampered = {**trusted, "content": trusted["content"] + "遭修改"}
        created_at = datetime(2026, 10, 4, 1, 2, 3, tzinfo=timezone.utc)
        connection = _RecordingConnection(uuid4(), created_at)
        event_id = uuid4()

        await _repository(connection).route(
            event_id,
            MemoryRouting(None, confidence=0.9),
            "本輪可保存內容",
            [old_session_message, tampered, trusted],
        )

        self.assertIsNone(read_memory_source(old_session_message))
        self.assertIsNone(read_memory_source(tampered))
        self.assertEqual(len(connection.source_insertions), 2)
        current, history = connection.source_insertions
        self.assertEqual((current[0], current[4], current[6]), (event_id, event_id, created_at))
        self.assertEqual(history[0], metadata.source_id)
        self.assertEqual(history[4], metadata.source_id)
        self.assertEqual(history[5], trusted["content"])
        self.assertEqual(history[6], occurred_at)

    def test_incomplete_metadata_is_not_persisted_but_short_term_text_remains(self):
        incomplete = {
            "role": "user",
            "content": "仍可作為短期聊天內容",
            "memory_source": {"version": 1, "policy": "observe"},
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "infrastructure.memory_store.CHAT_SESSION_DIR", directory
        ):
            save_session_messages("session_1", [incomplete])
            self.assertEqual(
                load_session_messages("session_1"),
                [{"role": "user", "content": "仍可作為短期聊天內容"}],
            )

    def test_source_metadata_is_not_sent_to_chat_or_jev_providers(self):
        message = build_user_message(
            "session_1", "turn_1", "只把可見文字送給模型",
            timestamp=1_700_000_000, generation=0,
        )
        chat = build_chat_context("system", [message], "下一句")
        emotion = build_emotion_context("下一句", [message], None)

        self.assertEqual(chat[1], {"role": "user", "content": message["content"]})
        self.assertEqual(
            emotion["recent_dialogue"],
            [{"role": "user", "text": message["content"]}],
        )
        self.assertNotIn("memory_source", str(chat))
        self.assertNotIn("memory_source", str(emotion))


if __name__ == "__main__":
    unittest.main()
