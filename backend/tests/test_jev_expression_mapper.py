import asyncio
import math
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.emotion_state import (
    EMOTION_FIELDS,
    NEUTRAL_EMOTION_STATE,
    PERSONALITY,
    resolve_emotion_state,
    state_from_jev_answers,
    validate_emotion_state,
)
from domain.jev_questions import (
    build_action_context,
    build_action_questions,
    build_emotion_context,
    build_emotion_questions,
    map_answers_to_intent,
)
from api.routes.memory_router import reset_memory
from infrastructure.memory_store import (
    load_session_emotion_state,
    load_session_messages,
    reset_session_emotion_state,
    save_session_emotion_state,
    save_session_messages,
)


def emotion_answers(score=0.6):
    return {field: {"type": "noul", "noul": score} for field in EMOTION_FIELDS}


def action_answers():
    return {
        "emotion": {"type": "choice", "choice": "shy", "confidence": 0.9},
        "secondary_emotion": {"type": "choice", "choice": "none", "confidence": 0.9},
        "performance_mode": {"type": "choice", "choice": "awkward", "confidence": 0.9},
        "arc": {"type": "choice", "choice": "steady", "confidence": 0.9},
        "intensity": {"type": "score", "score": 2.4, "confidence": 0.9},
        "energy": {"type": "score", "score": 2, "confidence": 0.9},
        "wants_goofy": {"type": "noul", "noul": 0.1},
        "needs_special_blink": {"type": "noul", "noul": 0.8},
    }


class EmotionContractTests(unittest.TestCase):
    def test_six_independent_noul_questions(self):
        questions = build_emotion_questions()
        self.assertEqual(set(questions), set(EMOTION_FIELDS))
        self.assertTrue(all(item["type"] == "noul" for item in questions.values()))
        self.assertEqual(state_from_jev_answers(emotion_answers()), dict.fromkeys(EMOTION_FIELDS, 0.6))

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
        self.assertEqual(set(context), {"personality", "recent_dialogue", "current_user_input", "previous_emotion_state"})
        self.assertEqual(context["personality"], PERSONALITY)
        self.assertEqual(len(context["recent_dialogue"]), 16)
        self.assertEqual(context["recent_dialogue"][0]["text"], "user 2")
        self.assertNotIn("latest", str(context["recent_dialogue"]))
        self.assertNotIn("memory secret", str(context))

    def test_action_uses_same_state_and_maps_to_compiler_intent(self):
        emotion = dict.fromkeys(EMOTION_FIELDS, 0.5)
        context = build_emotion_context("hello", [], None)
        action_context = build_action_context(context, emotion, {"emotion": "happy"})
        self.assertIs(action_context["current_emotion_state"], emotion)
        self.assertEqual(set(build_action_questions()), set(action_answers()))
        intent = map_answers_to_intent(action_answers())
        self.assertEqual(intent["emotion"], "shy")
        self.assertEqual(intent["intensity"], 0.6)
        self.assertEqual(intent["blink_style"], "shy_fast")

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

    def test_rest_reset_clears_only_selected_session_state_and_messages(self):
        with tempfile.TemporaryDirectory() as directory, \
            mock.patch("infrastructure.memory_store.EMOTION_STATE_DIR", directory + "/emotions"), \
            mock.patch("infrastructure.memory_store.CHAT_SESSION_DIR", directory + "/sessions"), \
            mock.patch("infrastructure.memory_store.MEMORY_DIR", directory), \
            mock.patch("infrastructure.memory_store.USER_PROFILE_PATH", directory + "/profile.json"), \
            mock.patch("infrastructure.memory_store.MEMORY_MD_PATH", directory + "/memory.md"):
            state = dict(NEUTRAL_EMOTION_STATE)
            for session_id in ("session_1", "session_2"):
                save_session_emotion_state(session_id, state)
                save_session_messages(session_id, [{"role": "user", "content": "hello"}])
            asyncio.run(reset_memory("session_1"))
            self.assertIsNone(load_session_emotion_state("session_1"))
            self.assertEqual(load_session_messages("session_1"), [])
            self.assertEqual(load_session_emotion_state("session_2"), state)
            self.assertEqual(len(load_session_messages("session_2")), 1)


if __name__ == "__main__":
    unittest.main()
