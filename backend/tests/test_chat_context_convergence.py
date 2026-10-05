"""短期上下文摘要與背景協調的純單元驗收。"""

import asyncio
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.chat_service import build_chat_context, generate_context_summary
from services.chat_session_service import ChatSessionService


class ChatContextConvergenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_prompt_contains_previous_summary_and_new_batch(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="合併後摘要"))],
        )
        with patch("services.chat_service.chat_create_with_fallback",
                   new=AsyncMock(return_value=response)) as create:
            result = await generate_context_summary(
                "早期重要事實",
                [{"role": "user", "content": "新的更正", "created_at": "2026-10-05T00:00:00+00:00"}],
            )
        self.assertEqual(result, "合併後摘要")
        supplied = create.await_args.kwargs["messages"][1]["content"]
        self.assertIn("早期重要事實", supplied)
        self.assertIn("新的更正", supplied)
        self.assertIn("untrusted_previous_summary", supplied)

    async def test_invalid_summary_does_not_become_a_successful_result(self):
        response = SimpleNamespace(choices=[])
        with patch("services.chat_service.chat_create_with_fallback",
                   new=AsyncMock(return_value=response)):
            with self.assertRaisesRegex(ValueError, "有效內容"):
                await generate_context_summary("舊摘要", [{"role": "user", "content": "一批對話"}])

    def test_context_no_longer_has_an_independent_sixteen_message_cutoff(self):
        history = [
            {"role": "user", "content": f"問題 {index}"}
            for index in range(20)
        ]
        context = build_chat_context("角色設定", history, "本輪問題", budget=4096)
        self.assertEqual(len(context), 22)
        self.assertEqual(context[1]["content"], "問題 0")
        self.assertEqual(context[-2]["content"], "問題 19")

    async def test_compression_service_retries_without_a_new_chat_input(self):
        service = ChatSessionService(SimpleNamespace())
        calls = []

        async def worker(force):
            calls.append(force)
            if len(calls) == 1:
                raise RuntimeError("temporary provider failure")
            return False

        with patch("services.chat_session_service.CHAT_COMPRESSION_RETRY_INTERVAL_SEC", 0.01):
            task = await service.ensure_compression(worker, force=True)
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(calls, [True, False])
        await service.stop_compression()

    async def test_compression_service_keeps_one_inflight_task(self):
        service = ChatSessionService(SimpleNamespace())
        started = asyncio.Event()
        release = asyncio.Event()

        async def worker(_force):
            started.set()
            await release.wait()
            return False

        first = await service.ensure_compression(worker)
        second = await service.ensure_compression(worker, force=True)
        self.assertIsNone(second)
        await started.wait()
        release.set()
        await first
        self.assertEqual(service._compression_task, None)


if __name__ == "__main__":
    unittest.main()
