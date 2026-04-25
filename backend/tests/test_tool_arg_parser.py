import pathlib
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.tool_arg_parser import parse_tool_call_arguments


class ToolArgParserTests(unittest.TestCase):
    def test_parse_tool_call_arguments_recovers_leading_zero_decimal_numbers(self):
        raw_args = (
            '{"head_intensity": 0.75, "blush_level": 0.45, "eye_sync": false, '
            '"eye_l_open": 0.65, "eye_r_open": 0.78, "duration_sec": 4.5, '
            '"mouth_form": 0.35, "brow_l_y": 0.25, "brow_r_y": 0.4, '
            '"brow_l_angle": -0.15, "brow_r_angle": 0.2, "brow_l_form": 0.1, '
            '"brow_r_form": 00.1, "brow_l_x": 0.3, "brow_r_x": 00.2, '
            '"eye_l_smile": 0.6, "eye_r_smile": 0.85, "speaking_rate": 1.15}'
        )

        parsed, was_normalized = parse_tool_call_arguments(raw_args)

        self.assertTrue(was_normalized)
        self.assertEqual(parsed["brow_r_form"], 0.1)
        self.assertEqual(parsed["brow_r_x"], 0.2)
        self.assertEqual(parsed["eye_r_smile"], 0.85)

    def test_parse_tool_call_arguments_recovers_python_bool_and_trailing_comma(self):
        raw_args = '{"eye_sync": False, "duration_sec": 2.5,}'

        parsed, was_normalized = parse_tool_call_arguments(raw_args)

        self.assertTrue(was_normalized)
        self.assertFalse(parsed["eye_sync"])
        self.assertEqual(parsed["duration_sec"], 2.5)

    def test_parse_tool_call_arguments_salvages_partial_live2d_fields(self):
        raw_args = (
            '{"head_intensity": 0.65, "blush_level": 0.45, "eye_sync": fal: 4.5, '
            '"mouth_form": 0.25, "brow_l_y": 00.15, "eye_l_smile": 0.35}'
        )

        parsed, was_normalized = parse_tool_call_arguments(
            raw_args,
            tool_name="set_ai_behavior",
            model_name="Hiyori",
        )

        self.assertTrue(was_normalized)
        self.assertEqual(parsed["head_intensity"], 0.65)
        self.assertEqual(parsed["blush_level"], 0.45)
        self.assertEqual(parsed["mouth_form"], 0.25)
        self.assertEqual(parsed["brow_l_y"], 0.15)
        self.assertEqual(parsed["eye_l_smile"], 0.35)
        self.assertNotIn("eye_sync", parsed)

    def test_parse_tool_call_arguments_keeps_tool_name_signature_stable(self):
        raw_args = '{"duration_sec": 2.0}'

        parsed, was_normalized = parse_tool_call_arguments(
            raw_args,
            tool_name="blink_control",
            model_name="Hiyori",
        )

        self.assertFalse(was_normalized)
        self.assertEqual(parsed["duration_sec"], 2.0)


if __name__ == "__main__":
    unittest.main()
