import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tools.memory_semantic_review import evaluate_semantics, validate_review


def response(value):
    return SimpleNamespace(model="actual-review-model", usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value, ensure_ascii=False)))])


class SemanticReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.case = dict(case_id="case_1", expected_result="新偏好",
            conversation=[dict(phase="memory_setup", input="舊偏好"),
                          dict(phase="probe", input="之前喜歡什麼？"),
                          dict(phase="update", input="新偏好")])
        self.record = dict(case_id="case_1", phase="probe", turn=2, reply="舊偏好",
            expected_result="舊偏好", stream_complete=True, errors=[],
            trace=[dict(stage="chat_context", messages=[dict(role="system", content="目前日期：2026-10-03（Asia/Taipei）。")])],
            hard_status={"memory_evidence_status": "failed"})
        self.correct = dict(verdict="correct", reason="回答符合當步來源", missing_facts=[], unsupported_claims=[])

    async def test_judge_never_receives_future_update_or_overrides_hard_failure(self):
        diagnostics = {}
        with patch("infrastructure.ai_client.chat_create_with_fallback", new=AsyncMock(return_value=response(self.correct))) as call:
            failed = await evaluate_semantics([self.case], [self.record], diagnostics)
        material = json.loads(call.await_args.kwargs["messages"][1]["content"])
        self.assertNotIn("新偏好", json.dumps(material, ensure_ascii=False))
        self.assertEqual(material["expected_result"], "舊偏好")
        self.assertEqual(material["system_date"], "2026-10-03（Asia/Taipei）。")
        self.assertEqual(failed, set())
        self.assertFalse(self.record["semantic_review"]["hard_conditions_met"])
        self.assertEqual(self.record["hard_status"]["memory_evidence_status"], "failed")
        self.assertEqual(diagnostics["models"], ["actual-review-model"])
        self.assertEqual(diagnostics["counts"], {"correct": 1})

    async def test_invalid_review_retries_without_weakening_schema(self):
        invalid = {**self.correct, "unsupported_claims": ["未確認事實"]}
        with patch("infrastructure.ai_client.chat_create_with_fallback", new=AsyncMock(side_effect=[response(invalid), response(self.correct)])):
            await evaluate_semantics([self.case], [self.record], {})
        self.assertEqual(self.record["semantic_review"]["attempts"], 2)
        self.assertNotIn("error", self.record["semantic_review"])

    async def test_multiple_probes_keep_earlier_failure_and_scope_each_input(self):
        self.case["conversation"].append(dict(phase="recall_probe", input="現在喜歡什麼？"))
        later = {**self.record, "phase": "recall_probe", "turn": 4, "reply": "新偏好", "expected_result": "新偏好"}
        incorrect = dict(verdict="incorrect", reason="第一輪缺少舊偏好", missing_facts=["舊偏好"], unsupported_claims=[])
        diagnostics = {}
        with patch("infrastructure.ai_client.chat_create_with_fallback", new=AsyncMock(
                side_effect=[response(incorrect), response(self.correct)])) as call:
            failed = await evaluate_semantics([self.case], [self.record, later], diagnostics)
        materials = [json.loads(c.kwargs["messages"][1]["content"]) for c in call.await_args_list]
        self.assertNotIn("新偏好", json.dumps(materials[0], ensure_ascii=False))
        self.assertIn("新偏好", json.dumps(materials[1], ensure_ascii=False))
        self.assertEqual(failed, {"case_1"})
        self.assertEqual(diagnostics["counts"], {"incorrect": 1, "correct": 1})

    async def test_incomplete_answer_is_not_evaluated_and_transport_failure_fails(self):
        self.record["stream_complete"] = False
        with patch("infrastructure.ai_client.chat_create_with_fallback", new=AsyncMock()) as call:
            self.assertEqual(await evaluate_semantics([self.case], [self.record], {}), {"case_1"})
        call.assert_not_awaited()
        self.record["stream_complete"] = True
        diagnostics = {}
        with patch("infrastructure.ai_client.chat_create_with_fallback", new=AsyncMock(side_effect=TimeoutError)) as call:
            self.assertEqual(await evaluate_semantics([self.case], [self.record], diagnostics), {"case_1"})
        self.assertEqual(call.await_count, 2)
        self.assertEqual(diagnostics["status"], "incomplete")

    def test_exact_contract_and_bounded_arrays(self):
        for value in ({**self.correct, "extra": 1}, {**self.correct, "verdict": "passed"},
                      {**self.correct, "missing_facts": ["x"] * 9},
                      {**self.correct, "reason": ""}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_review(value)
