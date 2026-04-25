import pathlib
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services.expression_compiler import (
    build_blink_plan,
    build_expression_sequence,
    compile_expression_plan,
)
from services.expression_legacy_renderer import render_legacy_behavior_payload


class ExpressionCompilerTests(unittest.TestCase):
    def test_compile_expression_plan_selects_playful_base_pose(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "playful",
                "intensity": 0.7,
                "energy": 0.65,
                "playfulness": 0.8,
                "asymmetry_bias": "strong",
                "arc": "steady",
                "hold_ms": 1800,
                "blink_style": "teasing_pause",
                "speaking_rate": 1.08,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["type"], "expression_plan")
        self.assertEqual(plan["basePose"]["preset"], "playful_smirk")
        self.assertFalse(plan["basePose"]["params"]["eyeSync"])
        self.assertEqual(plan["blinkPlan"]["style"], "teasing_pause")

    def test_compile_expression_plan_adds_micro_event_for_pop_then_settle(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "surprised",
                "intensity": 0.75,
                "energy": 0.82,
                "arc": "pop_then_settle",
                "hold_ms": 1500,
                "blink_style": "surprised_hold",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreaterEqual(len(plan["microEvents"]), 1)
        self.assertEqual(plan["microEvents"][0]["kind"], "surprised_pop")

    def test_render_legacy_behavior_payload_returns_behavior_and_blink_payloads(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "shy",
                "intensity": 0.55,
                "energy": 0.4,
                "arc": "steady",
                "hold_ms": 2000,
                "blink_style": "shy_fast",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        legacy = render_legacy_behavior_payload(plan)

        self.assertEqual(legacy["behavior_payload"]["type"], "behavior")
        self.assertIn("speaking_rate", legacy)
        self.assertEqual(
            legacy["blink_payloads"],
            [
                {
                    "type": "blink_control",
                    "action": "set_interval",
                    "intervalMin": 0.8,
                    "intervalMax": 1.5,
                }
            ],
        )

    def test_compile_expression_plan_uses_arc_fallback_when_must_include_is_all_unknown(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "surprised",
                "arc": "pop_then_settle",
                "must_include": ["unknown_event"],
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["microEvents"], [
            {
                "kind": "surprised_pop",
                "durationMs": 320,
                "patch": {"eyeLOpen": 1.14, "eyeROpen": 1.14, "browLY": 0.28, "browRY": 0.28},
                "returnToBase": True,
            }
        ])

    def test_compile_expression_plan_coerces_invalid_hold_ms_and_speaking_rate_to_safe_defaults(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "calm",
                "hold_ms": "not-a-number",
                "speaking_rate": object(),
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["basePose"]["durationSec"], 1.6)
        self.assertEqual(plan["speakingRate"], 1.0)

    def test_compile_expression_plan_replaces_unknown_blink_style_with_default(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "playful",
                "blink_style": "laser",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["blinkPlan"]["style"], "normal")
        self.assertEqual(plan["blinkPlan"]["commands"], [])

    def test_build_expression_sequence_returns_empty_extension_point_by_default(self):
        sequence = build_expression_sequence(
            {"primary_emotion": "calm", "arc": "steady"},
            {"preset": "calm_soft"},
            model_name="Hiyori",
        )

        self.assertEqual(sequence, [])

    def test_build_blink_plan_returns_default_style_for_unknown_value(self):
        blink_plan = build_blink_plan(
            {"blink_style": "laser"},
            model_name="Hiyori",
        )

        self.assertEqual(blink_plan, {"style": "normal", "commands": []})

    def test_render_legacy_behavior_payload_keeps_existing_field_names(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "calm",
                "intensity": 0.2,
                "energy": 0.25,
                "arc": "steady",
                "hold_ms": 1600,
                "blink_style": "normal",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        behavior = render_legacy_behavior_payload(plan)["behavior_payload"]

        self.assertIn("headIntensity", behavior)
        self.assertIn("eyeLOpen", behavior)
        self.assertIn("mouthForm", behavior)
        self.assertIn("browLY", behavior)

    def test_compile_expression_plan_amplifies_playful_high_energy_expression(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "playful",
                "intensity": 0.85,
                "energy": 0.9,
                "playfulness": 0.95,
                "warmth": 0.7,
                "dominance": 0.45,
                "asymmetry_bias": "strong",
                "arc": "pause_then_smirk",
                "hold_ms": 1400,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        params = plan["basePose"]["params"]
        self.assertGreater(params["mouthForm"], 0.3)
        self.assertGreater(params["eyeLSmile"], 0.5)
        self.assertLess(params["eyeROpen"], params["eyeLOpen"])
        self.assertGreaterEqual(len(plan["sequence"]), 1)

    def test_compile_expression_plan_turns_smile_arc_into_sequence(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "playful",
                "intensity": 0.7,
                "energy": 0.75,
                "playfulness": 0.9,
                "warmth": 0.8,
                "arc": "pause_then_smirk",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["sequence"][0]["kind"], "smirk_left")


if __name__ == "__main__":
    unittest.main()
