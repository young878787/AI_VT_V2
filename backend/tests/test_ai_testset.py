"""新分組固定資料、前序重播及生成請求隔離契約。"""

import copy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ai_testset import fingerprint, generate_followup, load_suite, select_cases


FOLLOWUP_14 = "剛才處理完後，你還知道我的飲料偏好、測試暗號和最喜歡的電影有哪些嗎？"
FOLLOWUP_15 = "好，那回到 AI VT 專案，動作現在和以前分別由誰負責，接下來的下一步是什麼？"


class DatasetTests(unittest.TestCase):
    def test_four_suites_have_exact_single_input_counts(self):
        for suite, count in (("chat", 7), ("memory", 14), ("tools", 7), ("integration", 15)):
            with self.subTest(suite=suite):
                data = load_suite(suite)
                self.assertEqual(len(data["cases"]), count)
                self.assertEqual(len({case["case_id"] for case in data["cases"]}), count)
                self.assertTrue(all("conversation" not in case for case in data["cases"]))
                self.assertEqual(sum(case["source"] == "generated" for case in data["cases"]), 2 if suite == "integration" else 0)

    def test_jev_emotion_is_one_continuous_curve_session(self):
        data = load_suite("jev-emotion")
        self.assertEqual([case["case_id"] for case in data["cases"]], [f"je_{index:03}" for index in range(1, 16)])
        self.assertEqual({(case["session"], case["context"]["scenario"]) for case in data["cases"]},
                         {("emotion_curve", "emotion_curve")})
        # 不再共用 chat 案例；連續罵 3 輪、原句重講在 JEV 可見的近 3 輪內。
        self.assertEqual([case["context"]["stimulus"] for case in data["cases"][7:10]], ["scold"] * 3)
        self.assertEqual(data["cases"][3]["input"], data["cases"][1]["input"])
        self.assertEqual([check["id"] for check in data["curve_checks"]], [f"C{index}" for index in range(1, 13)])
        self.assertEqual([case["case_id"] for case in select_cases(data, "je_010")],
                         [f"je_{index:03}" for index in range(1, 11)])
        self.assertEqual(load_suite("jev-emotion", data)["input_source"], "replayed_inputs")

    def test_jev_emotion_oracle_rejects_unknown_tokens_and_missing_emotion(self):
        data = load_suite("jev-emotion")
        for mutate, message in (
                (lambda d: d["cases"][8]["oracle"]["emotion"]["first"].append("happy"), "first"),
                (lambda d: d["cases"][8]["oracle"]["emotion"].update(second=[]), "second"),
                (lambda d: d["cases"][8]["oracle"]["emotion"]["axes"].update(pleased="up"), "axes"),
                (lambda d: d["cases"][8]["oracle"]["emotion"]["ideal_scores"].update(shy=5), "ideal_scores"),
                (lambda d: d["cases"][8]["oracle"]["emotion"]["ideal_scores"].pop("shy"), "ideal_scores"),
                (lambda d: d["cases"][8]["oracle"]["emotion"].update(checks=[]), "oracle.emotion"),
                (lambda d: d["cases"][8]["oracle"].pop("emotion"), "oracle.emotion"),
                (lambda d: d["curve_checks"][0]["conditions"].append({"kind": "rise", "field": "pleased",
                                                                       "cases": ["je_001", "je_099"]}), "C1"),
                (lambda d: d["curve_checks"][1].update(id="C1"), "重複"),
                (lambda d: d.pop("curve_checks"), "curve_checks")):
            changed = copy.deepcopy(data)
            mutate(changed)
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                load_suite("jev-emotion", changed)
        changed = copy.deepcopy(data)
        changed["curve_checks"][0]["title"] = "改寫標題"
        with self.assertRaisesRegex(ValueError, "曲線條件"):
            load_suite("jev-emotion", changed)
        chat = load_suite("chat")
        chat["cases"][0]["oracle"]["emotion"] = data["cases"][0]["oracle"]["emotion"]
        with self.assertRaisesRegex(ValueError, "oracle.emotion"):
            load_suite("chat", chat)
        chat = load_suite("chat")
        chat["curve_checks"] = data["curve_checks"]
        with self.assertRaisesRegex(ValueError, "curve_checks"):
            load_suite("chat", chat)

    def test_memory_correction_dependency_skips_unneeded_recall(self):
        cases = select_cases(load_suite("memory"), "mem_006")
        self.assertEqual([case["case_id"] for case in cases], ["mem_003", "mem_005", "mem_006"])
        self.assertEqual([case["case_id"] for case in select_cases(load_suite("memory"), "mem_014")], ["mem_014"])

    def test_integration_replays_actual_prefix_and_preserves_controls(self):
        cases = select_cases(load_suite("integration"), "it_014")
        self.assertEqual(len(cases), 14)
        self.assertEqual([cases[i - 1]["session"] for i in (1, 6, 8, 14)], ["A", "B", "C", "D"])
        self.assertTrue(cases[11]["context"]["compress_before"])
        self.assertTrue(cases[11]["context"]["vision_rejection"])
        self.assertTrue(all(not case["context"]["history"] for case in cases))

    def test_selection_does_not_mutate_source(self):
        data = load_suite("memory")
        before = fingerprint(data)
        selected = select_cases(data, "mem_006")
        selected[0]["context"]["memory_ids"].clear()
        self.assertEqual(fingerprint(data), before)
        with self.assertRaises(ValueError):
            select_cases(data, "case_006")

    def test_replay_rejects_legacy_list_txt_and_unresolved_slots(self):
        with self.assertRaises(ValueError):
            load_suite("memory", [{"case_id": "case_001"}] * 28)
        replay = load_suite("integration", load_suite("integration"))
        with self.assertRaisesRegex(ValueError, "未解析生成槽"):
            select_cases(replay)
        self.assertEqual(len(select_cases(replay, "it_006")), 6)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "old.txt"
            path.write_text("user: test", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_suite("chat", path)

    def test_replay_preserves_resolved_inputs_and_rejects_oracle_drift(self):
        data = load_suite("integration")
        data["cases"][13]["input"] = FOLLOWUP_14
        data["cases"][14]["input"] = FOLLOWUP_15
        replayed = load_suite("integration", data)
        expected = copy.deepcopy(data)
        expected["input_source"] = "replayed_inputs"
        self.assertEqual(replayed, expected)
        changed = copy.deepcopy(data)
        changed["cases"][14]["oracle"]["required_facts"] = []
        with self.assertRaisesRegex(ValueError, "oracle"):
            load_suite("integration", changed)
        changed = copy.deepcopy(data)
        changed["cases"][0]["input"] = "改寫固定輸入"
        with self.assertRaisesRegex(ValueError, "固定輸入"):
            load_suite("integration", changed)

    def test_fixture_cannot_preseed_operation_results(self):
        data = load_suite("memory")
        by_id = {memory["id"]: memory for memory in data["memories"]}
        self.assertEqual(data["cases"][0]["context"]["memory_ids"], [])
        self.assertEqual(data["cases"][2]["context"]["memory_ids"], ["game_minecraft"])
        self.assertNotIn("JEV", by_id["game_minecraft"]["text"])
        integration = load_suite("integration")
        loaded = [by_id[identifier]["text"] for identifier in integration["cases"][0]["context"]["memory_ids"]]
        self.assertFalse(any("草莓蛋糕" in text or "資訊管理系" in text for text in loaded))
        self.assertEqual(integration["memories"], data["memories"])

    def test_chinese_content_and_relative_dates_are_not_corrupted(self):
        for suite in ("chat", "memory", "tools", "integration"):
            data = load_suite(suite)
            self.assertNotIn("??", json.dumps(data, ensure_ascii=False))
            for case in data["cases"]:
                if case["source"] == "fixed":
                    self.assertRegex(case["input"], "[\\u4e00-\\u9fff]")
        memory = load_suite("memory")
        reference = datetime.fromisoformat(memory["reference_time"])
        expected = (reference + timedelta(days=14)).date().isoformat()
        self.assertIn(expected, memory["cases"][11]["oracle"]["required_facts"][1])
        self.assertNotIn("{next_demo_date}", json.dumps(memory, ensure_ascii=False))
        expected_memory = copy.deepcopy(memory)
        expected_memory["input_source"] = "replayed_inputs"
        self.assertEqual(load_suite("memory", memory), expected_memory)

    def test_partial_replay_preserves_selection_and_never_generates_empty_slots(self):
        snapshot = load_suite("integration")
        snapshot["selection"] = [f"it_{index:03}" for index in range(1, 7)]
        replay = load_suite("integration", snapshot)
        self.assertEqual(replay["input_source"], "replayed_inputs")
        self.assertEqual([case["case_id"] for case in select_cases(replay)], snapshot["selection"])
        self.assertEqual(len(select_cases(replay, "it_008")), 8)
        with self.assertRaisesRegex(ValueError, "未解析生成槽"):
            select_cases(replay, "it_014")
        snapshot["cases"][13]["input"] = FOLLOWUP_14
        replay = load_suite("integration", snapshot)
        self.assertEqual(len(select_cases(replay, "it_014")), 14)
        with self.assertRaisesRegex(ValueError, "未解析生成槽"):
            select_cases(replay, "it_015")

    def test_partial_memory_replay_rebuilds_actual_dependency_closure(self):
        snapshot = load_suite("memory")
        snapshot["selection"] = ["mem_003", "mem_005", "mem_006"]
        replay = load_suite("memory", snapshot)
        self.assertEqual([case["case_id"] for case in select_cases(replay)], snapshot["selection"])
        self.assertEqual([case["case_id"] for case in select_cases(replay, "mem_002")], ["mem_001", "mem_002"])

    def test_capacity_and_temporal_distractors_remain_explicit(self):
        data = load_suite("memory")
        capacity = data["cases"][-1]["oracle"]
        self.assertTrue(capacity["capacity"])
        self.assertEqual(len(capacity["required_memory_ids"]), 6)
        self.assertEqual(len(capacity["required_facts"]), 6)
        self.assertNotIn("superseded", data["cases"][5]["oracle"]["forbidden_memory_ids"])
        self.assertEqual(set(data["cases"][11]["oracle"]["forbidden_memory_ids"]),
                         {"corpus_expo_expired", "corpus_other_owner", "corpus_wrong_contract"})
        self.assertEqual(sum(case["context"]["compress_before"] for case in load_suite("chat")["cases"]), 1)
        compression = load_suite("chat")["cases"][-1]["context"]["history"]
        self.assertEqual(len(compression), 12)
        self.assertIn("眼睛", compression[0]["content"])
        self.assertTrue(all("眼睛" not in message["content"] and "阻尼" not in message["content"]
                            for message in compression[-8:]))

    def test_legacy_intents_are_mapped_without_accepting_legacy_format(self):
        legacy = {identifier for suite in ("chat", "memory", "tools", "integration")
                  for case in load_suite(suite)["cases"] for identifier in case["legacy_case_ids"]}
        self.assertTrue({f"case_{index:03}" for index in range(1, 24)} <= legacy)
        self.assertTrue({f"random_{index:03}" for index in range(21, 26)} <= legacy)


def response(value):
    return SimpleNamespace(model="stub-chat", usage=None,
                           choices=[SimpleNamespace(message=SimpleNamespace(content=value))])


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    def client_modules(self, call):
        return patch.dict(sys.modules, {
            "core.config": SimpleNamespace(CHAT_MODEL_NAME="stub-chat"),
            "infrastructure.ai_client": SimpleNamespace(chat_create_with_fallback=call),
        })

    async def test_generator_receives_only_visible_conversation_and_public_request(self):
        case = load_suite("integration")["cases"][13]
        case["oracle"]["private_gold"] = "DO_NOT_SEND_GOLD"
        call = AsyncMock(return_value=response(FOLLOWUP_14))
        diagnostics = {}
        with self.client_modules(call):
            value = await generate_followup(case, [{"input": "忘記我的飲料偏好。", "reply": "已處理。", "request_sent": True,
                "db": "DO_NOT_SEND_DB", "trace": "DO_NOT_SEND_TRACE", "judge": "DO_NOT_SEND_JUDGE"},
                {"user": "DO_NOT_SEND_UNSENT", "reply": "未送出內容", "request_sent": False}], diagnostics)
        self.assertEqual(value, FOLLOWUP_14)
        request = json.dumps(call.call_args.kwargs, ensure_ascii=False)
        self.assertNotIn("DO_NOT_SEND", request)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(diagnostics["status"], "completed")
        self.assertEqual(diagnostics["input_source"], "fresh_generated")
        self.assertEqual(diagnostics["calls"], 1)
        self.assertEqual(len(diagnostics["requests"]), 1)
        self.assertIsNone(diagnostics["requests"][0]["usage"])

    async def test_format_repair_is_bounded_and_does_not_retry_until_correct(self):
        case = load_suite("integration")["cases"][14]
        call = AsyncMock(side_effect=[response("主模型現在負責嗎？"), response(FOLLOWUP_15)])
        diagnostics = {}
        with self.client_modules(call):
            value = await generate_followup(case, [{"role": "assistant", "content": "這些喜好目前未知。", "request_sent": True}], diagnostics)
        self.assertEqual(value, FOLLOWUP_15)
        self.assertEqual(call.await_count, 2)
        self.assertEqual(len(diagnostics["attempts"]), 2)
        self.assertEqual(diagnostics["calls"], 2)
        call = AsyncMock(return_value=response("錯誤格式"))
        with self.client_modules(call), self.assertRaises(RuntimeError):
            await generate_followup(case, [{"role": "assistant", "content": "目前未知。", "request_sent": True}], {})
        self.assertEqual(call.await_count, 2)

    async def test_service_failure_does_not_draw_new_question(self):
        case = load_suite("integration")["cases"][13]
        call = AsyncMock(side_effect=TimeoutError)
        diagnostics = {}
        with self.client_modules(call), self.assertRaises(RuntimeError):
            await generate_followup(case, [{"role": "assistant", "content": "已處理。", "request_sent": True}], diagnostics)
        self.assertEqual(call.await_count, 1)
        self.assertEqual(diagnostics["status"], "failed")
        self.assertEqual(diagnostics["calls"], 1)

    async def test_cost_and_requests_accumulate_across_both_slots(self):
        data = load_suite("integration")
        call = AsyncMock(side_effect=[response(FOLLOWUP_14), response(FOLLOWUP_15)])
        diagnostics = {"calls": 0}
        with self.client_modules(call):
            for index in (13, 14):
                await generate_followup(data["cases"][index], [{"user": "已送出的續問", "reply": "已送出的回答", "request_sent": True}], diagnostics)
        self.assertEqual(diagnostics["calls"], 2)
        self.assertEqual(len(diagnostics["requests"]), 2)
        self.assertEqual(set(diagnostics["cases"]), {"it_014", "it_015"})
        with self.client_modules(call), self.assertRaisesRegex(RuntimeError, "不可重抽"):
            await generate_followup(data["cases"][14], [{"user": "前文", "reply": "回答", "request_sent": True}], diagnostics)
        self.assertEqual(diagnostics["calls"], 2)


if __name__ == "__main__":
    unittest.main()
