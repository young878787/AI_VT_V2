import asyncio
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import WebSocketDisconnect

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.chat_service import _VisibleTextFilter, build_chat_context, estimate_token_count
from infrastructure import memory_store, memory_records
from services import memory_jobs
from api.routes.chat_ws import websocket_endpoint
from backend.tests.test_emotion_chat_ws import jev_answers
from services import memory_consolidation
from types import SimpleNamespace
from core.config import role_model_config, provider_from_url
from domain.input_event import normalize_chat_input


class ArchitectureIntegrationTests(unittest.TestCase):
    def test_input_normalization_rejects_invalid_session_and_user_identity(self):
        self.assertIsNone(normalize_chat_input({"content": "hello", "session_id": "../bad"}))
        event = normalize_chat_input({"content": " 你好 ", "source": "voice", "turn_id": "bad/id", "user_id": "other"})
        self.assertEqual(event["text"], "你好")
        self.assertEqual(event["source"], "voice")
        self.assertEqual(event["user_id"], "default_user")
        self.assertNotEqual(event["turn_id"], "bad/id")

    def test_role_model_uses_only_its_triplet(self):
        with patch.dict("os.environ", {
            "CHAT_AI_API_KEY": "chat-key",
            "CHAT_AI_BASE_URL": "https://integrate.api.nvidia.com/v1",
            "CHAT_AI_MODEL": "test-chat",
        }):
            provider, key, url, model = role_model_config("CHAT")
            self.assertEqual((provider, key, url, model), (
                "nvidia", "chat-key", "https://integrate.api.nvidia.com/v1", "test-chat"
            ))
            with patch.dict("os.environ", {"CHAT_AI_API_KEY": ""}):
                with self.assertRaisesRegex(RuntimeError, "CHAT_AI_API_KEY"):
                    role_model_config("CHAT")
        self.assertEqual(provider_from_url("https://dashscope-intl.aliyuncs.com/compatible-mode/v1"), "qwen")
        self.assertEqual(provider_from_url("https://gateway.example/v1"), "custom")

    def test_chat_context_has_fixed_budget_and_keeps_latest_input(self):
        history = [{"role": "user", "content": "很久以前" * 8000},
                   {"role": "assistant", "content": "舊回覆" * 8000}]
        context = build_chat_context("角色設定" * 3000, history, "本輪問題" * 3000, budget=512)
        self.assertLessEqual(estimate_token_count(context), 512)
        self.assertEqual(context[-1]["role"], "user")
        self.assertTrue(context[-1]["content"].startswith("本輪問題"))

    def test_stream_filter_hides_split_tags_and_reasoning(self):
        filtered = _VisibleTextFilter()
        chunks = ["你好<thi", "nk>秘密", "</think>，", "今天<shy_sta", "te>0.8</shy_state>很開心"]
        output = "".join(filtered.feed(chunk) for chunk in chunks) + filtered.finish()
        self.assertEqual(output, "你好，今天很開心")

    def test_memory_operation_replay_and_reset_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            note_path = str(pathlib.Path(directory) / "memory.md")
            record_path = str(pathlib.Path(directory) / "memory_records.json")
            job_dir = str(pathlib.Path(directory) / "jobs")
            pathlib.Path(job_dir).mkdir()
            with patch.object(memory_records, "MEMORY_MD_PATH", note_path), \
                 patch.object(memory_records, "RECORDS_PATH", record_path), \
                 patch.object(memory_jobs, "JOB_DIR", job_dir), \
                 patch.object(memory_jobs, "EPOCH_PATH", str(pathlib.Path(job_dir) / "epoch.json")):
                memory_records.append_record_once("喜歡咖啡", "op1", "turn1")
                memory_records.append_record_once("喜歡咖啡", "op1", "turn1")
                memory_records.append_record_once("下次再說", "op2", "turn1", {"ttl": "session"})
                memory_records.append_record_once("生日是五月", "op3", "turn1", {
                    "memory_type": "special", "importance": 0.2, "ttl": "long",
                })
                self.assertEqual(len(memory_records.load_records()), 2)
                self.assertTrue(memory_records.load_records()[1]["protected"])
                self.assertEqual(memory_records.load_records()[1]["importance"], 1.0)
                self.assertIn("喜歡咖啡", memory_records.search_relevant_records("咖啡", max_items=1))
                self.assertEqual(memory_records.load_records()[0]["access_count"], 1)
                self.assertEqual(pathlib.Path(note_path).read_text().count("喜歡咖啡"), 1)
                old_epoch = memory_jobs.current_epoch()
                self.assertEqual(memory_jobs.reset_epoch(), old_epoch + 1)
                path = str(pathlib.Path(job_dir) / "old.json")
                pathlib.Path(path).write_text(json.dumps({"status": "pending", "epoch": old_epoch}))
                asyncio.run(memory_jobs.process_job(path))
                self.assertEqual(memory_records.load_records()[0]["text"], "喜歡咖啡")
            memory_store._memory_cache = None

    def test_new_turn_cancels_stale_jev_result(self):
        started = asyncio.Event()
        completed = asyncio.Event()
        payloads = []

        class Socket:
            index = 0

            async def accept(self):
                pass

            async def receive_text(self):
                self.index += 1
                if self.index == 1:
                    return json.dumps({"content": "第一句", "turn_id": "turn_1"})
                if self.index == 2:
                    await started.wait()
                    return json.dumps({"content": "第二句", "turn_id": "turn_2"})
                await completed.wait()
                raise WebSocketDisconnect()

            async def send_json(self, payload):
                payloads.append(payload)
                if payload.get("type") == "stream_end" and payload.get("turn_id") == "turn_2":
                    completed.set()

        async def fake_jev(context, questions):
            if context["current_user_input"] == "第一句":
                started.set()
                await asyncio.Event().wait()
            return jev_answers(0.7)

        async def fake_chat(messages, send_chunk):
            await send_chunk("第二句回覆")
            return "第二句回覆"

        async def run():
            with patch("api.routes.chat_ws.call_jev", side_effect=fake_jev), \
                 patch("api.routes.chat_ws.stream_agent_a", side_effect=fake_chat), \
                 patch("api.routes.chat_ws.enqueue_input", return_value="event"), \
                 patch("api.routes.chat_ws.broadcast_to_displays"), \
                 patch("api.routes.chat_ws.log_turn"), \
                 patch("api.routes.chat_ws.synthesize_and_send_voice"), \
                 patch("api.routes.chat_ws.load_user_profile", return_value={}), \
                 patch("api.routes.chat_ws.search_relevant_records", return_value=""):
                await websocket_endpoint(Socket())

        asyncio.run(run())
        self.assertEqual([p["turn_id"] for p in payloads if p["type"] == "text_stream"], ["turn_2"])
        self.assertIn("turn_1", [p["turn_id"] for p in payloads if p["type"] == "turn_cancelled"])

    def test_consolidation_cannot_restore_memory_after_reset(self):
        records = [{"id": str(index), "text": f"事實{index}", "status": "active"} for index in range(3)]
        async def fake_create(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="整理結果"))])
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(memory_consolidation, "SUMMARY_PATH", str(pathlib.Path(directory) / "summary.json")), \
             patch.object(memory_consolidation, "load_records", return_value=records), \
             patch.object(memory_consolidation, "chat_create_with_fallback", side_effect=fake_create):
            self.assertFalse(asyncio.run(memory_consolidation.consolidate_memory(1, lambda: 2)))
            self.assertFalse(pathlib.Path(memory_consolidation.SUMMARY_PATH).exists())
            self.assertTrue(asyncio.run(memory_consolidation.consolidate_memory(2, lambda: 2)))
            summary = json.loads(pathlib.Path(memory_consolidation.SUMMARY_PATH).read_text())
            self.assertEqual(summary["source_ids"], ["0", "1", "2"])


if __name__ == "__main__":
    unittest.main()
