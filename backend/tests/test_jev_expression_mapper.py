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
    validate_emotion_state,
)
from domain.jev_questions import (
    JEV_ROLES,
    build_action_context,
    build_action_questions,
    build_emotion_context,
    build_emotion_questions,
    build_memory_context,
    build_role_questions,
    map_answers_to_intent,
    question_hash,
)
from api.routes.memory_router import reset_memory
from infrastructure.memory.store import (
    load_session_emotion_state,
    reset_session_emotion_state,
    save_session_emotion_state,
    to_persistable_messages,
)
from backend.tests.chat_session_fakes import make_chat_session_service


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


class EmotionContractTests(unittest.TestCase):
    def test_six_mood_change_score_questions(self):
        questions = build_emotion_questions()
        self.assertEqual(set(questions), set(EMOTION_FIELDS))
        self.assertTrue(all(item["type"] == "score" for item in questions.values()))
        for field, item in questions.items():
            criteria = item["criteria"]
            self.assertEqual(len(criteria), 5)
            self.assertEqual([level[0] for level in criteria], list("01234"))
            if field == "masking_positive_feeling":
                self.assertIn("撒嬌", criteria[0])
                self.assertIn("嘴硬", criteria[4])
                self.assertIn("2 跟平常一樣", criteria[2])
            elif field == "wants_continue_interaction":
                self.assertIn("結束話題", criteria[0])
                self.assertIn("2 照常回應", criteria[2])
            else:
                self.assertIn("2 沒影響", criteria[2])
            instructions = item["instructions"]
            self.assertTrue(instructions.startswith("你是露西亞，死靈法師青少女。"))
            self.assertIn("跟這題無關就給 2", instructions)
            self.assertIn("兩邊都會用到", instructions)
            self.assertIn("不再加分", instructions)
            self.assertIn("turns_ago=1 是上一輪", instructions)
            self.assertNotIn("升高還是降低", instructions)
            self.assertNotIn("previous_emotion_state", instructions)
        self.assertIn("你自己嘴硬、吐槽不算生氣", questions["genuinely_angry"]["instructions"])

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

    def test_context_contains_only_fixed_personality_recent_dialogue_and_mood(self):
        history = [{"role": "system", "content": "memory secret"}]
        for index in range(10):
            history.extend([
                {"role": "user", "content": f"user {index}"},
                {"role": "assistant", "content": f"reply {index}"},
            ])
        context = build_emotion_context("latest", history, NEUTRAL_EMOTION_STATE)
        self.assertEqual(set(context), {"character_expression_profile", "recent_dialogue", "current_user_input",
                                        "current_mood", "mood_history"})
        self.assertEqual(context["current_mood"], NEUTRAL_EMOTION_STATE)
        self.assertEqual(context["mood_history"], [])
        self.assertEqual(context["character_expression_profile"], CHARACTER_EXPRESSION_PROFILE)
        self.assertNotIn("personality", context)
        self.assertEqual(len(context["recent_dialogue"]), 6)
        self.assertEqual(context["recent_dialogue"][0]["text"], "user 7")
        self.assertNotIn("latest", str(context["recent_dialogue"]))
        self.assertNotIn("memory secret", str(context))

    def test_mood_context_rounds_mood_and_maps_history_to_actual_turns(self):
        mood = {**NEUTRAL_EMOTION_STATE, "pleased": 0.34567, "masking_positive_feeling": 0.5749}
        # t3 沒有變化紀錄（JEV 失敗或全部不變）；turns_ago 仍依實際輪次，不依紀錄筆數。
        dialogue = []
        for index in range(1, 6):
            dialogue.extend([{"role": "user", "content": f"user {index}", "turn_id": f"t{index}"},
                             {"role": "assistant", "content": f"reply {index}", "turn_id": f"t{index}"}])
        history = [{"turn_id": turn_id, "changes": {"pleased": value}}
                   for turn_id, value in (("t1", 0.1), ("t2", 0.2), ("t4", 0.4), ("gone", 0.9))]
        context = build_emotion_context("hi", dialogue, mood, history)
        self.assertEqual(context["current_mood"]["pleased"], 0.35)
        self.assertEqual(context["current_mood"]["masking_positive_feeling"], 0.57)
        self.assertEqual(context["mood_history"], [
            {"turns_ago": 4, "changes": {"pleased": 0.2}},
            {"turns_ago": 2, "changes": {"pleased": 0.4}},
        ])
        self.assertNotIn("turn_id", str(context["mood_history"]))

    def test_three_role_contexts_contain_only_their_own_fields(self):
        history = [{"role": "system", "content": "memory secret"}]
        for index in range(5):
            history.extend([
                {"role": "user", "content": f"user {index}" + "x" * 600},
                {"role": "assistant", "content": f"reply {index}"},
            ])
        emotion = build_emotion_context("hello", history, NEUTRAL_EMOTION_STATE)
        action = build_action_context("hello", history, {"emotion": "happy"}, {"status": "started"})
        memory = build_memory_context("hello", history)
        self.assertEqual(set(emotion), {"current_user_input", "recent_dialogue", "character_expression_profile",
                                        "current_mood", "mood_history"})
        self.assertEqual(set(action), {"current_user_input", "recent_dialogue", "interaction_personality",
                                       "previous_expression_carry_state", "current_action"})
        self.assertEqual(set(build_action_context("hello", history, None, None)),
                         {"current_user_input", "recent_dialogue", "interaction_personality"})
        self.assertEqual(set(memory), {"current_user_input", "recent_dialogue"})
        self.assertEqual(action["interaction_personality"], PERSONALITY)
        self.assertEqual(action["previous_expression_carry_state"], {"emotion": "happy"})
        self.assertEqual(action["current_action"], {"status": "started"})
        self.assertEqual(emotion["recent_dialogue"], action["recent_dialogue"])
        self.assertEqual(emotion["recent_dialogue"], memory["recent_dialogue"])
        self.assertEqual(len(memory["recent_dialogue"]), 6)
        self.assertTrue(all(len(item["text"]) <= 500 for item in memory["recent_dialogue"]))
        self.assertNotIn("hello", str(memory["recent_dialogue"]))
        self.assertNotIn("memory secret", str([emotion, action, memory]))

    def test_role_questions_keep_previous_output_schema(self):
        roles = build_role_questions()
        self.assertEqual(set(roles), JEV_ROLES)
        self.assertEqual(set(roles["emotion"]), set(EMOTION_FIELDS))
        self.assertEqual(set(roles["action"]), set(action_answers()))
        self.assertEqual(set(roles["memory"]), {"memory_noise"})
        self.assertEqual(sum(len(questions) for questions in roles.values()), 14)
        self.assertEqual(len({question_hash(questions) for questions in roles.values()}), 3)
        for item in roles["emotion"].values():
            self.assertNotIn("relevant_memory", item["instructions"])
        for item in roles["action"].values():
            self.assertNotIn("六欄", item["instructions"])
        self.assertEqual(question_hash(build_action_questions()), question_hash(roles["action"]))
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
            "infrastructure.memory.store.EMOTION_STATE_DIR", directory
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
