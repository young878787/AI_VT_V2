"""Memory 多輪傳輸與 worker 的 deadline、故障及取消回歸。"""
import asyncio
import json
import os
import signal
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from openai import BadRequestError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from services import memory_agent_client, memory_worker
from services.memory_agent_client import MemoryAgentClient, CUMULATIVE_DIAGNOSTIC_FIELDS
from services.memory_worker import MemoryWorker


async def blocked(*args, **kwargs):
    await asyncio.Event().wait()


class MemoryTransportStabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.agent = object.__new__(MemoryAgentClient)
        self.agent.model, self.agent.provider = "test-model", "custom"
        self.create = AsyncMock(side_effect=blocked)
        self.agent.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)))
        self.diag = dict.fromkeys(CUMULATIVE_DIAGNOSTIC_FIELDS, 0)
        self.messages = [{"role": "user", "content": "synthetic"}]

    async def test_model_timeout_is_wall_clock_and_keeps_cost(self):
        with patch.object(memory_agent_client, "CALL_TIMEOUT_SEC", .02):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(self.agent.call(self.messages, [], self.diag), 1)
        self.assertEqual(self.diag["calls"], 1)
        self.assertGreater(self.diag["input_token_estimate"], 0)
        self.assertGreater(self.diag["latency_ms"], 0)

    async def test_parameter_resend_shares_deadline_and_counts_both_requests(self):
        error = BadRequestError("set reasoning_effort to 'none'", response=httpx.Response(
            400, request=httpx.Request("POST", "https://example.invalid/v1")),
            body={"param": "reasoning_effort"})
        async def first_then_block(**kwargs):
            if self.create.await_count == 1:
                await asyncio.sleep(.015)
                raise error
            await blocked()
        self.create.side_effect = first_then_block
        with patch.object(memory_agent_client, "CALL_TIMEOUT_SEC", .03):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(self.agent.call(self.messages, [], self.diag), 1)
        self.assertEqual(self.diag["calls"], 2)
        self.assertEqual(self.create.call_args.kwargs["reasoning_effort"], "none")
        single = dict.fromkeys(CUMULATIVE_DIAGNOSTIC_FIELDS, 0)
        self.create.side_effect = blocked
        with patch.object(memory_agent_client, "CALL_TIMEOUT_SEC", .01):
            with self.assertRaises(TimeoutError):
                await self.agent.call(self.messages, [], single)
        self.assertEqual(self.diag["input_token_estimate"], single["input_token_estimate"] * 2)

    async def test_other_bad_request_is_not_resent(self):
        self.create.side_effect = BadRequestError("invalid model", response=httpx.Response(
            400, request=httpx.Request("POST", "https://example.invalid/v1")), body={"param": "model"})
        with self.assertRaises(BadRequestError):
            await self.agent.call(self.messages, [], self.diag)
        self.create.assert_awaited_once()

    async def test_input_budget_does_not_send_and_output_truncation_fails(self):
        with self.assertRaisesRegex(ValueError, "input budget"):
            await self.agent.call([{"role": "user", "content": "token " * 9000}], [], self.diag)
        self.create.assert_not_awaited()
        self.create.side_effect = None
        self.create.return_value = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1024),
                                                  choices=[SimpleNamespace(finish_reason="length")])
        with self.assertRaisesRegex(ValueError, "output incomplete"):
            await self.agent.call(self.messages, [], self.diag)
        self.assertEqual(self.diag["output_tokens"], 1024)


class MemoryWorkerStabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.job = {"id": uuid4(), "message_id": uuid4(), "source_text": "我喜歡茶", "attempts": 1,
                    "agent_diagnostics": {"logical_steps": 4, "calls": 4}}
        self.repo = SimpleNamespace(
            claim=AsyncMock(return_value=self.job), queue_health=AsyncMock(return_value={"active_jobs": 1}),
            context_jobs=AsyncMock(return_value=[]), job_sources=AsyncMock(return_value=[]),
            agent_candidates=AsyncMock(return_value=[]), mark_pending_targets=AsyncMock(),
            finish=AsyncMock(return_value=True))
        self.embedding = SimpleNamespace(embed=AsyncMock(return_value=[1.0] + [0.0] * 1023))
        self.result = {"outcome": "complete", "decisions": [{"action": "CREATE", "canonical_text": "使用者喜歡茶",
                       "source_ids": [str(self.job["id"])]}], "targets": {}}
        self.llm = SimpleNamespace(decide=AsyncMock(return_value=self.result))
        self.manager = SimpleNamespace(apply=AsyncMock(return_value=True))
        self.worker = MemoryWorker(self.repo, self.embedding, self.llm, self.manager)

    async def test_attempt_timeout_after_proposal_never_commits(self):
        async def pending(*args):
            args[-1].update(logical_steps=5, proposal_count=1)
            await blocked()
        self.llm.decide.side_effect = pending
        with patch.object(memory_worker, "ATTEMPT_TIMEOUT_SEC", .02):
            self.assertTrue(await asyncio.wait_for(self.worker.process_one(), 1))
        self.manager.apply.assert_not_awaited()
        args, kwargs = self.repo.finish.call_args
        self.assertEqual(args[1], "retry")
        self.assertEqual(kwargs["error"], "TimeoutError")
        self.assertEqual(kwargs["diagnostic"]["logical_steps"], 5)
        self.assertEqual(kwargs["diagnostic"]["proposal_count"], 1)
        self.assertEqual(kwargs["diagnostic"]["timeouts"], 1)
        self.assertEqual(kwargs["diagnostic"]["phase"], "agent")

    async def test_claim_and_failure_finalization_are_bounded(self):
        self.repo.claim.side_effect = blocked
        with patch.object(memory_worker, "DB_TIMEOUT_SEC", .01):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(self.worker.process_one(), 1)
        self.llm.decide.assert_not_awaited()
        self.repo.claim.side_effect = None
        self.llm.decide.side_effect = RuntimeError("offline")
        self.repo.finish.side_effect = blocked
        with patch.object(memory_worker, "DB_TIMEOUT_SEC", .01):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(self.worker.process_one(), 1)
        self.manager.apply.assert_not_awaited()

    async def test_late_claim_reserves_lease_time_for_failure_finalization(self):
        self.job["lease_until"] = datetime.now(timezone.utc) + timedelta(seconds=1)
        await self.worker.process_one()
        self.llm.decide.assert_not_awaited()
        self.manager.apply.assert_not_awaited()
        self.assertEqual(self.repo.finish.call_args.kwargs["diagnostic"]["attempt_budget_sec"], 0)
        self.assertEqual(self.repo.finish.call_args.kwargs["error"], "TimeoutError")

    async def test_commit_rejection_attempts_guarded_retry_without_success_event(self):
        self.manager.apply.return_value = False
        with patch.object(memory_worker, "publish_memory_event") as publish:
            await self.worker.process_one()
        publish.assert_not_called()
        self.assertEqual(self.repo.finish.call_args.args[1], "retry")
        self.assertEqual(self.repo.finish.call_args.kwargs["error"], "CommitRejectedError")

    async def test_embedding_failure_defers_all_mutations(self):
        self.embedding.embed.side_effect = RuntimeError("offline")
        await self.worker.process_one()
        self.assertTrue(self.job["retrieval_degraded"])
        self.manager.apply.assert_not_awaited()
        self.assertEqual(self.repo.finish.call_args.args[1], "retry")

    async def test_document_embedding_failure_never_applies(self):
        self.embedding.embed.side_effect = [[1.0] + [0.0] * 1023, RuntimeError("offline")]
        await self.worker.process_one()
        self.manager.apply.assert_not_awaited()
        self.assertEqual(self.repo.finish.call_args.kwargs["diagnostic"]["phase"], "document_embedding")

    async def test_needs_context_discards_all_proposals(self):
        self.result.update(outcome="needs_context", reason="主體不明")
        await self.worker.process_one()
        self.manager.apply.assert_not_awaited()
        self.embedding.embed.assert_awaited_once()
        self.assertEqual(self.repo.finish.call_args.args[1], "buffered")

    async def test_retry_exhaustion_preserves_cumulative_cost(self):
        self.job["attempts"] = 3
        self.llm.decide.side_effect = ValueError("invalid proposal")
        await self.worker.process_one()
        self.assertEqual(self.repo.finish.call_args.args[1], "failed")
        diag = self.repo.finish.call_args.kwargs["diagnostic"]
        self.assertTrue(diag["retry_exhausted"])
        self.assertEqual((diag["calls"], diag["logical_steps"], diag["failures"]), (4, 4, 1))

    async def test_stop_cancels_inflight_agent_without_partial_commit(self):
        entered = asyncio.Event()
        async def pending(*args):
            entered.set()
            await blocked()
        self.llm.decide.side_effect = pending
        self.worker.start()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(self.worker.stop(), 1)
        self.repo.finish.assert_not_awaited()
        self.manager.apply.assert_not_awaited()


