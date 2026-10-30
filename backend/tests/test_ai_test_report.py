import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from backend.tools.ai_test.report import (
    file_sha256, fingerprint, initialize_report, rebuild_report, write_reports,
)


def record(case_id="ch_001", status="passed", **extra):
    return {"case_id": case_id, "turn_id": "turn-" + case_id, "session_id": "session-" + case_id,
            "user": "請說明目前進度", "reply": "完整回答，包含必要資訊。\n第二段不能截斷。",
            "checks": [{"stage": "prompt", "status": status, "reason_code": "prompt_source",
                        "evidence": {"prompt_memory_ids": ["memory-1"]}}],
            "errors": [], "duration_sec": 1.5, **extra}


class AiTestReportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "latest"

    def initialize(self, suite="chat", cases=None):
        cases = cases or [{"case_id": "ch_001", "source": "fixed", "required_facts": ["進度"]}]
        initialize_report(self.directory, suite, {"version": 1, "cases": cases}, {"commit": "abc"})
        return {"suite": suite, "started_at": "2026-10-23T00:00:00+08:00",
                "planned_case_ids": [case["case_id"] for case in cases], "provenance": {"commit": "abc"}}

    def save(self, records, states=None):
        if states is None:
            suite = json.loads((self.directory / "review.json").read_text(encoding="utf-8"))["suite"]
            states = [{"case_id": row["case_id"], "state": "saved"} for row in records] if suite in ("memory", "integration") else []
        for name, values in (("turns.jsonl", records), ("case_states.jsonl", states or [])):
            (self.directory / name).write_text("".join(json.dumps(value, ensure_ascii=False) + "\n" for value in values), encoding="utf-8")

    def write(self, metadata, rows, execution="completed", **kwargs):
        states = [json.loads(line) for line in (self.directory / "case_states.jsonl").read_text(encoding="utf-8").splitlines() if line]
        return write_reports(self.directory, metadata, rows, states, execution=execution, **kwargs)

    def report(self):
        return (self.directory / "report.md").read_text(encoding="utf-8")

    def review(self, accepted_ids):
        path = self.directory / "review.json"
        review = json.loads(path.read_text(encoding="utf-8"))
        for case_id in accepted_ids:
            review["cases"][case_id] = {"status": "accepted", "reason": "已閱讀完整對話與來源",
                                        "turn_id": "turn-" + case_id, "evidence": []}
        path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")

    def test_success_has_hashes_null_unknown_cost_and_complete_dialogue(self):
        metadata = self.initialize()
        rows = [record(summary_before="舊摘要", summary_after="新摘要", job_wait={"status": "done", "seconds": 2},
                       expression_debug={"raw_selection": "sad", "confidence": 0.6, "probabilities": {"sad": 0.6},
                                         "fallback_reason": "jev_timeout", "resolved": "neutral"})]
        self.save(rows)
        run = self.write(metadata, rows, cleanup={"schema": "dropped"})
        self.assertEqual(run["acceptance"]["overall"], "passed")
        self.assertEqual(run["counts"]["fixed"], 1)
        self.assertIsNone(run["cost"]["chat"]["calls"])
        self.assertIsNone(run["metrics"]["tool_delivery"]["rate"])
        for name in ("cases.json", "turns.jsonl", "case_states.jsonl", "review.json"):
            self.assertEqual(run["artifacts"][name]["sha256"], file_sha256(self.directory / name))
        text = self.report()
        for expected in ("case-ch_001-step-1", "第二段不能截斷", "舊摘要", "新摘要", "job_wait", "raw_selection", "probabilities", "jev_timeout", "resolved"):
            self.assertIn(expected, text)
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_jev_role_records_are_reported_and_question_hashes_must_resolve(self):
        metadata = self.initialize()
        traces = [{"stage": "jev", "role": role, "question_hash": "hash_" + role, "duration_sec": 0.4,
                   "error": None, "state": {"current_user_input": "請說明目前進度", "recent_dialogue": []},
                   "answers": {"marker": "answer_" + role}} for role in ("emotion", "action", "memory")]
        rows = [record(trace=traces, emotion_source="jev", memory_route="process")]
        self.save(rows)
        questions = {"hash_" + role: {"role": role, "questions": {}} for role in ("emotion", "action", "memory")}
        (self.directory / "jev_questions.json").write_text(json.dumps({"questions": questions}), encoding="utf-8")
        run = self.write(metadata, rows)
        self.assertEqual(run["acceptance"]["overall"], "passed")
        self.assertEqual(run["artifacts"]["jev_questions.json"]["sha256"],
                         file_sha256(self.directory / "jev_questions.json"))
        text = self.report()
        self.assertIn("JEV 判斷", text)
        for role in ("emotion", "action", "memory"):
            self.assertIn(f"| {role} | current_user_input, recent_dialogue | 0 | 0.4 | 無 | `hash_{role}` |", text)
            self.assertIn("answer_" + role, text)
        self.assertIn('"route": "process"', text)
        questions.pop("hash_memory")
        (self.directory / "jev_questions.json").write_text(json.dumps({"questions": questions}), encoding="utf-8")
        run = self.write(metadata, rows)
        self.assertEqual(run["acceptance"]["overall"], "failed")
        self.assertIn("jev_question_missing", {item["reason_code"] for item in run["attention"]})

    def test_emotion_curve_is_reported_and_reviewed_outside_overall(self):
        metadata = self.initialize("jev-emotion", [{"case_id": "je_001", "source": "fixed"}])
        self.assertEqual(json.loads((self.directory / "review.json").read_text(encoding="utf-8"))["curve"],
                         {"status": "pending", "reason": None})
        metadata["emotion_curve"] = {
            "source": "replay_of_actual_scores", "recompute_consistent": True,
            "turns": {"je_001": {"scores": {}, "first": "calm", "second": "none",
                                 "mood": {"shy": 0, "pleased": 0, "genuinely_angry": 0, "sad_or_hurt": 0,
                                          "masking_positive_feeling": 0.5, "wants_continue_interaction": 0.5}}},
            "checks": [{"id": "C6", "title": "連續罵：生氣累積", "status": "failed",
                        "conditions": [{"kind": "rise", "actual": [0.4, 0.4]}],
                        "root_cause": {"kind": "single_turn", "case_ids": ["je_009"], "details": []}}]}
        rows = [record("je_001")]
        self.save(rows)
        run = self.write(metadata, rows)
        self.assertEqual((run["acceptance"]["overall"], run["acceptance"]["curve"]), ("passed", "pending"))
        self.assertIn("curve_pending", [item["reason_code"] for item in run["attention"]])
        text = self.report()
        self.assertIn("情緒曲線（人工判定，不計入驗收）：pending", text)
        self.assertIn("| [je_001](#case-je_001) | 0.00 | 0.00 | 0.00 | 0.00 | 0.50 | 0.50 | 平穩 | 無 |", text)
        self.assertIn("| C6 | 連續罵：生氣累積 | failed | rise=[0.4, 0.4] | single_turn je_009 |", text)
        path = self.directory / "review.json"
        for curve, expected in (({"status": "needs_changes", "reason": "罵 2 沒累積"}, "failed"),
                                ({"status": "accepted", "reason": "人工看過曲線"}, "passed"),
                                ({"status": "accepted", "reason": None}, "pending")):
            review = json.loads(path.read_text(encoding="utf-8"))
            review["curve"] = curve
            path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
            run = self.write(metadata, rows)
            with self.subTest(curve=curve):
                self.assertEqual((run["acceptance"]["overall"], run["acceptance"]["curve"]), ("passed", expected))
        self.assertIn("invalid_curve_review", [item["reason_code"] for item in run["attention"]])
        self.assertEqual(rebuild_report(self.directory)["emotion_curve"], metadata["emotion_curve"])

    def test_other_suites_have_no_curve_acceptance(self):
        metadata = self.initialize()
        rows = [record()]
        self.save(rows)
        run = self.write(metadata, rows)
        self.assertNotIn("curve", run["acceptance"])
        self.assertNotIn("emotion_curve", run)

    def test_emotion_rows_show_scores_ranking_and_failure_layer(self):
        metadata = self.initialize("jev-emotion", [{"case_id": "je_011", "source": "fixed"}])
        evidence = {"jev": {"scores": {"shy": 2, "pleased": 0, "genuinely_angry": 4, "sad_or_hurt": 3,
                                       "masking_positive_feeling": 2, "wants_continue_interaction": 1}},
                    "judgment": {"first": "pleased", "second": "none", "axes": {"masking_positive_feeling": "middle",
                                                                                "wants_continue_interaction": "middle"}},
                    "failure_layers": ["jev_judgment"]}
        row = record("je_011", emotion_evidence=evidence)
        row["checks"].append({"stage": "emotion", "status": "failed", "reason_code": "emotion_first",
                              "evidence": {"field": "emotion_evidence"}})
        self.save([row])
        run = self.write(metadata, [row])
        self.assertEqual(run["acceptance"]["overall"], "failed")
        text = self.report()
        self.assertIn("## 情緒逐輪", text)
        self.assertIn("| [je_011](#case-je_011) | 請說明目前進度 | 2／0／4／3／2／1 | pleased | none | middle／middle | "
                      "emotion_first | jev_judgment |", text)

    def test_capacity_failure_fails_overall_but_separates_core(self):
        metadata = self.initialize("memory", [{"case_id": "mem_013"}, {"case_id": "mem_014"}])
        rows = [record("mem_013"), record("mem_014", status="failed")]
        self.save(rows)
        run = self.write(metadata, rows)
        self.assertEqual(run["acceptance"], {"fixed_core": "passed", "generated": "not_applicable", "capacity": "failed", "overall": "failed"})
        self.assertIn("核心通過、容量缺口未解", self.report())

    def test_partial_cancelled_is_pending_not_success(self):
        metadata = self.initialize(cases=[{"case_id": "ch_001"}, {"case_id": "ch_002"}])
        metadata["selected_case_ids"] = ["ch_001"]
        rows = [record(status="pending")]
        self.save(rows)
        run = self.write(metadata, rows, execution="cancelled", cleanup={"status": "failed"})
        self.assertEqual(run["acceptance"]["overall"], "pending")
        self.assertEqual(run["counts"]["unexecuted"], 1)
        self.assertTrue(run["execution"]["partial"])
        self.assertEqual(run["cleanup"]["status"], "failed")
        self.assertIn("完整回答", self.report())
        self.assertIn("未執行", self.report())

    def test_failed_prerequisite_without_request_is_failed_with_objective_evidence(self):
        metadata = self.initialize("memory", [{"case_id": "mem_003"}, {"case_id": "mem_004"}])
        rows = [record("mem_003", status="failed"), record("mem_004", request_sent=False, reply="", checks=[
            {"stage": "execution", "status": "failed", "reason_code": "prerequisite_failed",
             "evidence": {"failed_dependencies": ["mem_003"]}}])]
        self.save(rows)
        run = self.write(metadata, rows)
        self.assertEqual(run["acceptance"]["overall"], "failed")
        self.assertEqual(run["counts"]["hard"], {"failed": 2})
        self.assertEqual(run["counts"]["executed"], 1)
        self.assertEqual(run["execution"]["submitted_turns"], 1)
        self.assertIn("| [mem_004](#case-mem_004) | fixed_core | failed | failed |", self.report())
        self.assertIn("user（預定，未送出）", self.report())
        self.assertIn("prerequisite_failed", self.report())
        self.assertNotIn("blocked", self.report())

    def test_operation_observations_remain_unscored_and_do_not_change_objective_result(self):
        metadata = self.initialize("memory", [{"case_id": "mem_003", "oracle": {
            "expected_operation": "reinforce", "required_facts": ["週末玩 Minecraft"]}}])
        observation = {"expected_operation": "reinforce", "actual_operations": ["create"],
                       "target_ids": ["new-memory"], "source_event_id": "event-3"}
        rows = [record("mem_003", operation_observations=[observation])]
        self.save(rows)
        run = self.write(metadata, rows)
        self.assertEqual(run["acceptance"]["overall"], "passed")
        self.assertNotIn("semantic", run["counts"])
        self.assertNotIn("judge", run["cost"])
        self.assertNotIn("judge_sec", run["cost"])
        self.assertFalse(any(item["stage"] == "semantic_review" for item in run["attention"]))
        for value in ("完整回答", "週末玩 Minecraft", "reinforce", "create", "new-memory", "event-3"):
            self.assertIn(value, self.report())
        self.assertNotIn("AI 語意評分", self.report())

    def test_cancelled_after_last_turn_does_not_claim_acceptance(self):
        metadata = self.initialize()
        rows = [record()]
        self.save(rows)
        run = self.write(metadata, rows, execution="cancelled")
        self.assertEqual(run["acceptance"]["overall"], "pending")

    def test_integration_requires_human_review_for_fixed_and_generated(self):
        cases = [{"case_id": "it_001"}, {"case_id": "it_014", "source": "generated"}]
        metadata = self.initialize("integration", cases)
        rows = [record(case["case_id"]) for case in cases]
        self.save(rows)
        pending = self.write(metadata, rows)
        self.assertEqual(pending["acceptance"]["overall"], "pending")
        self.assertEqual(pending["counts"]["human_pending"], 2)
        self.assertEqual(pending["counts"]["hard"], {"passed": 2})
        self.assertIn("| [it_001](#case-it_001) | fixed_core | completed | passed | pending |", self.report())
        self.review(["it_014"])
        self.assertEqual(rebuild_report(self.directory)["acceptance"]["fixed_core"], "pending")
        self.review(["it_001"])
        passed = rebuild_report(self.directory)
        self.assertEqual(passed["acceptance"]["overall"], "passed")
        self.assertEqual(passed["counts"]["human_pending"], 0)

    def test_stale_review_is_not_reused_after_machine_evidence_changes(self):
        metadata = self.initialize("integration", [{"case_id": "it_001"}])
        rows = [record("it_001")]
        self.save(rows)
        self.write(metadata, rows)
        self.review(["it_001"])
        self.assertEqual(rebuild_report(self.directory)["acceptance"]["overall"], "passed")
        rows[0]["reply"] = "修改後回答"
        self.save(rows)
        rebuilt = rebuild_report(self.directory)
        self.assertTrue(rebuilt["review"]["stale"])
        self.assertEqual(rebuilt["acceptance"]["overall"], "failed")
        self.assertIn("machine_artifact_changed", {item["reason_code"] for item in rebuilt["attention"]})

    def test_missing_image_is_visible_and_prevents_pass(self):
        metadata = self.initialize("tools", [{"case_id": "to_005"}])
        rows = [record("to_005", tools=[{"name": "capture_screenshot", "asset_path": "assets/to_005-1.jpg"}])]
        self.save(rows)
        run = self.write(metadata, rows)
        self.assertEqual(run["acceptance"]["overall"], "failed")
        self.assertIn("缺圖", self.report())
        (self.directory / "assets").mkdir()
        (self.directory / "assets/to_005-1.jpg").write_bytes(b"synthetic fixture bytes")
        run = self.write(metadata, rows)
        self.assertEqual(run["acceptance"]["overall"], "passed")
        self.assertIn("圖片預覽", self.report())

    def test_initialize_clears_review_and_ai_synthesis_without_removing_history(self):
        metadata = self.initialize("integration", [{"case_id": "it_001"}])
        rows = [record("it_001")]
        self.save(rows)
        self.write(metadata, rows)
        self.review(["it_001"])
        history = self.directory.parent / "historical"
        history.mkdir()
        (history / "report.md").write_text("歷史保留", encoding="utf-8")
        (self.directory / "report.md").write_text("上次 AI 綜整", encoding="utf-8")
        self.initialize("integration", [{"case_id": "it_001"}])
        review = json.loads((self.directory / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(review["cases"]["it_001"]["status"], "pending")
        self.assertNotIn("上次 AI 綜整", self.report())
        self.assertEqual((history / "report.md").read_text(encoding="utf-8"), "歷史保留")

    def test_rebuild_is_offline_and_keeps_machine_files_unchanged(self):
        metadata = self.initialize()
        metadata["cost"] = {"generation": {"calls": 2, "input_tokens": 10, "output_tokens": 20}}
        rows = [record()]
        self.save(rows)
        first = self.write(metadata, rows)
        before = {name: (self.directory / name).read_bytes() for name in ("cases.json", "turns.jsonl", "case_states.jsonl")}
        with mock.patch("socket.socket", side_effect=AssertionError("must remain offline")):
            second = rebuild_report(self.directory)
        self.assertEqual(first["acceptance"], second["acceptance"])
        self.assertEqual(second["cost"]["generation"], first["cost"]["generation"])
        self.assertEqual(before, {name: (self.directory / name).read_bytes() for name in before})

    def test_missing_hard_evidence_and_unsaved_records_do_not_pass(self):
        metadata = self.initialize()
        rows = [record(checks=[{"stage": "injection", "status": "passed"}])]
        run = write_reports(self.directory, metadata, rows, [], execution="completed")
        self.assertEqual(run["acceptance"]["overall"], "failed")
        reasons = {item["reason_code"] for item in run["attention"]}
        self.assertTrue({"missing_evidence", "unsaved_evidence"}.issubset(reasons))

    def test_final_missing_checks_or_case_state_fails_but_running_remains_pending(self):
        metadata = self.initialize("memory", [{"case_id": "mem_001"}])
        rows = [record("mem_001", checks=[])]
        self.save(rows, states=[])
        self.assertEqual(self.write(metadata, rows, execution="running")["acceptance"]["overall"], "pending")
        final = self.write(metadata, rows)
        self.assertEqual(final["acceptance"]["overall"], "failed")
        self.assertEqual(final["counts"]["hard"], {"failed": 1})
        self.assertIn("missing_case_state", {item["reason_code"] for item in final["attention"]})

    def test_runner_failure_without_submitted_turn_is_failed(self):
        metadata = self.initialize()
        metadata["failure"] = {"case_id": "ch_001", "stage": "runner", "exception": "RuntimeError"}
        self.save([])
        run = self.write(metadata, [], execution="failed", error="fixture failed")
        self.assertEqual(run["acceptance"]["overall"], "failed")
        self.assertEqual(run["counts"]["hard"], {"failed": 1})
        self.assertEqual(run["execution"]["submitted_turns"], 0)
        self.assertIn("| [ch_001](#case-ch_001) | fixed_core | failed | failed |", self.report())

    def test_stale_human_review_alone_is_pending_not_objective_failure(self):
        metadata = self.initialize("integration", [{"case_id": "it_001"}])
        rows = [record("it_001")]
        self.save(rows)
        self.write(metadata, rows)
        self.review(["it_001"])
        path = self.directory / "review.json"
        review = json.loads(path.read_text(encoding="utf-8"))
        review["basis"]["turns.jsonl"] = "stale"
        path.write_text(json.dumps(review), encoding="utf-8")
        run = rebuild_report(self.directory)
        self.assertEqual(run["acceptance"]["overall"], "pending")
        self.assertEqual(run["counts"]["hard"], {"passed": 1})

    def test_private_image_without_saved_asset_is_not_a_missing_tool_failure(self):
        metadata = self.initialize("tools", [{"case_id": "to_005"}])
        rows = [record("to_005", tools=[{"name": "capture_screenshot", "image_accepted": True}],
                       visual_evidence_unavailable="private_image_not_saved")]
        self.save(rows)
        run = self.write(metadata, rows)
        self.assertEqual(run["acceptance"]["overall"], "passed")
        self.assertFalse(any(item["reason_code"] == "missing_image" for item in run["attention"]))
        self.assertIn("無圖片預覽", self.report())

    def test_human_acceptance_cannot_override_failed_hard_checks(self):
        metadata = self.initialize("integration", [{"case_id": "it_001"}])
        rows = [record("it_001", status="failed")]
        self.save(rows)
        self.write(metadata, rows)
        self.review(["it_001"])
        self.assertEqual(rebuild_report(self.directory)["acceptance"]["overall"], "failed")

    def test_fingerprint_is_stable_across_key_order(self):
        self.assertEqual(fingerprint({"b": 2, "a": 1}), fingerprint({"a": 1, "b": 2}))

    def test_retrieval_metrics_sum_saved_facts_instead_of_check_pass_rate(self):
        metadata = self.initialize("memory", [{"case_id": "mem_013", "oracle": {
            "answerability": "answerable", "unknown_topics": ["電影喜好"]}}])
        rows = [record("mem_013", checks=[{"stage": "retrieval", "status": "passed", "evidence": "trace"}],
                       retrieval_metrics={"required_count": 2, "required_recalled": 1, "required_projected": 1,
                          "required_injected": 0, "candidate_ids": ["a", "b", "c"], "candidate_precision": 1 / 3,
                          "non_target_candidates": 2, "lexical_coverage": 1, "vector_coverage": 0})]
        self.save(rows)
        metrics = self.write(metadata, rows)["metrics"]
        self.assertEqual(metrics["retrieval"]["rate"], 0.5)
        self.assertEqual(metrics["projection"]["rate"], 0.5)
        self.assertEqual(metrics["injection"]["rate"], 0)
        self.assertEqual(metrics["check_pass_rates"]["retrieval"]["rate"], 1)
        self.assertAlmostEqual(metrics["candidate_precision"]["rate"], 1 / 3)
        self.assertEqual(metrics["non_target_candidates"], 2)
        self.assertEqual(metrics["lexical_coverage"]["rate"], 0.5)
        self.assertEqual(metrics["vector_coverage"]["rate"], 0)
        self.assertNotIn("unknown_honesty", metrics)
        self.assertNotIn("semantic_facts", metrics)

    def test_tool_metrics_use_saved_reason_codes_even_when_stage_is_tools(self):
        metadata = self.initialize("tools", [{"case_id": "to_001", "oracle": {"required_tools": ["get_datetime"]}}])
        rows = [record("to_001", tools=[{"name": "get_datetime"}], checks=[
            {"stage": "tools", "reason_code": "required_get_datetime", "status": "passed", "evidence": "trace"},
            {"stage": "tools", "reason_code": "receipt_pairs", "status": "passed", "evidence": "trace"},
            {"stage": "tools", "reason_code": "receipt_content", "status": "failed", "evidence": "trace"}])]
        self.save(rows)
        metrics = self.write(metadata, rows)["metrics"]
        self.assertEqual(metrics["tool_selection"], {"passed": 1, "total": 1, "rate": 1})
        self.assertEqual(metrics["tool_delivery"], {"passed": 1, "total": 2, "rate": 0.5})
        self.assertIsNone(metrics["retrieval"]["rate"])
        self.assertNotIn("unknown_honesty", metrics)


if __name__ == "__main__":
    unittest.main()
