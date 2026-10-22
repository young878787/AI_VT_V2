"""候選校準與獨立保留組的純契約測試。"""
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from tools.memory_retrieval_eval import (
    DATA, aggregate, candidate_rows, choose_candidate_policy, per_case_metrics,
    probe_text, project_ids, review_answer, validate_dataset,
)


def memory(text="測試事實"):
    return {"canonical_text": text, "memory_type": "preference", "subject_key": "user",
            "status": "active", "valid_from": None, "valid_to": None}


class MemoryRetrievalEvaluationTests(unittest.TestCase):
    def test_fixed_split_and_no_coffee_tuning(self):
        cases, items = validate_dataset(json.loads(DATA.read_text(encoding="utf-8")))
        self.assertEqual(len(cases), 16)
        self.assertEqual(len(items), 66)
        self.assertEqual(sum(c["split"] == "calibration" for c in cases), 8)
        self.assertEqual(sum(c["split"] == "holdout" for c in cases), 8)
        self.assertNotIn("咖啡", json.dumps(cases, ensure_ascii=False))

    def test_holdout_cannot_select_policy(self):
        with self.assertRaises(ValueError):
            choose_candidate_policy([{"split": "holdout", "gold": [], "ranked": []}], {})

    def test_candidate_calibration_does_not_require_unknown_empty(self):
        rows = [{"key": "wrong", "similarity": .60, "exact_match": True},
                {"key": "needed", "similarity": .52, "exact_match": False}]
        policy = choose_candidate_policy([
            {"split": "calibration", "gold": ["needed"], "ranked": rows},
            {"split": "calibration", "gold": [], "ranked": rows[:1]},
        ], {"wrong": memory(), "needed": memory()})
        self.assertTrue(policy["feasible"])
        self.assertEqual((policy["threshold"], policy["limit"]), (.50, 5))
        self.assertEqual(policy["vector_recall"], 1)
        self.assertGreater(policy["mean_candidates"], 0)

    def test_lexical_match_cannot_hide_missing_vector_coverage(self):
        policy = choose_candidate_policy([
            {"split": "calibration", "gold": ["needed"], "ranked": [
                {"key": "needed", "similarity": .2, "exact_match": True}]},
        ], {"needed": memory()})
        self.assertFalse(policy["feasible"])

    def test_multifact_requires_all_facts_and_bounded_projection(self):
        rows = [{"key": k, "similarity": s, "exact_match": False}
                for k, s in (("a", .7), ("b", .65), ("c", .55))]
        policy = choose_candidate_policy([
            {"split": "calibration", "gold": ["a", "b", "c"], "ranked": rows},
        ], {k: memory() for k in ("a", "b", "c")})
        self.assertEqual((policy["threshold"], policy["limit"]), (.55, 5))
        self.assertEqual(policy["projection_recall"], 1)
        rows[2]["key"] = "oversized"
        policy = choose_candidate_policy([
            {"split": "calibration", "gold": ["oversized"], "ranked": rows},
        ], {"a": memory(), "b": memory(), "oversized": memory("字" * 1000)})
        self.assertFalse(policy["feasible"])

    def test_empty_precision_is_undefined_not_perfect(self):
        self.assertIsNone(aggregate([{"gold": [], "selected": []}])["precision"])
        self.assertEqual(per_case_metrics(["a", "b"], ["a"])["recall"], .5)
        self.assertEqual(per_case_metrics([], ["wrong"])["fp"], 1)

    def test_calibration_does_not_cap_general_multifact_questions_at_three(self):
        keys = [f"fact{i}" for i in range(4)]
        policy = choose_candidate_policy([{"split":"calibration", "gold":keys, "ranked":[
            {"key":k, "similarity":.6, "exact_match":False} for k in keys]}],
            {k:memory() for k in keys})
        self.assertTrue(policy["feasible"])
        self.assertEqual(policy["limit"], 5)
        self.assertEqual(policy["projection_recall"], 1)

    def test_projection_never_injects_partial_fact(self):
        self.assertEqual(project_ids(["long", "a"], {"long": memory("字" * 1000), "a": memory()}), ["a"])

    def test_six_required_facts_report_capacity_failure(self):
        keys = [f"fact{i}" for i in range(6)]
        policy = choose_candidate_policy([{"split": "calibration", "gold": keys, "ranked": [
            {"key": k, "similarity": .9, "exact_match": True} for k in keys]}],
            {k: memory() for k in keys})
        self.assertFalse(policy["feasible"])
        self.assertEqual(project_ids(keys, {k: memory() for k in keys}), keys[:5])

    def test_vector_and_lexical_qualification_keep_ranking_and_cap(self):
        rows = [{"key": "a", "similarity": .7, "exact_match": False},
                {"key": "b", "similarity": .4, "exact_match": True}]
        self.assertEqual(candidate_rows(rows, .5, 3), rows)
        self.assertEqual(candidate_rows(rows, .5, 3, vector_only=True), rows[:1])
        self.assertEqual(candidate_rows(rows, .5, 1), rows[:1])

    def test_probe_is_self_contained_without_stored_history(self):
        self.assertEqual(probe_text({"user": "現在怎麼安排？", "recent_user": ["今晚要早睡。"]}),
                         "現在怎麼安排？\n今晚要早睡。")


class ReviewContractTests(unittest.IsolatedAsyncioTestCase):
    async def _review(self, values):
        responses = [SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps(v)))], model="test", usage=None) for v in values]
        create = AsyncMock(side_effect=responses)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        settings = SimpleNamespace(memory_model="test", memory_base_url="https://api.openai.com/v1")
        result = await review_answer(client, settings, {"user":"問題", "gold":[]}, "回答", {}, [])
        return result, create

    async def test_schema_error_gets_one_bounded_retry(self):
        good = {"verdict":"correct", "reason":"符合證據", "missing_facts":[], "unsupported_claims":[]}
        result, create = await self._review([{**good, "missing_facts":["矛盾"]}, good])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(create.await_count, 2)

    async def test_valid_incorrect_is_not_retried(self):
        wrong = {"verdict":"incorrect", "reason":"捏造", "missing_facts":[], "unsupported_claims":["未知偏好"]}
        result, create = await self._review([wrong])
        self.assertEqual(result["verdict"], "incorrect")
        self.assertEqual(create.await_count, 1)


if __name__ == "__main__":
    unittest.main()
