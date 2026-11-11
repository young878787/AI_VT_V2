"""聊天卡的純契約：主題白名單、名稱規則、代號轉換、卡內防重複、心情彙總與概要檢查。"""

import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.memory.cards import (
    DOMAINS, aggregate_mood, card_document, card_header, member_fingerprint, normalize_key, turn_mood,
    validate_summary, validate_title,
)
from domain.memory.decisions import normalize_proposal, translate_codes, translate_read_codes, validate_decisions
from services.memory.llm import MemoryLLM


SOURCE = uuid4()
TEXT = "請記住我喜歡烏龍茶"


def create(**fields):
    return {"action": "CREATE", "canonical_text": "使用者喜歡烏龍茶", "memory_type": "preference",
            "importance": .7, "confidence": .9, "reason": "explicit", "search_terms": ["烏龍茶"],
            "source_ids": [str(SOURCE)], **fields}


def propose(operation, cards=None):
    return normalize_proposal(operation, set(), {SOURCE}, TEXT, SOURCE,
                              allowed_cards=set() if cards is None else cards)


class CardRuleTests(unittest.TestCase):
    def test_domain_whitelist_has_eight_fixed_domains(self):
        self.assertEqual(set(DOMAINS), {"self", "preference", "daily", "relation", "project", "event", "bond", "other"})
        with self.assertRaises(ValueError):
            propose(create(domain="food", subtopic="茶飲"))

    def test_title_length_and_content(self):
        self.assertEqual(validate_title(" 茶飲 "), "茶飲")
        self.assertEqual(validate_title("十" * 16), "十" * 16)
        for value in ("茶", "十" * 17, "", "!!", "茶\n飲", 12):
            with self.assertRaises(ValueError, msg=repr(value)):
                validate_title(value)

    def test_normalized_title_key(self):
        self.assertEqual(normalize_key("ＡＩ_VT 專案"), normalize_key("ai vt專案"))
        self.assertEqual(normalize_key("茶 飲！"), "茶飲")
        self.assertNotEqual(normalize_key("茶飲"), normalize_key("紅茶"))


class CardProposalTests(unittest.TestCase):
    def test_create_requires_exactly_one_card_form(self):
        card = uuid4()
        with self.assertRaises(ValueError):
            propose(create())
        with self.assertRaises(ValueError):
            propose(create(card_id=str(card), domain="preference", subtopic="茶飲"), {card})
        with self.assertRaises(ValueError):
            propose(create(domain="preference"))
        self.assertEqual(propose(create(card_id=str(card)), {card})["card_id"], str(card))
        self.assertEqual(propose(create(domain="preference", subtopic=" 茶飲 "))["subtopic"], "茶飲")

    def test_card_outside_candidates_is_rejected(self):
        with self.assertRaises(ValueError):
            propose(create(card_id=str(uuid4())), {uuid4()})

    def test_other_actions_cannot_carry_card_fields(self):
        target = uuid4()
        for extra in ({"card_id": str(uuid4())}, {"domain": "preference", "subtopic": "茶飲"}):
            with self.assertRaises(ValueError, msg=extra):
                validate_decisions({"decisions": [{"action": "REINFORCE", "target_memory_ids": [str(target)],
                                                   "source_ids": [str(SOURCE)], "reason": "support", **extra}]},
                                   {target})
        with self.assertRaises(ValueError):
            validate_decisions({"decisions": [create(domain="preference", subtopic="茶飲",
                                                     card_ids=[str(uuid4()), str(uuid4())])]}, set())

    def test_merge_cards_needs_two_distinct_candidates(self):
        first, second = uuid4(), uuid4()
        merged = propose({"action": "MERGE_CARDS", "card_ids": [str(first), str(second)],
                          "source_ids": [str(SOURCE)], "reason": "same topic"}, {first, second})
        self.assertEqual(merged["card_ids"], [str(first), str(second)])
        for cards in ([str(first)], [str(first), str(first)]):
            with self.assertRaises(ValueError):
                propose({"action": "MERGE_CARDS", "card_ids": cards, "source_ids": [str(SOURCE)],
                         "reason": "same topic"}, {first, second})
        with self.assertRaises(ValueError):
            propose({"action": "MERGE_CARDS", "card_ids": [str(first), str(uuid4())],
                     "source_ids": [str(SOURCE)], "reason": "same topic"}, {first, second})

    def test_translate_codes_maps_short_codes_and_rejects_uuid_or_unknown(self):
        memory, card, other = uuid4(), uuid4(), uuid4()
        codes = {"S1": SOURCE, "M1": memory, "C1": card, "C2": other}
        result = translate_codes({"action": "CREATE", "source_ids": ["S1"], "card": "C1"}, codes)
        self.assertEqual((result["source_ids"], result["card_id"]), ([str(SOURCE)], str(card)))
        self.assertNotIn("card", result)
        merged = translate_codes({"action": "MERGE_CARDS", "cards": ["C1", "C2"], "source_ids": ["S1"]}, codes)
        self.assertEqual(merged["card_ids"], [str(card), str(other)])
        self.assertEqual(translate_read_codes({"memory_ids": ["M1"], "source_ids": ["S1"]}, codes),
                         ({memory}, {SOURCE}))
        for operation in ({"source_ids": [str(SOURCE)]}, {"source_ids": ["S9"]}, {"target_memory_ids": ["S1"]},
                          {"card": str(card)}, {"card": "M1"}, {"cards": "C1"}, {"source_ids": "S1"}):
            with self.assertRaises(ValueError, msg=operation):
                translate_codes(operation, codes)
        with self.assertRaises(ValueError):
            translate_read_codes({"memory_ids": [str(memory)]}, codes)

    def test_same_card_duplicate_create_is_rejected_toward_reinforce(self):
        card = uuid4()
        rows = {card: {"domain": "preference", "title": "茶飲", "member_keys": {normalize_key("使用者喜歡烏龍茶")}}}
        for operation in (create(card_id=str(card), canonical_text="使用者喜歡 烏龍茶！"),
                          create(domain="preference", subtopic="茶 飲")):
            with self.assertRaisesRegex(ValueError, "REINFORCE"):
                MemoryLLM._check_card_duplicate(operation, rows)
        # 不同卡（同名不同主題）或不同要點不算重複。
        MemoryLLM._check_card_duplicate(create(domain="relation", subtopic="茶飲"), rows)
        MemoryLLM._check_card_duplicate(create(card_id=str(card), canonical_text="使用者喜歡紅茶"), rows)
        MemoryLLM._check_card_duplicate({"action": "REINFORCE", "card_id": str(card),
                                         "canonical_text": "使用者喜歡烏龍茶"}, rows)