@unittest.skipUnless(os.getenv("MEMORY_STABILITY_LIVE") == "1", "固定 Chat 長跑需 MEMORY_STABILITY_LIVE=1")
class MemoryChatSoakTests(unittest.IsolatedAsyncioTestCase):
    async def test_three_replays_keep_db_prompt_and_semantic_evidence(self):
        from tools import chat_test_cli as cli
        from tools.memory_testset import load_snapshot, fingerprint
        cli.test_database_url()
        backend = pathlib.Path(__file__).resolve().parents[1]
        cases = load_snapshot(str(backend / "log" / "chat_test_runs" / "latest" / "cases.json"))
        output = cli.create_run_dir(backend / "log" / "memory_stability")
        cli.atomic_write_text(output / "cases.json", json.dumps(cases, ensure_ascii=False, indent=2))
        for name in ("turns.jsonl", "case_states.jsonl"):
            (output / name).write_text("", encoding="utf-8")
        manifest = {"status": "running", "started_at": cli.timestamp(), "cases_sha256": fingerprint(cases),
                    "planned_cycles": 3, "cycles": []}
        def persist():
            manifest["updated_at"] = cli.timestamp()
            cli.atomic_write_text(output / "run.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            lines = ["# Memory 固定 Chat 重播穩定性", "", f"- 狀態：{manifest['status']}；案例 SHA-256：`{manifest['cases_sha256']}`",
                     "- 每輪沿用同一 28-case 快照、相同業務入口及獨立 DB schema；每案 reset 隔離。",
                     "- DB／prompt／回答詳查 turns.jsonl 及 case_states.jsonl 的 cycle＋case_id＋turn。",
                     "- 這是三輪循序重播，沒有模擬持續到達負載；完成與語意判定分開，不推定正式 SLO。", "",
                     "| Cycle | Status | Cases／Turns | Agent jobs／Steps／HTTP | Timeout／Failures | 語意評分 | Cleanup |",
                     "|---|---|---|---|---|---|---|"]
            for row in manifest["cycles"]:
                m, s = row["memory_stability"], row.get("semantic_evaluation", {})
                lines.append(f"| {row['cycle']} | {row['status']} | {row['executed_cases']}/{row['executed_turns']} | "
                             f"{m['agent_jobs']}/{m['totals']['logical_steps']}/{m['totals']['calls']} | "
                             f"{m['totals']['timeouts']}/{m['totals']['failures']} | {s.get('counts')} | {row['cleanup']} |")
            cli.atomic_write_text(output / "memory_report.md", "\n".join(lines) + "\n")
        persist()
        try:
            for cycle in range(1, 4):
                process = await asyncio.create_subprocess_exec(sys.executable, str(backend / "tools" / "chat_test_cli.py"),
                                                               "--scenario", str(output / "cases.json"))
                try:
                    returncode = await process.wait()
                except BaseException:
                    process.send_signal(signal.SIGINT)
                    await process.wait()
                    raise
                directory = backend / "log" / "chat_test_runs" / "latest"
                result = json.loads((directory / "run.json").read_text(encoding="utf-8"))
                status = result["status"]
                if returncode not in (0, 1) or status not in {"completed", "failed"}:
                    raise RuntimeError("Chat 重播未正常結案")
                manifest["cycles"].append({"cycle": cycle, **{key: result.get(key) for key in (
                    "status", "started_at", "updated_at", "executed_cases", "executed_turns", "memory_stability",
                    "hard_checks", "semantic_evaluation", "cleanup", "error", "chat_model", "jev_model", "memory_model",
                    "embedding_model")}})
                for name in ("turns.jsonl", "case_states.jsonl"):
                    with (output / name).open("a", encoding="utf-8") as destination:
                        for line in (directory / name).read_text(encoding="utf-8").splitlines():
                            destination.write(json.dumps({"cycle": cycle, **json.loads(line)}, ensure_ascii=False) + "\n")
                persist()
                print(f"[Memory stability] cycle {cycle}: {status}", flush=True)
            manifest["status"] = "completed" if all(row["status"] == "completed" for row in manifest["cycles"]) else "failed"
        except BaseException:
            manifest["status"] = "failed"
            raise
        finally:
            persist()
        self.assertEqual(manifest["status"], "completed", "固定重播未全部通過；詳見 memory_stability/latest/memory_report.md")


if __name__ == "__main__":
    unittest.main()
