"""兩種角色權限、來源與指令政策的回歸測試。"""
import pathlib
import sys
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from domain.memory_intake import validate_intake
from domain.memory_routing import instruction_policy
from services.memory_llm import MemoryLLM, librarian_tools


class MemoryAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = {"id": uuid4(), "speaker": "user", "raw_text": "我喜歡茶"}
        self.fact = {"canonical_text": "使用者喜歡茶", "source_ids": [str(self.source["id"])],
                     "intent": "fact", "memory_type": "preference", "importance": .7,
                     "confidence": .9, "retention_class": "normal", "reason": "user statement"}

    def test_intake_cannot_write_or_cite_assistant(self):
        for name, sources in (("create_memory", [self.source]),
                              ("accept_candidates", [{**self.source, "speaker": "assistant"}]),
                              ("accept_candidates", [])):
            with self.assertRaises(ValueError):
                validate_intake(name, {"candidates": [self.fact]}, sources, "我喜歡茶")
        self.assertEqual(validate_intake("accept_candidates", {"candidates": [self.fact]},
                                        [self.source], "我喜歡茶")["route"], "candidate")

    def test_instruction_negation_and_no_store(self):
        for text in ("不要忘記我喜歡茶", "我忘記帶傘了", "我忘記我喜歡什麼了", "你記得我喜歡什麼嗎"):
            self.assertNotEqual(instruction_policy(text), "forget")
        self.assertEqual(instruction_policy("忘記我的地址"), "forget")
        self.assertEqual(instruction_policy("不要記住這件事"), "no_store")
        with self.assertRaises(ValueError):
            validate_intake("accept_candidates", {"candidates": [self.fact]}, [self.source], "不要記住這件事")
        with self.assertRaises(ValueError):
            validate_intake("accept_candidates", {"candidates": [{**self.fact, "intent": "forget"}]}, [self.source], "不要忘記我喜歡茶")
        self.assertNotIn("forget_memory", {item["function"]["name"] for item in librarian_tools(False)})

    def test_intake_rejects_out_of_range_model_scores(self):
        for field in ("importance", "confidence"):
            for value in (-1, 3):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    validate_intake("accept_candidates", {
                        "candidates": [{**self.fact, field: value}],
                    }, [self.source], "我喜歡茶")

    async def test_librarian_requires_every_candidate_and_keeps_sources(self):
        llm = object.__new__(MemoryLLM)
        llm.call = AsyncMock(return_value=([("create_memory", {"candidate_index": 0, "target_memory_ids": [], "reason": "new"})], {}))
        job = {"source_text": "我喜歡茶", "reviewed_candidates": [self.fact]}
        decisions, _ = await llm.decide(job, [], [])
        self.assertEqual(decisions[0]["source_ids"], self.fact["source_ids"])
        with self.assertRaises(ValueError):
            await llm.decide({**job, "reviewed_candidates": [self.fact, self.fact]}, [], [])
        llm.call.return_value = ([("forget_memory", {"candidate_index": 0, "target_memory_ids": [str(uuid4())], "reason": "erase"})], {})
        with self.assertRaises(ValueError):
            await llm.decide(job, [], [])
