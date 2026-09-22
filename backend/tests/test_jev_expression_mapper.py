import asyncio
import httpx
import pathlib
import sys
import unittest
from unittest import mock

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.expression_intent_schema import normalize_expression_intent
from domain.jev_questions import (
    build_jev_state,
    build_questions,
    map_answers_to_intent,
)
from infrastructure.typesafe_client import call_jev


def _full_answers() -> dict:
    return {
        "emotion": {"type": "choice", "choice": "happy", "confidence": 0.91},
        "secondary_emotion": {"type": "choice", "choice": "playful", "confidence": 0.72},
        "performance_mode": {"type": "choice", "choice": "bright_talk", "confidence": 0.68},
        "arc": {"type": "choice", "choice": "steady", "confidence": 0.85},
        "intensity": {"type": "score", "score": 2.3, "confidence": 0.77},
        "energy": {"type": "score", "score": 3.1, "confidence": 0.80},
        "wants_goofy": {"type": "noul", "noul": 0.12},
        "needs_special_blink": {"type": "noul", "noul": 0.05},
    }


class TestMapAnswersToIntent(unittest.TestCase):
    def test_full_confidence_answers_map_to_intent(self):
        intent = map_answers_to_intent(_full_answers())
        self.assertEqual(intent["emotion"], "happy")
        self.assertEqual(intent["secondary_emotion"], "playful")
        self.assertEqual(intent["performance_mode"], "bright_talk")
        self.assertEqual(intent["arc"], "steady")
        self.assertEqual(intent["intensity"], 0.575)
        self.assertEqual(intent["energy"], 0.775)
        self.assertNotIn("must_include", intent)
        self.assertNotIn("blink_style", intent)

    def test_low_confidence_fields_are_dropped(self):
        answers = {
            "emotion": {"type": "choice", "choice": "angry", "confidence": 0.3},
            "performance_mode": {"type": "choice", "choice": "meltdown", "confidence": 0.49},
            "arc": {"type": "choice", "choice": "steady", "confidence": 0.49},
            "intensity": {"type": "score", "score": 4.0, "confidence": 0.2},
            "energy": {"type": "score", "score": 0.0, "confidence": 0.1},
        }
        intent = map_answers_to_intent(answers)
        self.assertEqual(intent, {})

    def test_secondary_none_maps_to_empty_string(self):
        answers = _full_answers()
        answers["secondary_emotion"] = {"type": "choice", "choice": "none", "confidence": 0.9}
        intent = map_answers_to_intent(answers)
        self.assertEqual(intent["secondary_emotion"], "")

    def test_score_clamped_to_unit_range(self):
        answers = _full_answers()
        answers["intensity"] = {"type": "score", "score": 9.0, "confidence": 0.9}
        answers["energy"] = {"type": "score", "score": -1.0, "confidence": 0.9}
        intent = map_answers_to_intent(answers)
        self.assertEqual(intent["intensity"], 1.0)
        self.assertEqual(intent["energy"], 0.0)

    def test_wants_goofy_above_threshold_adds_must_include(self):
        answers = _full_answers()
        answers["wants_goofy"] = {"type": "noul", "noul": 0.85}
        intent = map_answers_to_intent(answers)
        self.assertEqual(intent["must_include"], ["goofy_eye_cross_bias"])

    def test_wants_goofy_below_threshold_no_effect(self):
        answers = _full_answers()
        answers["wants_goofy"] = {"type": "noul", "noul": 0.7}
        intent = map_answers_to_intent(answers)
        self.assertNotIn("must_include", intent)

    def test_special_blink_maps_by_emotion(self):
        answers = _full_answers()
        answers["emotion"] = {"type": "choice", "choice": "shy", "confidence": 0.9}
        answers["needs_special_blink"] = {"type": "noul", "noul": 0.9}
        intent = map_answers_to_intent(answers)
        self.assertEqual(intent["blink_style"], "shy_fast")

    def test_special_blink_without_emotion_uses_focused_pause(self):
        answers = {"needs_special_blink": {"type": "noul", "noul": 0.9}}
        intent = map_answers_to_intent(answers)
        self.assertEqual(intent["blink_style"], "focused_pause")

    def test_mapped_intent_passes_normalize(self):
        intent = map_answers_to_intent(_full_answers())
        normalized = normalize_expression_intent(intent, emotion_state=None)
        self.assertEqual(normalized["emotion"], "happy")
        self.assertEqual(normalized["performance_mode"], "bright_talk")
        self.assertEqual(normalized["arc"], "steady")
        self.assertEqual(normalized["intensity"], 0.575)
        self.assertEqual(normalized["energy"], 0.775)

    def test_empty_intent_falls_back_to_default(self):
        normalized = normalize_expression_intent({}, emotion_state={"primary_emotion": "angry", "intensity": 0.7})
        self.assertEqual(normalized["emotion"], "angry")
        self.assertEqual(normalized["intensity"], 0.7)
        self.assertEqual(normalized["performance_mode"], "smile")