class CardMoodAndSummaryTests(unittest.TestCase):
    def test_turn_mood_uses_primary_mood_or_calm(self):
        self.assertEqual(turn_mood(None), "calm")
        self.assertEqual(turn_mood({"broken": True}), "calm")

    def test_aggregate_mood_most_frequent_then_most_recent(self):
        now = datetime(2026, 11, 10, tzinfo=timezone.utc)
        self.assertIsNone(aggregate_mood([]))
        self.assertIsNone(aggregate_mood([(None, now), ("excited", now)]))
        self.assertEqual(aggregate_mood([("pleased", now), ("shy", now), ("pleased", now - timedelta(days=1))]),
                         "pleased")
        self.assertEqual(aggregate_mood([("pleased", now - timedelta(days=2)), ("sad_or_hurt", now)]), "sad_or_hurt")
        self.assertEqual(aggregate_mood([("sad_or_hurt", now - timedelta(days=2)), ("pleased", now)]), "pleased")

    def test_validate_summary_length_and_unknown_tokens(self):
        points = ["使用者的朋友小岑喜歡《鏈鋸人》", "使用者每週三次 3 公里慢跑", "使用者用 Python 寫 AI_VT"]
        self.assertEqual(validate_summary(" 小岑喜歡《鏈鋸人》；使用者每週跑 3 公里，用 Python。 ", points),
                         "小岑喜歡《鏈鋸人》；使用者每週跑 3 公里，用 Python。")
        for summary in ("", "長" * 201, None, "使用者每週跑 5 公里", "小美喜歡《鏈鋸人》", "使用者改用 Rust",
                        "喜歡「進擊的巨人」"):
            with self.assertRaises(ValueError, msg=repr(summary)):
                validate_summary(summary, points)

    def test_fingerprint_ignores_order_and_tracks_status(self):
        first = {"id": UUID(int=1), "canonical_text": "甲", "status": "active", "valid_from": None, "valid_to": None}
        second = {"id": UUID(int=2), "canonical_text": "乙", "status": "active", "valid_from": None, "valid_to": None}
        self.assertEqual(member_fingerprint([first, second]), member_fingerprint([second, first]))
        self.assertNotEqual(member_fingerprint([first, second]),
                            member_fingerprint([first, {**second, "status": "expired"}]))

    def test_card_document_and_header(self):
        self.assertEqual(card_document("preference", "茶飲", None, ["甲", "乙"]), "喜好與興趣／茶飲：甲；乙")
        self.assertEqual(card_document("preference", "茶飲", "概要", ["甲"]), "喜好與興趣／茶飲：概要")
        self.assertEqual(card_header("relation", "小岑", "pleased"), "【人際關係／小岑】（露西亞當時的心情：開心）")
        self.assertEqual(card_header(None, None, "calm"), "【其他／未分類】")


if __name__ == "__main__":
    unittest.main()
