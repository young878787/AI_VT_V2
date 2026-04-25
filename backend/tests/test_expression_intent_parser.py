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
            '{"primary_emotion":"playful","intensity":0.72,"energy":0.68,'
            '"arc":"pop_then_settle","hold_ms":1800,"blink_style":"teasing_pause"}',
            emotion_state={"primary_emotion": "calm", "intensity": 0.2},
            previous_state=None,
        )

        self.assertEqual(intent["primary_emotion"], "playful")
        self.assertEqual(intent["arc"], "pop_then_settle")
        self.assertEqual(intent["blink_style"], "teasing_pause")
        self.assertAlmostEqual(intent["intensity"], 0.72)

    def test_parse_expression_intent_fills_defaults_from_emotion_state(self):
        intent = parse_expression_intent(
            '{"primary_emotion":"shy"}',
            emotion_state={"intensity": 0.61, "energy": 0.33},
            previous_state={"summary": "上一輪是輕微微笑"},
        )

        self.assertEqual(intent["primary_emotion"], "shy")
        self.assertAlmostEqual(intent["intensity"], 0.61)
        self.assertAlmostEqual(intent["energy"], 0.33)
        self.assertEqual(intent["arc"], "steady")
        self.assertEqual(intent["hold_ms"], 1600)

    def test_parse_expression_intent_replaces_invalid_enum_with_default(self):
        intent = parse_expression_intent(
            '{"primary_emotion":"playful","arc":"explode_forever","blink_style":"laser"}',
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

        self.assertEqual(intent["primary_emotion"], "playful")
        self.assertEqual(intent["arc"], "steady")
        self.assertEqual(intent["blink_style"], "normal")

    def test_parse_expression_intent_extracts_first_complete_json_object(self):
        intent = parse_expression_intent(
            '前文 {"primary_emotion":"playful","intensity":0.7} 後文 '
            '{"primary_emotion":"shy","intensity":0.2}',
            emotion_state={"primary_emotion": "calm", "intensity": 0.1, "energy": 0.2},
            previous_state=None,
        )

        self.assertEqual(intent["primary_emotion"], "playful")
        self.assertAlmostEqual(intent["intensity"], 0.7)

    def test_parse_expression_intent_validates_emotion_state_primary_emotion_fallback(self):
        intent = parse_expression_intent(
            '不是 JSON',
            emotion_state={"primary_emotion": "laser", "intensity": 0.4, "energy": 0.5},
            previous_state=None,
        )

        self.assertEqual(intent["primary_emotion"], "calm")

    def test_parse_expression_intent_normalizes_remaining_schema_fields(self):
        intent = parse_expression_intent(
            '{"primary_emotion":"playful","secondary_emotion":"laser","dominance":2,'
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

        first["must_include"].append("surprised_pop")
        first["avoid"].append("freeze")

        self.assertEqual(second["must_include"], [])
        self.assertEqual(second["avoid"], [])

    def test_parse_expression_intent_maps_common_emotion_and_arc_aliases(self):
        intent = parse_expression_intent(
            '{"primary_emotion":"happy","secondary_emotion":"joyful","arc":"neutral_to_smile"}',
            emotion_state=None,
            previous_state=None,
        )

        self.assertEqual(intent["primary_emotion"], "playful")
        self.assertEqual(intent["secondary_emotion"], "playful")
        self.assertEqual(intent["arc"], "pause_then_smirk")


if __name__ == "__main__":
    unittest.main()
