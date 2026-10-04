"""單一 agent 的工具迴圈、來源與交易前提回歸。"""
import json
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from domain.memory_decisions import normalize_proposal, agent_tools, validate_batch
from domain.memory_routing import forget_scope, instruction_policy
from services.memory_llm import MemoryLLM, PROMPT
from services.memory_db_manager import _memory_keywords


def tool_response(name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": str(uuid4()), "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


class MemoryAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = {"id": uuid4(), "speaker": "user", "raw_text": "我喜歡茶", "occurred_at": "2026-10-03T12:00:00+08:00"}
        self.fact = {"action": "CREATE", "canonical_text": "使用者喜歡茶", "source_ids": [str(self.source["id"])],
                     "memory_type": "preference", "importance": .7, "confidence": .9, "reason": "user statement",
                     "search_terms": ["茶", "飲品偏好"]}
        self.job = {"id": self.source["id"], "source_text": "我喜歡茶", "instruction": "observe"}
        self.diag = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "input_token_estimate": 0, "latency_ms": 0}

    async def run_agent(self, responses, related=None, search=None, read=None):
        self.agent = object.__new__(MemoryLLM)
        self.agent.call = AsyncMock(side_effect=responses)
        return await self.agent.decide(self.job, [self.source], related or [], search or AsyncMock(return_value=[]),
                                       read or AsyncMock(return_value={}), self.diag)

    def test_sources_no_store_and_numeric_validation(self):
        for field, value in (("source_ids", []), ("source_ids", [str(uuid4())]), ("importance", 3),
                             ("confidence", True), ("confidence", float("nan")), ("subject_key", "健康.飲食")):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                normalize_proposal({**self.fact, field: value}, set(), {self.source["id"]}, "我喜歡茶")
        with self.assertRaises(ValueError):
            normalize_proposal(self.fact, set(), {self.source["id"]}, "不要保存")
        normalized = normalize_proposal(self.fact, set(), {self.source["id"]}, "我喜歡茶")
        self.assertEqual(normalized["retention_class"], "normal")
        self.assertEqual(normalized["target_memory_ids"], [])
        with self.assertRaisesRegex(ValueError, "search_terms"):
            normalize_proposal({key: value for key, value in self.fact.items() if key != "search_terms"},
                               set(), {self.source["id"]}, "我喜歡茶")

    def test_instruction_negation_and_forget_permissions(self):
        denied = (
            ("請幫我寫一句「忘記我的地址」的台詞", "observe"),
            ("忘記我生日的是我朋友", "observe"),
            ("Please write a line saying \"forget my address\".", "observe"),
            ("My friend forgot my birthday.", "observe"),
            ("Forget my birthday was something my friend did.", "observe"),
            ("不要忘記我喜歡茶", "remember"),
            ("Please don't forget my address.", "observe"),
            ("我忘記帶傘了", "observe"),
            ("忘記帶傘了", "observe"),
            ("你記得我喜歡什麼嗎", "observe"),
            ("更正：忘記我生日的是我朋友", "remember"),
            ("更正：我現在不喝拿鐵了", "remember"),
            ("我這週暫時不喝咖啡", "observe"),
            ("請暫時忘記我的地址", "observe"),
            ("Forget my address for now.", "observe"),
            ("暫時不要忘記我的地址", "remember"),
            ("不是請你忘記我的地址，我是要更正它", "remember"),
            ("忘記我的密碼怎麼辦？", "observe"),
            ("忘記我的地址會造成什麼問題？", "observe"),
            ("Forget my birthday was a mistake.", "observe"),
        )
        for text, expected in denied:
            with self.subTest(text=text):
                self.assertEqual(instruction_policy(text), expected)
                schema = next(tool["function"]["parameters"] for tool in agent_tools(
                    instruction_policy(text) == "forget") if tool["function"]["name"] == "propose_operation")
                self.assertNotIn("FORGET", schema["properties"]["operation"]["properties"]["action"]["enum"])

        allowed = (
            "請忘記我的地址",
            "請忘記「這件事」",
            "請只忘記咖啡偏好的最新版本",
            "忘記我的咖啡偏好，包含以前的版本",
            "幫我刪除關於舊住址的記憶",
            "請把我的地址忘掉",
            "請忘記我的地址好嗎？",
            "Please forget my address.",
            "Forget about my old address",
            "Delete my memory about the old address.",
        )
        for text in allowed:
            with self.subTest(text=text):
                self.assertEqual(instruction_policy(text), "forget")
                schema = next(tool["function"]["parameters"] for tool in agent_tools(True)
                              if tool["function"]["name"] == "propose_operation")
                self.assertIn("FORGET", schema["properties"]["operation"]["properties"]["action"]["enum"])

        self.assertEqual(forget_scope("請只忘記咖啡偏好的最新版本"), "version")
        self.assertEqual(forget_scope("請只忘記最新的咖啡偏好"), "version")
        self.assertEqual(forget_scope("請忘記目前的咖啡偏好"), "version")
        for text in (
            "請忘記剛剛那筆咖啡偏好",
            "請只忘記上一筆咖啡偏好",
            "請只忘記最舊的咖啡偏好",
            "請忘記這次的咖啡偏好",
        ):
            with self.subTest(scope_text=text):
                self.assertEqual(forget_scope(text), "version")
        self.assertEqual(forget_scope("忘記我的咖啡偏好，包含以前的版本"), "fact")
        schema = next(tool["function"]["parameters"] for tool in agent_tools(False)
                      if tool["function"]["name"] == "propose_operation")
        self.assertNotIn("FORGET", schema["properties"]["operation"]["properties"]["action"]["enum"])
        target = uuid4()
        proposal = {"action": "FORGET", "source_ids": [str(self.source["id"])],
                    "target_memory_ids": [str(target)], "reason": "erase"}
        for text in ("不要忘記我喜歡茶", "請幫我寫一句「忘記我的地址」的台詞", "忘記我生日的是我朋友"):
            with self.subTest(validator_text=text), self.assertRaises(ValueError):
                normalize_proposal(proposal, {target}, {self.source["id"]}, text)
        normalized = normalize_proposal(proposal, {target}, {self.source["id"]}, "請只忘記咖啡偏好的最新版本")
        self.assertEqual(normalized["forget_scope"], "version")

    def test_memory_prompt_preserves_subject_scope_without_domain_overfitting(self):
        self.assertIn("subject or actor explicit", PROMPT)
        self.assertIn("modality, frequency, uncertainty", PROMPT)
        self.assertIn("search_terms", PROMPT)
        self.assertNotIn("cake/chocolate/parfait", PROMPT)
        self.assertNotIn("Minecraft concerns", PROMPT)

    def test_search_terms_are_validated_and_prioritized_in_index(self):
        schema = next(tool["function"]["parameters"] for tool in agent_tools(False)
                      if tool["function"]["name"] == "propose_operation")
        self.assertIn("search_terms", schema["properties"]["operation"]["properties"])
        keywords = _memory_keywords("使用者週末經常玩 ExampleGame。", "example_game",
                                   ["ExampleGame", "遊戲", "週末遊戲習慣"])
        self.assertEqual(keywords[:3], ["examplegame", "遊戲", "週末遊戲習慣"])
        category_keywords = _memory_keywords("", search_terms=["甜點偏好", "遊戲習慣", "飲料偏好"])
        for keyword in ("甜點", "遊戲", "飲料", "偏好"):
            self.assertIn(keyword, category_keywords)
        with self.assertRaisesRegex(ValueError, "search_terms"):
            normalize_proposal({**self.fact, "search_terms": ["茶", "茶"]}, set(),
                               {self.source["id"]}, "我喜歡茶")

    async def test_minimal_two_round_job_keeps_tool_id_and_no_repeated_decisions(self):
        proposal = tool_response("propose_operation", {"operation": self.fact})
        result = await self.run_agent([proposal, tool_response("finish", {"outcome": "complete", "reason": "done"})])
        self.assertEqual(len(result["decisions"]), 1)
        messages = self.agent.call.call_args_list[1].args[0]
        self.assertEqual(messages[-1]["tool_call_id"], proposal["tool_calls"][0]["id"])
        self.assertEqual(json.loads(messages[-1]["content"]), {"accepted": True, "index": 0})
        self.assertNotIn("tool_results", self.diag)

    async def test_unresolved_overall_project_canonical_is_rejected_then_corrected(self):
        self.job["source_text"] = self.source["raw_text"] = "圖書館系統整體設計追求低功耗"
        invalid = {**self.fact, "memory_type": "project", "canonical_text": "整體設計追求低功耗 (設計目標)"}
        corrected = {**invalid, "canonical_text": "圖書館系統整體設計追求低功耗 (設計目標)"}
        result = await self.run_agent([tool_response("propose_operation", {"operation": invalid}),
            tool_response("propose_operation", {"operation": corrected}),
            tool_response("finish", {"outcome": "complete", "reason": "done"})])
        self.assertEqual([d["canonical_text"] for d in result["decisions"]], [corrected["canonical_text"]])
        self.assertEqual(self.diag["corrections"], 1)
        feedback = json.loads(self.agent.call.call_args_list[1].args[0][-1]["content"])
        self.assertIn("所屬主體", feedback["error"])

    async def test_explicit_project_label_without_proper_name_can_be_stored(self):
        self.job["source_text"] = self.source["raw_text"] = "請記住這個專案前端使用 Vue 3"
        fact = {**self.fact, "memory_type": "project", "canonical_text": "使用者專案前端使用 Vue 3"}
        result = await self.run_agent([tool_response("propose_operation", {"operation": fact}),
            tool_response("finish", {"outcome": "complete", "reason": "done"})])
        self.assertEqual(result["decisions"][0]["canonical_text"], fact["canonical_text"])
        self.assertEqual(self.diag["corrections"], 0)

    async def test_search_then_reinforce_supplied_target(self):
        target = {"id": uuid4(), "canonical_text": "使用者喜歡茶", "status": "active"}
        operation = {"action": "REINFORCE", "source_ids": [str(self.source["id"])],
                     "target_memory_ids": [str(target["id"])], "reason": "same fact"}
        search = AsyncMock(return_value=[target])
        result = await self.run_agent([tool_response("search_memories", {"query": "茶偏好"}),
            tool_response("propose_operation", {"operation": operation}),
            tool_response("finish", {"outcome": "complete", "reason": "done"})], search=search)
        self.assertEqual(result["decisions"][0]["action"], "REINFORCE")
        search.assert_awaited_once()
        self.assertEqual(self.diag["search_calls"], 1)

    async def test_invalid_proposal_repaired_in_same_workflow(self):
        await self.run_agent([tool_response("propose_operation", {"operation": {**self.fact, "subject_key": "非法主題"}}),
            tool_response("propose_operation", {"operation": self.fact}),
            tool_response("finish", {"outcome": "complete", "reason": "done"})])
        self.assertEqual(self.diag["corrections"], 1)

    async def test_duplicate_and_replacement_do_not_add_records(self):
        result = await self.run_agent([tool_response("propose_operation", {"operation": self.fact}),
            tool_response("propose_operation", {"operation": self.fact}),
            tool_response("propose_operation", {"operation": {**self.fact, "canonical_text": "使用者喜歡無糖茶"}, "replace_index": 0}),
            tool_response("finish", {"outcome": "complete", "reason": "done"})])
        self.assertEqual([d["canonical_text"] for d in result["decisions"]], ["使用者喜歡無糖茶"])

    async def test_needs_context_discards_pending_proposals(self):
        result = await self.run_agent([tool_response("propose_operation", {"operation": self.fact}),
            tool_response("finish", {"outcome": "needs_context", "reason": "缺少主體"})])
        self.assertEqual(result["decisions"], [])

    async def test_unknown_source_target_read_and_multi_tool_rejected(self):
        search = AsyncMock(return_value=[])
        multi = tool_response("search_memories", {"query": "茶"})
        multi["tool_calls"] += tool_response("search_memories", {"query": "咖啡"})["tool_calls"]
        with self.assertRaises(ValueError):
            await self.run_agent([multi, tool_response("read_context", {"source_ids": [str(uuid4())]})], search=search)
        search.assert_not_awaited()

    async def test_truncated_target_requires_read_before_mutation(self):
        target = {"id": uuid4(), "canonical_text": "茶" * 601, "status": "active"}
        operation = {"action": "REINFORCE", "target_memory_ids": [str(target["id"])],
                     "source_ids": [str(self.source["id"])], "reason": "support"}
        read = AsyncMock(return_value={"memories": [target], "sources": [], "evidence": []})
        result = await self.run_agent([tool_response("propose_operation", {"operation": operation}),
            tool_response("read_context", {"memory_ids": [str(target["id"])]}),
            tool_response("propose_operation", {"operation": operation}),
            tool_response("finish", {"outcome": "complete", "reason": "done"})], related=[target], read=read)
        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(self.diag["corrections"], 1)

    async def test_older_context_cannot_be_reextracted_for_unrelated_current_input(self):
        old = {"id": uuid4(), "speaker": "user", "raw_text": "我喜歡茶"}
        self.agent = object.__new__(MemoryLLM)
        operation = {**self.fact, "source_ids": [str(old["id"])]}
        self.agent.call = AsyncMock(side_effect=[tool_response("propose_operation", {"operation": operation}),
            tool_response("finish", {"outcome": "ignore", "reason": "沒有新事實"})])
        result = await self.agent.decide(self.job, [self.source, old], [], AsyncMock(), AsyncMock(), self.diag)
        self.assertEqual(result["decisions"], [])
        feedback = self.agent.call.call_args_list[1].args[0][-1]["content"]
        self.assertIn("current_source_id", feedback)

    async def test_no_finish_or_empty_complete_never_commits(self):
        with self.assertRaises(ValueError):
            await self.run_agent([tool_response("finish", {"outcome": "complete", "reason": "done"})] * 3)
        with self.assertRaises(ValueError):
            await self.run_agent([tool_response("propose_operation", {"operation": self.fact})] * 20)

    def test_batch_conflicts_and_mixed_forget_rejected(self):
        target = str(uuid4())
        with self.assertRaises(ValueError):
            validate_batch([{"action": "SUPERSEDE", "target_memory_ids": [target], "canonical_text": "茶"},
                            {"action": "ARCHIVE", "target_memory_ids": [target]}])
        with self.assertRaises(ValueError):
            validate_batch([{"action": "FORGET", "target_memory_ids": [target]}, self.fact | {"target_memory_ids": []}])


if __name__ == "__main__":
    unittest.main()
