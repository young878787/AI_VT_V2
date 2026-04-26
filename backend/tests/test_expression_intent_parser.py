import pathlib
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.expression_intent_parser import parse_expression_intent


class ExpressionIntentParserTests(unittest.TestCase):
    def test_parse_expression_intent_keeps_valid_payload(self):
        intent = parse_expression_intent(
            '{"emotion":"playful","intensity":0.72,"energy":0.68,'
            '"arc":"pop_then_settle","hold_ms":1800,"blink_style":"teasing_pause"}',
            emotion_state={"primary_emotion": "neutral", "intensity": 0.2},
            previous_state=None,
        )

        self.assertEqual(intent["emotion"], "playful")
        self.assertEqual(intent["arc"], "pop_then_settle")
        self.assertEqual(intent["blink_style"], "teasing_pause")
        self.assertAlmostEqual(intent["intensity"], 0.72)

    def test_parse_expression_intent_fills_defaults_from_emotion_state(self):
        intent = parse_expression_intent(
            '{"emotion":"shy"}',
            emotion_state={"intensity": 0.61, "energy": 0.33},
            previous_state={"summary": "上一輪是輕微微笑"},
        )

        self.assertEqual(intent["emotion"], "shy")
        self.assertAlmostEqual(intent["intensity"], 0.61)
        self.assertAlmostEqual(intent["energy"], 0.33)
        self.assertEqual(intent["arc"], "steady")
        self.assertEqual(intent["hold_ms"], 1600)

    def test_parse_expression_intent_replaces_invalid_enum_with_default(self):
        intent = parse_expression_intent(
            '{"emotion":"playful","arc":"explode_forever","blink_style":"laser"}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["arc"], "steady")
        self.assertEqual(intent["blink_style"], "normal")

    def test_parse_expression_intent_returns_safe_default_when_json_is_missing(self):
        intent = parse_expression_intent(
            '我覺得這句要有點調皮但不要太誇張',
            emotion_state={"primary_emotion": "playful", "intensity": 0.4, "energy": 0.5},
            previous_state=None,
        )

        self.assertEqual(intent["emotion"], "playful")
        self.assertEqual(intent["arc"], "steady")
        self.assertEqual(intent["blink_style"], "normal")

    def test_parse_expression_intent_extracts_first_complete_json_object(self):
        intent = parse_expression_intent(
            '前文 {"emotion":"playful","intensity":0.7} 後文 '
            '{"emotion":"shy","intensity":0.2}',
            emotion_state={"primary_emotion": "neutral", "intensity": 0.1, "energy": 0.2},
            previous_state=None,
        )

        self.assertEqual(intent["emotion"], "playful")
        self.assertAlmostEqual(intent["intensity"], 0.7)

    def test_parse_expression_intent_validates_emotion_state_primary_emotion_fallback(self):
        intent = parse_expression_intent(
            '不是 JSON',
            emotion_state={"primary_emotion": "laser", "intensity": 0.4, "energy": 0.5},
            previous_state=None,
        )

        self.assertEqual(intent["emotion"], "neutral")

    def test_parse_expression_intent_normalizes_remaining_schema_fields(self):
        intent = parse_expression_intent(
            '{"emotion":"playful","secondary_emotion":"laser","dominance":2,'
            '"playfulness":-1,"warmth":"hot","asymmetry_bias":"extreme","tempo":"warp"}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["secondary_emotion"], "")
        self.assertEqual(intent["dominance"], 1.0)
        self.assertEqual(intent["playfulness"], 0.0)
        self.assertEqual(intent["warmth"], 0.5)
        self.assertEqual(intent["asymmetry_bias"], "auto")
        self.assertEqual(intent["tempo"], "medium")

    def test_parse_expression_intent_returns_independent_list_fields(self):
        first = parse_expression_intent(
            '{}',
            emotion_state=None,
            previous_state=None,
        )
        second = parse_expression_intent(
            '{}',
            emotion_state=None,
            previous_state=None,
        )

        first["must_include"].append("smirk_left")
        first["avoid"].append("freeze")

        self.assertEqual(second["must_include"], [])
        self.assertEqual(second["avoid"], [])

    def test_parse_expression_intent_maps_common_emotion_and_arc_aliases(self):
        intent = parse_expression_intent(
            '{"emotion":"joyful","secondary_emotion":"cheerful","arc":"neutral_to_smile"}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["emotion"], "happy")
        self.assertEqual(intent["secondary_emotion"], "happy")
        self.assertEqual(intent["arc"], "pause_then_smirk")

    def test_parse_expression_intent_backward_compat_primary_emotion(self):
        intent = parse_expression_intent(
            '{"primary_emotion":"happy","intensity":0.7}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["emotion"], "happy")
        self.assertEqual(intent["primary_emotion"], "happy")

    def test_parse_expression_intent_parses_performance_mode(self):
        intent = parse_expression_intent(
            '{"emotion":"playful","performance_mode":"goofy_face","intensity":0.8}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["emotion"], "playful")
        self.assertEqual(intent["performance_mode"], "goofy_face")

    def test_parse_expression_intent_parses_topic_guard(self):
        intent = parse_expression_intent(
            '{"emotion":"sad","performance_mode":"awkward",'
            '"topic_guard":{"must_preserve_theme":true,"source_theme":"crying","allow_style_override":false}}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["emotion"], "sad")
        self.assertEqual(intent["performance_mode"], "awkward")
        self.assertEqual(intent["topic_guard"]["source_theme"], "crying")
        self.assertTrue(intent["topic_guard"]["must_preserve_theme"])

    def test_parse_expression_intent_rejects_invalid_performance_mode(self):
        intent = parse_expression_intent(
            '{"emotion":"happy","performance_mode":"laser_mode"}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["emotion"], "happy")
        self.assertEqual(intent["performance_mode"], "smile")

    def test_parse_expression_intent_rejects_invalid_topic_guard_theme(self):
        intent = parse_expression_intent(
            '{"emotion":"sad","topic_guard":{"source_theme":"laser_theme"}}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["topic_guard"]["source_theme"], "daily_talk")


if __name__ == "__main__":
    unittest.main()
