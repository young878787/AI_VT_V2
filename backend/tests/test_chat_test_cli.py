import argparse
import asyncio
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tools import chat_test_cli as cli


class FakeSocket:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def make_record(turn: int, error: str | None = None) -> dict:
    return {
        "turn": turn, "ts": "2026-09-23T12:00:00", "user": f"message {turn}",
        "reply": "露西亞的回答" if not error else "",
        "emotion_state": {"shy": 0.5}, "emotion_source": "jev",
        "expression": "shy", "expression_debug": {}, "memory_changes": {},
        "errors": [error] if error else [], "latency_first_text_sec": 1.0,
        "duration_sec": 2.0,
    }


class ChatTestCliTests(unittest.TestCase):
    def test_memory_stability_deduplicates_event_and_keeps_semantics_separate(self):
        earlier = {"memory_event_id": "event", "memory_attempts": 1, "memory_job_status": "retry",
                   "memory_agent_diagnostics": {"calls": 2, "logical_steps": 2}}
        final = {"memory_event_id": "event", "memory_attempts": 2, "memory_job_status": "done",
                 "latency_memory_completion_sec": 3.5, "semantic_review": {"verdict": "incorrect"},
                 "memory_agent_diagnostics": {"calls": 5, "logical_steps": 4, "failures": 1,
                    "queue": {"active_jobs": 2, "oldest_age_sec": 7}}}
        result = cli.summarize_memory_stability([earlier, final, {"memory_event_id": None}])
        self.assertEqual(result["observed_jobs"], 1)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["totals"]["calls"], 5)
        self.assertEqual(result["totals"]["logical_steps"], 4)
        self.assertEqual(result["completion_wait_sec"]["p95"], 3.5)
        self.assertEqual(result["max_observed_queue_oldest_sec"], 7)
        self.assertNotIn("semantic_review", result)

    def test_fixed_latest_reused_without_creating_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            first = cli.create_run_dir(root)
            (first / "turns.jsonl").write_text("existing evidence")
            self.assertEqual(cli.create_run_dir(root), first)
            self.assertFalse(first.is_symlink())
            self.assertEqual([p.name for p in root.iterdir()], ["latest"])

    def test_old_latest_symlink_does_not_overwrite_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            old = root / "20261003_130507"
            old.mkdir()
            (old / "turns.jsonl").write_text("previous evidence")
            (root / "latest").symlink_to(old.name, target_is_directory=True)
            latest = cli.create_run_dir(root)
            (latest / "turns.jsonl").write_text("new evidence")
            self.assertFalse(latest.is_symlink())
            self.assertEqual((old / "turns.jsonl").read_text(), "previous evidence")

    def test_backend_uses_isolated_test_database_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = pathlib.Path(directory)
            (run_dir / "memory").mkdir()
            fake_process = mock.Mock()
            user_id, character_id = cli.uuid.uuid4(), cli.uuid.uuid4()
            schema = "test_" + cli.uuid.uuid4().hex
            with mock.patch("tools.chat_test_cli.subprocess.Popen", return_value=fake_process) as popen:
                process, log_file = cli.start_backend(run_dir, 12345, "postgresql://test/db", schema,
                                                       user_id, character_id)
            try:
                self.assertIs(process, fake_process)
                kwargs = popen.call_args.kwargs
                self.assertEqual(kwargs["env"]["AI_VT_MEMORY_DIR"], str((run_dir / "memory").resolve()))
                self.assertEqual(kwargs["env"]["AI_VT_TEST_MODE"], "true")
                self.assertEqual(kwargs["env"]["MEMORY_TEST_DATABASE_URL"], "postgresql://test/db")
                self.assertEqual(kwargs["env"]["MEMORY_DATABASE_SCHEMA"], schema)
                self.assertEqual(kwargs["env"]["MEMORY_DEFAULT_USER_ID"], str(user_id))
                self.assertEqual(kwargs["env"]["MEMORY_DEFAULT_CHARACTER_ID"], str(character_id))
                self.assertEqual(kwargs["cwd"], cli.BACKEND_ROOT)
                self.assertIn("12345", popen.call_args.args[0])
            finally:
                log_file.close()

    def test_report_shows_joint_jev_choices_and_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            record = make_record(1)
            record["expression_debug"] = {
                "jevBaseEmotionChoice": "shy", "jevBaseEmotionConfidence": 0.8,
                "jevInteractionAttitudeChoice": "awkward",
                "jevInteractionAttitudeConfidence": 0.75,
                "jevResolvedEmotion": "shy", "jevResolvedAttitude": "awkward",
                "jevDecisionSource": "jev",
                "jevBaseEmotionFallbackReason": "none",
                "jevInteractionAttitudeFallbackReason": "none",
                "jevDecisionCriteriaVersion": "joint_two_axis_v4",
                "jevDecisionQuestionHash": "abc123abc123",
            }
            path = pathlib.Path(directory) / "memory_report.md"
            cli.write_markdown_report([record], path, {
                "run_id": "test", "planned_turns": 1, "started_at": "now",
                "scenario": "test", "scenario_sha256": "hash",
                "ai_provider": "test", "chat_model": "test", "jev_model": "test",
                "memory_schema": "test_" + "a" * 32,
            }, "completed", None)
            report = path.read_text(encoding="utf-8")
            self.assertIn("## 統計摘要", report)
            self.assertIn("## 全部輪次決策總覽", report)
            self.assertIn("| 1 | message 1 | 露西亞的回答 | shy 0.50、pleased 未提供、genuinely_angry 未提供、sad_or_hurt 未提供、masking_positive_feeling 未提供、wants_continue_interaction 未提供 | shy | awkward | shy | awkward | B 0.80 / A 0.75 | OK |", report)
            self.assertIn("## 詳細紀錄", report)
            self.assertIn("turns.jsonl", report)
            self.assertNotIn("```json", report)
            self.assertNotIn("## 詳細資料", report)
            self.assertIn("問題指紋：`abc123abc123`", report)

    def test_report_identifies_rejected_attitude_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            record = make_record(15)
            record["expression"] = "neutral"
            record["expression_debug"] = {
                "jevBaseEmotionChoice": "neutral", "jevBaseEmotionConfidence": 0.82,
                "jevInteractionAttitudeChoice": "smile",
                "jevInteractionAttitudeConfidence": 0.33,
                "jevResolvedEmotion": "neutral", "jevResolvedAttitude": "smile",
                "jevDecisionSource": "partial_fallback",
                "jevBaseEmotionFallbackReason": "none",
                "jevInteractionAttitudeFallbackReason": "low_confidence",
            }
            path = pathlib.Path(directory) / "memory_report.md"
            cli.write_markdown_report([record], path, {
                "run_id": "test", "planned_turns": 1, "started_at": "now",
                "scenario": "test", "scenario_sha256": "hash",
                "ai_provider": "test", "chat_model": "test", "jev_model": "test",
                "memory_schema": "test_" + "a" * 32,
            }, "completed", None)
            report = path.read_text(encoding="utf-8")
            self.assertIn("互動態度（JEV 原始選擇）", report)
            self.assertIn("| 原始態度 |", report)
            self.assertIn("| neutral | smile | neutral | smile | B 0.82 / A 0.33 | 部分回退（態度 low_confidence） |", report)

    def test_failed_turn_stops_scenario_and_keeps_partial_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            scenario = root / "scenario.txt"
            scenario.write_text("one\ntwo\nthree\n", encoding="utf-8")
            socket = FakeSocket()
            process = mock.Mock()
            process.poll.return_value = None
            log_file = mock.Mock()
            args = argparse.Namespace(scenario=str(scenario), model="Hiyori", max_turns=0,
                                      retries=0, startup_timeout=1, turn_timeout=1)
            with mock.patch.object(cli, "RUNS_DIR", root / "runs"), \
                mock.patch.object(cli, "test_database_url", return_value="postgresql://test/db"), \
                mock.patch.object(cli.MemoryRunStore, "open"), \
                mock.patch.object(cli.MemoryRunStore, "close"), \
                mock.patch.object(cli, "start_backend", return_value=(process, log_file)), \
                mock.patch.object(cli, "connect_backend", return_value=socket), \
                mock.patch.object(cli, "stop_backend") as stop, \
                mock.patch.object(cli, "run_turn", side_effect=[make_record(1), make_record(2, "overloaded")]) as turn:
                run_dir, status = asyncio.run(cli.run(args))
            self.assertEqual(status, "failed")
            self.assertEqual(turn.call_count, 2)
            self.assertTrue(socket.closed)
            stop.assert_called_once_with(process, log_file)
            records = [json.loads(line) for line in (run_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["turn"] for record in records], [1, 2])
            report = (run_dir / "expression_report.md").read_text(encoding="utf-8")
            self.assertIn("**failed**；已完成 1 / 3 輪", report)
            self.assertIn("第 2 輪失敗：overloaded", report)
            self.assertNotIn("message 3", report)
            self.assertTrue((run_dir / "run.json").exists())
            self.assertTrue((run_dir / "case_states.jsonl").exists())

    def test_interruption_preserves_first_turn_and_closes_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            scenario = root / "scenario.txt"
            scenario.write_text("one\ntwo\n", encoding="utf-8")
            socket = FakeSocket()
            process = mock.Mock()
            log_file = mock.Mock()
            args = argparse.Namespace(scenario=str(scenario), model="Hiyori", max_turns=0,
                                      retries=0, startup_timeout=1, turn_timeout=1)
            with mock.patch.object(cli, "RUNS_DIR", root / "runs"), \
                mock.patch.object(cli, "test_database_url", return_value="postgresql://test/db"), \
                mock.patch.object(cli.MemoryRunStore, "open"), \
                mock.patch.object(cli.MemoryRunStore, "close"), \
                mock.patch.object(cli, "start_backend", return_value=(process, log_file)), \
                mock.patch.object(cli, "connect_backend", return_value=socket), \
                mock.patch.object(cli, "stop_backend") as stop, \
                mock.patch.object(cli, "run_turn", side_effect=[make_record(1), asyncio.CancelledError()]):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(cli.run(args))
            run_dir = (root / "runs" / "latest").resolve()
            report = (run_dir / "expression_report.md").read_text(encoding="utf-8")
            self.assertIn("**interrupted**；已完成 1 / 2 輪", report)
            self.assertEqual(len((run_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()), 1)
            self.assertTrue(socket.closed)
            stop.assert_called_once_with(process, log_file)

    def test_retry_keeps_one_final_turn_and_records_attempt_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            scenario = root / "scenario.txt"
            scenario.write_text("one\n", encoding="utf-8")
            sockets = [FakeSocket(), FakeSocket()]
            args = argparse.Namespace(scenario=str(scenario), model="Hiyori", max_turns=0,
                                      retries=1, startup_timeout=1, turn_timeout=1)
            with mock.patch.object(cli, "RUNS_DIR", root / "runs"), \
                mock.patch.object(cli, "test_database_url", return_value="postgresql://test/db"), \
                mock.patch.object(cli.MemoryRunStore, "open"), \
                mock.patch.object(cli.MemoryRunStore, "close"), \
                mock.patch.object(cli, "start_backend", return_value=(mock.Mock(), mock.Mock())), \
                mock.patch.object(cli, "connect_backend", side_effect=sockets) as connect, \
                mock.patch.object(cli, "stop_backend"), \
                mock.patch.object(cli, "run_turn", side_effect=[make_record(1, "overloaded"), make_record(1)]):
                run_dir, status = asyncio.run(cli.run(args))
            self.assertEqual(status, "completed")
            self.assertEqual(connect.call_count, 2)
            self.assertTrue(all(socket.closed for socket in sockets))
            records = [json.loads(line) for line in (run_dir / "turns.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["attempts"], 2)
            self.assertEqual(records[0]["attempt_errors"], ["overloaded"])

    def test_turn_timeout_returns_error_without_memory_job(self):
        class SlowSocket:
            async def send(self, payload):
                self.payload = json.loads(payload)

            async def recv(self):
                await asyncio.sleep(1)

        with tempfile.TemporaryDirectory():
            socket = SlowSocket()
            store = mock.Mock()
            store.snapshot.return_value = {}
            record = asyncio.run(cli.run_turn(socket, 1, "hi", "Hiyori", "test_session", store, 0.01))
            self.assertIn("逾時", record["errors"][0])
            self.assertEqual(socket.payload["session_id"], "test_session")
            self.assertEqual(record["memory_changes"], {})

    def test_missing_or_same_test_database_fails_before_backend(self):
        with mock.patch.object(cli, "ENV_PATH", pathlib.Path("/nonexistent/.env")), \
             mock.patch.dict(cli.os.environ, {"MEMORY_TEST_DATABASE_URL": "",
                                              "MEMORY_DATABASE_URL": "postgresql://localhost/prod"}):
            with self.assertRaisesRegex(RuntimeError, "MEMORY_TEST_DATABASE_URL"):
                cli.test_database_url()
        with mock.patch.object(cli, "ENV_PATH", pathlib.Path("/nonexistent/.env")), \
             mock.patch.dict(cli.os.environ, {"MEMORY_TEST_DATABASE_URL": "postgresql://localhost/prod",
                                              "MEMORY_DATABASE_URL": "postgresql://localhost/prod"}):
            with self.assertRaisesRegex(RuntimeError, "不同"):
                cli.test_database_url()

    def test_wait_requires_finalized_route_even_if_initial_status_is_ignored(self):
        store = mock.Mock()
        store.job.side_effect = [
            {"route_finalized": False, "status": "ignored"},
            {"route_finalized": True, "status": "buffered"},
        ]
        with mock.patch.object(cli.asyncio, "sleep", new=mock.AsyncMock()):
            job = asyncio.run(cli.wait_memory_job(store, "a" * 32, timeout=1))
        self.assertEqual(job["status"], "buffered")
        self.assertEqual(store.job.call_count, 2)


if __name__ == "__main__":
    unittest.main()
