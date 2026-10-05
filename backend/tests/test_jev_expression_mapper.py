import asyncio
import math
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.emotion_state import (
    CHARACTER_EXPRESSION_PROFILE,
    EMOTION_FIELDS,
    NEUTRAL_EMOTION_STATE,
    PERSONALITY,
    resolve_emotion_state,
    state_from_jev_answers,
    validate_emotion_state,
)
from domain.jev_questions import (
    build_action_questions,
    build_emotion_context,
    build_emotion_questions,
    build_jev_context,
    build_jev_questions,
    build_memory_questions,
    map_answers_to_intent,
)
from api.routes.memory_router import reset_memory
from infrastructure.memory_store import (
    load_session_emotion_state,
    reset_session_emotion_state,
    save_session_emotion_state,
    to_persistable_messages,
)
from backend.tests.chat_session_fakes import make_chat_session_service


def emotion_answers(score=0.6):
    return {field: {"type": "noul", "noul": score} for field in EMOTION_FIELDS}


def action_answers():
    return {
        "base_emotion": {"type": "choice", "choice": "shy", "confidence": 0.9},
        "interaction_attitude": {"type": "choice", "choice": "awkward", "confidence": 0.9},
        "arc": {"type": "choice", "choice": "steady", "confidence": 0.9},
        "intensity": {"type": "score", "score": 2.4, "confidence": 0.9},
        "energy": {"type": "score", "score": 2, "confidence": 0.9},
        "wants_goofy": {"type": "noul", "noul": 0.1},
        "needs_special_blink": {"type": "noul", "noul": 0.8},
    }


def combined_answers(score=0.6):
    return {**emotion_answers(score), **action_answers()}


