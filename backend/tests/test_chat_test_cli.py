"""分組入口與狀態重播測試；所有模型、DB、WebSocket及子程序皆為替身。"""

import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import chat_test_cli as cli


class CliTests(unittest.TestCase):
    def test_no_arguments_prints_usage_without_run(self):
        with patch.object(cli, "run") as run, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main([]), 0)
        run.assert_not_called()
        self.assertIn("--suite", out.getvalue())

    def test_removed_legacy_flags_rejected(self):
        for flags in (["--max-turns", "1"], ["--model", "Rushia"], ["--retries", "3"]):
            with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.main(["--suite", "chat", *flags])

    def test_database_guard_rejects_same_database_and_does_not_print_credentials(self):
        with patch.object(cli, "dotenv_values", return_value={
                "MEMORY_TEST_DATABASE_URL": "postgresql://secret:credential@localhost/common",
                "MEMORY_DATABASE_URL": "postgresql://other:credential@other/common"}), patch.dict(cli.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "不同的 PostgreSQL") as exc:
                cli.test_database_url()
        self.assertNotIn("credential", str(exc.exception))

    def test_backend_is_local_isolated_and_disables_audio(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(cli.subprocess, "Popen") as start:
            process, log = cli.start_backend(Path(directory), 1234, "postgresql://test/db", "test_" + "a" * 32, uuid4(), uuid4())
            log.close()
            self.assertIs(process, start.return_value)
            env = start.call_args.kwargs["env"]
            self.assertEqual(env["AI_VT_TEST_MODE"], "true")
            self.assertEqual(env["TTS_ENABLED"], "false")
            self.assertEqual(env["ASR_ENABLED"], "false")
            self.assertEqual(Path(env["AI_VT_TEST_FIXTURE_PATH"]), Path(directory) / "fixture.json")

    def test_safe_error_redacts_transport_details(self):
        self.assertNotIn("secret", cli.safe_error(RuntimeError("postgresql://secret@server/db")))
        self.assertEqual(cli.safe_error(ValueError("fixture_invalid")), "fixture_invalid")

    def test_weather_text_source_is_not_a_screenshot_asset(self):
        record = {"tools": [{"name": "get_weather", "result": {"data": {"source": "CWA fixture"}}}]}
        self.assertEqual(cli.collect_assets(record, Path(".")), [])

    def test_cost_excludes_injected_provider_fault_and_keeps_failed_summary_unknown(self):
        records = [{"trace": [{"stage": "context_test_fault", "fault": "vision_rejection"},
                               {"stage": "chat", "error": "BadRequestError", "duration_sec": 0.1},
                               {"stage": "chat", "error": None, "usage": None}],
                    "control": {"action": "compress", "status": "failed", "duration_sec": 2}}]
        metadata = {}
        cli.update_cost(metadata, records, None, cli.time.monotonic())
        self.assertEqual(metadata["cost"]["roles"]["chat"]["calls"], 1)
        self.assertIsNone(metadata["cost"]["roles"]["chat"]["duration_sec"])
        self.assertIsNone(metadata["cost"]["roles"]["summary"]["calls"])
        self.assertEqual(metadata["cost"]["roles"]["summary"]["duration_sec"], 2)

    def test_cost_counts_native_tool_selection_and_final_request_without_double_counting_direct_reply(self):
        records = [{"trace": [{"stage": "chat_request"}, {"stage": "context_tool_selection", "usage": None},
                               {"stage": "chat_request"}, {"stage": "context_test_fault"},
                               {"stage": "chat", "error": "BadRequestError"},
                               {"stage": "chat_request"}, {"stage": "chat", "usage": None}]},
                   {"trace": [{"stage": "chat_request"}, {"stage": "context_tool_selection", "usage": None},
                               {"stage": "chat", "usage": None}]}]
        metadata = {}
        cli.update_cost(metadata, records, None, cli.time.monotonic())
        self.assertEqual(metadata["cost"]["roles"]["chat"]["calls"], 3)


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_dependency_replays_only_required_inputs_and_preserves_denominator(self):
        await self._memory_run()

    async def test_objective_failure_prevents_dependent_requests_with_failed_status(self):
        for stage in ("retrieval", "injection", "tool_selection", "tool_delivery", "transaction", "isolation"):
            with self.subTest(stage=stage):
                await self._memory_run(failure_stage=stage)

    async def test_fixture_failure_marks_unsent_selected_cases_failed(self):
        await self._memory_run(prepare_failure=True)

    async def test_objective_failure_does_not_stop_independent_memory_scenarios(self):
        await self._memory_run(failure_stage="retrieval", full_suite=True)

    async def _memory_run(self, failure_stage=None, prepare_failure=False, full_suite=False):
        from tools.ai_testset import load_suite
        dataset = load_suite("memory")
        sent = []
        store = SimpleNamespace(open=Mock(), close=Mock(), case_state=Mock(return_value={}), created=True)
        loader = SimpleNamespace(embedding_cost={}, memory_ids={}, other_owner_state=Mock(return_value={}))

        async def prepare(case, _):
            if prepare_failure:
                raise ValueError("fixture_missing")
            return {"session_id": case["session"], "metadata": {}}

        async def turn(ws, index, message, model, session, storage, timeout, mode):
            sent.append(message)
            return {"turn_id": uuid4().hex, "user": message, "reply": "合成回答", "stream_complete": True,
                    "request_sent": True, "errors": [], "_db_state": {}}

        def check(suite, case, record, *args):
            failed = failure_stage and case["case_id"] == "mem_003"
            record["checks"] = [{"stage": failure_stage or "execution", "status": "failed" if failed else "passed", "reason_code": "test_double",
                                  "evidence": {"file": "turns.jsonl", "case_id": case["case_id"]}}]
            record["operation_observations"] = [{"operation": "reinforce", "observed": {
                "referenced_target_ids": ["existing"], "reinforced_target_ids": [], "new_same_text_ids": ["new"]}}]

        loader.prepare = prepare
        settings = SimpleNamespace(embedding_model="test", embedding_serving_model="test", embedding_dimension=1024,
                                   embedding_query_prefix="", embedding_document_prefix="", embedding_contract="test")
        socket = SimpleNamespace(close=AsyncMock())
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            replacements = {
                "test_database_url": lambda: "postgresql://test/test_db", "MemoryRunStore": Mock(return_value=store),
                "start_backend": Mock(return_value=(Mock(), Mock())), "stop_backend": Mock(),
                "connect_backend": AsyncMock(return_value=socket), "control_step": AsyncMock(return_value={}),
                "run_turn": turn, "provenance": lambda _: {}, "RUNS_DIR": root,
            }
            for name, value in replacements.items():
                stack.enter_context(patch.object(cli, name, value))
            stack.enter_context(patch("tools.ai_test_fixtures.FixtureLoader", return_value=loader))
            stack.enter_context(patch("domain.memory_settings.load_memory_settings", return_value=settings))
            stack.enter_context(patch("tools.ai_test_evidence.check_turn", side_effect=check))
            ai_client = stack.enter_context(patch("infrastructure.ai_client.chat_create_with_fallback", side_effect=AssertionError("No runtime Judge allowed")))
            stack.enter_context(patch("infrastructure.ai_client._role_clients", {}))
            args = argparse.Namespace(suite="memory", case=None if full_suite else "mem_006", scenario=None, turn_timeout=1, startup_timeout=1)
            run_dir, status = await cli.run(args)
            result = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            snapshot = json.loads((run_dir / "cases.json").read_text(encoding="utf-8"))
            records = [json.loads(line) for line in (run_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
        expected = [c["input"] for c in dataset["cases"] if c["case_id"] in {"mem_003", "mem_005", "mem_006"}]
        if full_suite:
            self.assertEqual(sent, [c["input"] for c in dataset["cases"] if c["case_id"] not in {"mem_004", "mem_005", "mem_006"}])
        else:
            self.assertEqual(sent, [] if prepare_failure else expected[:1] if failure_stage else expected)
        ai_client.assert_not_called()
        self.assertNotIn("judge", result["cost"])
        self.assertTrue(all("semantic_review" not in record for record in records))
        self.assertTrue(all("blocked" not in json.dumps(record) for record in records))
        if failure_stage and not full_suite:
            self.assertEqual(status, "failed")
            self.assertTrue(all(record["status"] == "failed" for record in records))
            self.assertTrue(all(record["request_sent"] is False for record in records[1:]))
            self.assertEqual(records[1]["checks"][0]["evidence"]["case_ids"], ["mem_003"])
            self.assertEqual(records[2]["checks"][0]["evidence"]["case_ids"], ["mem_005"])
        if prepare_failure:
            self.assertEqual(status, "failed")
            self.assertEqual(result["execution"]["status"], "failed")
            self.assertTrue(all(record["status"] == "failed" and record["request_sent"] is False for record in records))
            self.assertEqual(records[0]["checks"][0]["reason_code"], "runner_failure")
            self.assertEqual(records[1]["checks"][0]["reason_code"], "run_terminated")
        self.assertEqual(snapshot["selection"], [c["case_id"] for c in dataset["cases"]] if full_suite else ["mem_003", "mem_005", "mem_006"])
        self.assertEqual(result["execution"]["planned_cases"], 14)
        self.assertNotEqual(status, "passed")
        store.close.assert_called_once()

    async def test_turn_does_not_retry_sent_user_input_on_connection_failure(self):
        socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(side_effect=OSError("closed")))
        store = SimpleNamespace(snapshot=Mock(return_value={}), case_state=Mock(return_value={}))
        from domain.chat_test_mode import ChatTestMode
        result = await cli.run_turn(socket, 1, "合成輸入", "Rushia", "fixture_test", store, 1, ChatTestMode.SHORT_ONLY)
        socket.send.assert_awaited_once()
        self.assertTrue(result["request_sent"])
        self.assertTrue(result["errors"])

    async def test_state_capture_failure_preserves_sent_input_and_reply(self):
        socket = SimpleNamespace(send=AsyncMock(), recv=AsyncMock(side_effect=[
            json.dumps({"type": "text_stream", "content": "實際回答"}),
            json.dumps({"type": "expression_plan", "plan": {"expression": "neutral"}}),
            json.dumps({"type": "stream_end"})]))
        store = SimpleNamespace(snapshot=Mock(return_value={}),
                                case_state=Mock(side_effect=[{}, RuntimeError("DB read failed")]))
        from domain.chat_test_mode import ChatTestMode
        result = await cli.run_turn(socket, 1, "合成輸入", "Rushia", "fixture_test", store, 1, ChatTestMode.SHORT_ONLY)
        socket.send.assert_awaited_once()
        self.assertTrue(result["request_sent"])
        self.assertEqual(result["reply"], "實際回答")
        self.assertIsNone(result["_db_state"])
        self.assertIn("讀取 DB 狀態失敗：RuntimeError", result["errors"])

    async def test_full_integration_saves_generated_inputs_and_uses_one_owner_reset(self):
        await self._integration_run()

    async def test_generation_failure_keeps_prior_dialogue_and_marks_unsent_slots_failed(self):
        await self._integration_run(generation_failure=True)

    async def _integration_run(self, generation_failure=False):
        from tools.ai_testset import load_suite
        dataset = load_suite("integration")
        sent, generated = [], []
        store = SimpleNamespace(open=Mock(), close=Mock(), case_state=Mock(return_value={}), created=True)
        loader = SimpleNamespace(embedding_cost={}, memory_ids={}, other_owner_state=Mock(return_value={}))

        async def prepare(case, _):
            return {"session_id": case["session"], "metadata": {}}

        async def turn(ws, index, message, model, session, storage, timeout, mode):
            sent.append({"user": message, "session": session, "mode": mode.value})
            return {"turn_id": uuid4().hex, "user": message, "reply": "本次實際合成回覆", "stream_complete": True,
                    "request_sent": True, "errors": [], "_db_state": {}}

        async def generate(case, previous, diagnostics):
            expected_previous = "it_013" if case["case_id"] == "it_014" else "it_014"
            self.assertEqual(previous[-1]["case_id"], expected_previous)
            self.assertEqual(previous[-1]["reply"], "本次實際合成回覆")
            generated.append(case["case_id"])
            if generation_failure:
                raise RuntimeError("generation_unavailable")
            diagnostics["calls"] += 1
            return ("你現在還記得我的飲料偏好、先前暗號和電影喜好有哪些嗎？" if case["case_id"] == "it_014"
                    else "那切回 AI VT 專案，現在和以前各由誰負責動作，接下來的下一步是什麼？")

        def check(suite, case, record, *args):
            record["checks"] = [{"stage": "execution", "status": "passed", "reason_code": "test_double",
                                  "evidence": {"file": "turns.jsonl", "case_id": case["case_id"]}}]

        async def control(ws, action, *args):
            return {"action": action, "status": "success", "acknowledged": True}

        loader.prepare = prepare
        settings = SimpleNamespace(embedding_model="test", embedding_serving_model="test", embedding_dimension=1024,
                                   embedding_query_prefix="", embedding_document_prefix="", embedding_contract="test")
        socket = SimpleNamespace(close=AsyncMock())
        controls = AsyncMock(side_effect=control)
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            root = Path(directory)
            replacements = {
                "test_database_url": lambda: "postgresql://test/test_db", "MemoryRunStore": Mock(return_value=store),
                "start_backend": Mock(return_value=(Mock(), Mock())), "stop_backend": Mock(),
                "connect_backend": AsyncMock(return_value=socket), "control_step": controls,
                "run_turn": turn, "provenance": lambda _: {}, "RUNS_DIR": root,
                "read_session_state": Mock(side_effect=[{"summary": "舊摘要", "summary_through_sequence": -1},
                                                       {"summary": "新摘要", "summary_through_sequence": 5}]),
                "read_compressed_texts": Mock(return_value=["已壓縮的原始user與assistant對話"]),
            }
            for name, value in replacements.items():
                stack.enter_context(patch.object(cli, name, value))
            stack.enter_context(patch("tools.ai_test_fixtures.FixtureLoader", return_value=loader))
            stack.enter_context(patch("domain.memory_settings.load_memory_settings", return_value=settings))
            stack.enter_context(patch("tools.ai_test_evidence.check_turn", side_effect=check))
            ai_client = stack.enter_context(patch("infrastructure.ai_client.chat_create_with_fallback", side_effect=AssertionError("No runtime Judge allowed")))
            stack.enter_context(patch("tools.ai_testset.generate_followup", side_effect=generate))
            stack.enter_context(patch("infrastructure.ai_client._role_clients", {}))
            args = argparse.Namespace(suite="integration", case=None, scenario=None, turn_timeout=1, startup_timeout=1)
            run_dir, status = await cli.run(args)
            result = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            snapshot = json.loads((run_dir / "cases.json").read_text(encoding="utf-8"))
            records = [json.loads(line) for line in (run_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
        if generation_failure:
            self.assertEqual(len(sent), 13)
            self.assertEqual(generated, ["it_014"])
            self.assertEqual(status, "failed")
            self.assertEqual(result["execution"]["completed_turns"], 13)
            self.assertTrue(all(record["request_sent"] is False and record["status"] == "failed" for record in records[-2:]))
            self.assertTrue(all(record["reply"] == "本次實際合成回覆" for record in records[:13]))
            self.assertTrue(all(not c["input"] for c in snapshot["cases"][-2:]))
            return
        self.assertEqual(len(sent), 15)
        ai_client.assert_not_called()
        self.assertTrue(all("semantic_review" not in record for record in records))
        self.assertEqual(generated, ["it_014", "it_015"])
        self.assertEqual([c.args[1] for c in controls.await_args_list], ["reset", "compress"])
        self.assertEqual([row["user"] for row in sent[:13]], [c["input"] for c in dataset["cases"][:13]])
        self.assertEqual(snapshot["cases"][-2]["input"], sent[-2]["user"])
        self.assertEqual(snapshot["cases"][-1]["input"], sent[-1]["user"])
        self.assertEqual(records[-2]["test_mode"], "mixed_read")
        self.assertEqual(records[-1]["test_mode"], "mixed_read")
        self.assertEqual(result["execution"]["completed_turns"], 15)
        self.assertEqual(result["counts"]["human_pending"], 15)
        self.assertEqual(status, "pending")


if __name__ == "__main__":
    unittest.main()