class TestBuildJevState(unittest.TestCase):
    def test_history_strips_tags_and_filters_roles(self):
        history = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "嗨<jpaf_state>{}</jpaf_state>"},
            {"role": "assistant", "content": "你好呀<thinking>思考</thinking>"},
            {"role": "system", "content": "[JPAF Turn 1] weights"},
        ]
        state = build_jev_state("今天天氣如何？", history)
        self.assertEqual(
            state["chat_history"],
            [
                {"role": "user", "text": "嗨"},
                {"role": "assistant", "text": "你好呀"},
            ],
        )
        self.assertEqual(state["user_message"], "今天天氣如何？")
        self.assertNotIn("persona", state)
        self.assertNotIn("last_emotion_state", state)
        self.assertNotIn("last_expression", state)

    def test_history_turns_limit(self):
        history = [
            {"role": "user", "content": f"訊息 {i}"} for i in range(10)
        ]
        state = build_jev_state("現在的訊息", history, history_turns=4)
        self.assertEqual(len(state["chat_history"]), 4)
        self.assertEqual(state["chat_history"][-1]["text"], "訊息 9")

    def test_persona_and_last_state_included_when_present(self):
        class FakeJpaf:
            current_persona = "tsundere"
            dominant = "Ti"
            auxiliary = "Si"
            turn_count = 14

            def weights_inline(self):
                return "Ti:0.42 | Si:0.38"

        state = build_jev_state(
            "原諒你！",
            [{"role": "user", "content": "上一句"}],
            jpaf_session=FakeJpaf(),
            last_emotion_state={
                "primary_emotion": "angry",
                "secondary_emotion": "playful",
                "energy": 0.6,
                "intensity": 0.5,
            },
            last_expression={"summary": "嘴角明顯下壓", "emotion": "angry", "residue": 0.32},
        )
        self.assertEqual(state["persona"]["current_persona"], "tsundere")
        self.assertEqual(state["last_emotion_state"]["primary_emotion"], "angry")
        self.assertEqual(state["last_expression"]["residue"], 0.32)

    def test_last_emotion_none_values_removed(self):
        state = build_jev_state(
            "嗨",
            [],
            last_emotion_state={"primary_emotion": "happy", "secondary_emotion": None},
        )
        self.assertEqual(state["last_emotion_state"], {"primary_emotion": "happy"})


class TestBuildQuestions(unittest.TestCase):
    def test_question_types_and_criteria(self):
        questions = build_questions()
        self.assertEqual(
            sorted(questions.keys()),
            [
                "arc",
                "emotion",
                "energy",
                "intensity",
                "needs_special_blink",
                "performance_mode",
                "secondary_emotion",
                "wants_goofy",
            ],
        )
        self.assertEqual(questions["emotion"]["type"], "choice")
        self.assertEqual(questions["intensity"]["type"], "score")
        self.assertEqual(questions["wants_goofy"]["type"], "noul")
        self.assertEqual(len(questions["emotion"]["criteria"]), 10)
        self.assertEqual(len(questions["performance_mode"]["criteria"]), 12)
        self.assertEqual(len(questions["arc"]["criteria"]), 6)
        self.assertEqual(len(questions["intensity"]["criteria"]), 5)
        self.assertEqual(len(questions["energy"]["criteria"]), 5)


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = mock.Mock()
            response = mock.Mock(
                status_code=self.status_code,
                text=self._payload if isinstance(self._payload, str) else repr(self._payload),
            )
            raise httpx.HTTPStatusError(
                f"error {self.status_code}", request=request, response=response
            )

    def json(self):
        return self._payload


class _FakeAsyncClient:
    fake_response = _FakeResponse({})
    raise_on_post: Exception | None = None
    last_payload: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.last_payload = {"url": url, "headers": headers, "json": json}
        if _FakeAsyncClient.raise_on_post is not None:
            raise _FakeAsyncClient.raise_on_post
        return _FakeAsyncClient.fake_response


class TestCallJev(unittest.TestCase):
    def setUp(self):
        _FakeAsyncClient.fake_response = _FakeResponse({})
        _FakeAsyncClient.raise_on_post = None
        _FakeAsyncClient.last_payload = None

    def _patch_client(self):
        return mock.patch("infrastructure.typesafe_client.httpx.AsyncClient", return_value=_FakeAsyncClient())

    def test_success_returns_answers(self):
        _FakeAsyncClient.fake_response = _FakeResponse(
            {"model": "typesafe/jev-1.13", "answers": {"emotion": {"type": "choice", "choice": "happy"}}}
        )
        with self._patch_client():
            answers = asyncio.run(call_jev({"user_message": "嗨"}, {"emotion": {"type": "choice", "instructions": "?"}}))
        self.assertEqual(answers["emotion"]["choice"], "happy")
        self.assertEqual(
            _FakeAsyncClient.last_payload["url"],
            "https://openrouter.ai/api/v1/systemone",
        )
        self.assertIn("Bearer ", _FakeAsyncClient.last_payload["headers"]["Authorization"])
        self.assertEqual(_FakeAsyncClient.last_payload["json"]["model"], "jev-latest")

    def test_timeout_returns_none(self):
        _FakeAsyncClient.raise_on_post = httpx.TimeoutException("timeout")
        with self._patch_client():
            answers = asyncio.run(call_jev({}, {}))
        self.assertIsNone(answers)

    def test_http_error_returns_none(self):
        _FakeAsyncClient.fake_response = _FakeResponse({"error": "rate limited"}, status_code=429)
        with self._patch_client():
            answers = asyncio.run(call_jev({}, {}))
        self.assertIsNone(answers)

    def test_missing_answers_returns_none(self):
        _FakeAsyncClient.fake_response = _FakeResponse({"usage": {}})
        with self._patch_client():
            answers = asyncio.run(call_jev({}, {}))
        self.assertIsNone(answers)

    def test_missing_api_key_returns_none(self):
        with mock.patch("infrastructure.typesafe_client.os.getenv", return_value=""):
            answers = asyncio.run(call_jev({}, {}))
        self.assertIsNone(answers)


if __name__ == "__main__":
    unittest.main()