class EmotionContractTests(unittest.TestCase):
    def test_six_independent_noul_questions(self):
        questions = build_emotion_questions()
        self.assertEqual(set(questions), set(EMOTION_FIELDS))
        self.assertTrue(all(item["type"] == "noul" for item in questions.values()))
        self.assertEqual(state_from_jev_answers(emotion_answers()), dict.fromkeys(EMOTION_FIELDS, 0.6))
        for item in questions.values():
            instructions = item["instructions"]
            self.assertIn("當輪可觀察線索為最高優先", instructions)
            self.assertIn("不能獨立構成情緒證據", instructions)
            self.assertIn("previous_emotion_state 只供連續性參考", instructions)
            self.assertIn("否認本身不足以提高分數", instructions)

    def test_rejects_partial_extra_invalid_and_nonfinite_states_atomically(self):
        valid = dict(NEUTRAL_EMOTION_STATE)
        invalid_cases = [
            {key: value for key, value in valid.items() if key != "shy"},
            {**valid, "extra": 0.1},
            {**valid, "shy": True},
            {**valid, "shy": "0.2"},
            {**valid, "shy": math.nan},
            {**valid, "shy": math.inf},
            {**valid, "shy": 1.1},
        ]
        for state in invalid_cases:
            with self.subTest(state=state):
                self.assertIsNone(validate_emotion_state(state))
        self.assertIsNone(state_from_jev_answers({**emotion_answers(), "extra": {"type": "noul", "noul": 0.2}}))

    def test_complete_previous_or_neutral_fallback(self):
        previous = dict.fromkeys(EMOTION_FIELDS, 0.7)
        invalid = emotion_answers()
        invalid.pop("sad_or_hurt")
        self.assertEqual(resolve_emotion_state(invalid, previous), (previous, "previous_fallback"))
        self.assertEqual(resolve_emotion_state(None, None), (NEUTRAL_EMOTION_STATE, "neutral_fallback"))

    def test_context_contains_only_fixed_personality_recent_dialogue_and_previous_state(self):
        history = [{"role": "system", "content": "memory secret"}]
        for index in range(10):
            history.extend([
                {"role": "user", "content": f"user {index}"},
                {"role": "assistant", "content": f"reply {index}"},
            ])
        context = build_emotion_context("latest", history, NEUTRAL_EMOTION_STATE)
        self.assertEqual(set(context), {"character_expression_profile", "recent_dialogue", "current_user_input", "previous_emotion_state"})
        self.assertEqual(context["character_expression_profile"], CHARACTER_EXPRESSION_PROFILE)
        self.assertNotIn("personality", context)
        self.assertEqual(len(context["recent_dialogue"]), 16)
        self.assertEqual(context["recent_dialogue"][0]["text"], "user 2")
        self.assertNotIn("latest", str(context["recent_dialogue"]))
        self.assertNotIn("memory secret", str(context))

    def test_single_call_context_and_questions_contain_emotion_and_two_axes(self):
        context = build_jev_context(
            "hello", [], None, {"emotion": "happy"}, "memory", {"status": "started"},
        )
        self.assertEqual(context["interaction_personality"], PERSONALITY)
        self.assertEqual(context["previous_expression_carry_state"], {"emotion": "happy"})
        self.assertEqual(context["relevant_memory"], "memory")
        self.assertEqual(context["current_action"], {"status": "started"})
        self.assertEqual(set(build_action_questions()), set(action_answers()))
        self.assertEqual(set(build_jev_questions()), set(combined_answers()) | set(build_memory_questions()))
        primary_instructions = build_action_questions()["base_emotion"]["instructions"]
        self.assertIn("一般友善、輕微正向或想繼續互動仍選 neutral", primary_instructions)
        self.assertIn("聯合判斷", primary_instructions)
        self.assertIn("不代表露西亞必然具有相同情緒", primary_instructions)
        self.assertIn("共同互動很開心時可選 happy", primary_instructions)
        attitude_instructions = build_action_questions()["interaction_attitude"]["instructions"]
        self.assertIn("要求開玩笑或調皮回應時優先考慮 cheeky_wink", attitude_instructions)
        intent = map_answers_to_intent(action_answers())
        self.assertEqual(intent["emotion"], "shy")
        self.assertEqual(intent["performance_mode"], "awkward")
        self.assertNotIn("secondary_emotion", intent)
        self.assertEqual(intent["intensity"], 0.6)
        self.assertEqual(intent["blink_style"], "shy_fast")

    def test_two_choices_are_passed_through_without_pair_mapping(self):
        questions = build_action_questions()
        self.assertNotIn("playful", questions["base_emotion"]["criteria"])
        self.assertIn("cheeky_wink", questions["interaction_attitude"]["criteria"])
        answers = action_answers()
        answers["base_emotion"]["choice"] = "happy"
        answers["interaction_attitude"]["choice"] = "deadpan"
        intent = map_answers_to_intent(answers)
        self.assertEqual(intent["emotion"], "happy")
        self.assertEqual(intent["performance_mode"], "deadpan")

    def test_action_nonfinite_scores_cannot_reach_compiler(self):
        answers = action_answers()
        answers["intensity"]["score"] = math.nan
        answers["energy"]["score"] = math.inf
        intent = map_answers_to_intent(answers)
        self.assertNotIn("intensity", intent)
        self.assertNotIn("energy", intent)

    def test_session_state_is_isolated_validated_and_reset(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "infrastructure.memory_store.EMOTION_STATE_DIR", directory
        ):
            first = dict.fromkeys(EMOTION_FIELDS, 0.2)
            second = dict.fromkeys(EMOTION_FIELDS, 0.8)
            save_session_emotion_state("session_1", first)
            save_session_emotion_state("session_2", second)
            self.assertEqual(load_session_emotion_state("session_1"), first)
            self.assertEqual(load_session_emotion_state("session_2"), second)
            reset_session_emotion_state("session_1")
            self.assertIsNone(load_session_emotion_state("session_1"))
            self.assertEqual(load_session_emotion_state("session_2"), second)
            with self.assertRaises(ValueError):
                save_session_emotion_state("../unsafe", first)

    def test_rest_reset_uses_atomic_owner_chat_and_memory_boundary(self):
        chat_sessions = make_chat_session_service([{"role": "user", "content": "hello"}])

        async def reset_both(repository, session_id):
            return await repository.reset(session_id)

        runtime = SimpleNamespace(reset_chat_and_memory=mock.AsyncMock(side_effect=reset_both))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            memory_runtime=runtime, chat_session_service=chat_sessions,
        )))
        asyncio.run(reset_memory(request=request))
        runtime.reset_chat_and_memory.assert_awaited_once_with(
            chat_sessions.repository, "server_session",
        )
        self.assertEqual(chat_sessions.repository.messages, [])
        self.assertEqual(chat_sessions.repository.generation, 1)

    def test_interrupted_assistant_message_preserves_status(self):
        messages = [
            {"role": "user", "content": "第一句"},
            {"role": "assistant", "content": "只送出的半句", "status": "interrupted"},
            {"role": "assistant", "content": "不應保存的未知狀態", "status": "unexpected"},
        ]
        self.assertEqual(
            to_persistable_messages(messages),
            [
                {"role": "user", "content": "第一句"},
                {"role": "assistant", "content": "只送出的半句", "status": "interrupted"},
                {"role": "assistant", "content": "不應保存的未知狀態"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
