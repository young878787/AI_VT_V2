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


if __name__ == "__main__":
    unittest.main()
