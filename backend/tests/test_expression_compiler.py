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
    resolve_effective_performance_mode,
    resolve_topic_guard,
    select_base_pose,
)
from services.expression_legacy_renderer import render_legacy_behavior_payload


class ExpressionCompilerTests(unittest.TestCase):
    def test_compile_expression_plan_selects_playful_base_pose(self):
        plan = compile_expression_plan(
            {
                "emotion": "playful",
                "performance_mode": "goofy_face",
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
        self.assertEqual(plan["basePose"]["preset"], "playful_goofy_face")
        self.assertFalse(plan["basePose"]["params"]["eyeSync"])
        self.assertEqual(plan["blinkPlan"]["style"], "teasing_pause")

    def test_compile_expression_plan_selects_happy_smile_preset(self):
        plan = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "smile",
                "intensity": 0.5,
                "energy": 0.5,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["basePose"]["preset"], "happy_smile_soft")

    def test_compile_expression_plan_selects_different_presets_for_happy_vs_playful_goofy(self):
        happy_plan = compile_expression_plan(
            {"emotion": "happy", "performance_mode": "smile"},
            model_name="Hiyori",
            previous_state=None,
        )
        goofy_plan = compile_expression_plan(
            {"emotion": "playful", "performance_mode": "goofy_face"},
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(happy_plan["basePose"]["preset"], "happy_smile_soft")
        self.assertEqual(goofy_plan["basePose"]["preset"], "playful_goofy_face")
        self.assertNotEqual(happy_plan["basePose"]["preset"], goofy_plan["basePose"]["preset"])

    def test_compile_expression_plan_adds_micro_event_for_goofy_face(self):
        plan = compile_expression_plan(
            {
                "emotion": "playful",
                "performance_mode": "goofy_face",
                "intensity": 0.75,
                "energy": 0.82,
                "arc": "steady",
                "hold_ms": 1500,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreaterEqual(len(plan["microEvents"]), 1)

    def test_topic_guard_downgrades_goofy_face_on_crying_theme(self):
        result = resolve_effective_performance_mode(
            "sad",
            "goofy_face",
            {"must_preserve_theme": True, "source_theme": "crying", "allow_style_override": False},
        )

        self.assertEqual(result, "awkward")

    def test_topic_guard_downgrades_bright_talk_on_gloomy_theme(self):
        result = resolve_effective_performance_mode(
            "gloomy",
            "bright_talk",
            {"must_preserve_theme": True, "source_theme": "gloomy", "allow_style_override": False},
        )

        self.assertEqual(result, "deadpan")

    def test_topic_guard_allows_performance_on_daily_talk(self):
        result = resolve_effective_performance_mode(
            "happy",
            "bright_talk",
            {"must_preserve_theme": True, "source_theme": "daily_talk", "allow_style_override": False},
        )

        self.assertEqual(result, "bright_talk")

    def test_topic_guard_bypasses_when_must_preserve_theme_false(self):
        result = resolve_effective_performance_mode(
            "sad",
            "goofy_face",
            {"must_preserve_theme": False, "source_theme": "crying", "allow_style_override": True},
        )

        self.assertEqual(result, "goofy_face")

    def test_compile_expression_plan_applies_topic_guard_in_pipeline(self):
        plan = compile_expression_plan(
            {
                "emotion": "sad",
                "performance_mode": "goofy_face",
                "topic_guard": {
                    "must_preserve_theme": True,
                    "source_theme": "crying",
                    "allow_style_override": False,
                },
                "hold_ms": 1800,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertTrue(plan["debug"]["modeDowngraded"])
        self.assertEqual(plan["debug"]["intentPerformanceMode"], "awkward")
        self.assertEqual(plan["debug"]["originalPerformanceMode"], "goofy_face")

    def test_compile_expression_plan_conflicted_volatile_produces_asymmetric_event(self):
        plan = compile_expression_plan(
            {
                "emotion": "conflicted",
                "performance_mode": "volatile",
                "intensity": 0.8,
                "energy": 0.75,
                "arc": "steady",
                "hold_ms": 1400,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["basePose"]["preset"], "conflicted_volatile")
        self.assertGreaterEqual(len(plan["microEvents"]), 1)
        self.assertFalse(plan["basePose"]["params"]["eyeSync"])

    def test_compile_expression_plan_angry_meltdown_differs_from_angry_deadpan(self):
        meltdown = compile_expression_plan(
            {"emotion": "angry", "performance_mode": "meltdown", "intensity": 0.9, "energy": 0.85},
            model_name="Hiyori",
            previous_state=None,
        )
        angry_deadpan = compile_expression_plan(
            {"emotion": "angry", "performance_mode": "deadpan", "intensity": 0.5, "energy": 0.4},
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(meltdown["basePose"]["preset"], "angry_meltdown")
        self.assertEqual(angry_deadpan["basePose"]["preset"], "angry_meltdown")
        self.assertGreater(
            meltdown["basePose"]["params"]["headIntensity"],
            angry_deadpan["basePose"]["params"]["headIntensity"],
        )
        self.assertLess(
            meltdown["basePose"]["params"]["mouthForm"],
            angry_deadpan["basePose"]["params"]["mouthForm"],
        )

    def test_angry_high_intensity_prefers_sync_stare_over_asymmetric_squint(self):
        plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.92,
                "energy": 0.82,
                "arc": "steady",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        params = plan["basePose"]["params"]
        self.assertTrue(params["eyeSync"])
        self.assertGreater(params["eyeLOpen"], 0.95)
        self.assertGreater(params["eyeROpen"], 0.95)
        self.assertLess(abs(params["eyeLOpen"] - params["eyeROpen"]), 0.08)

    def test_angry_mid_intensity_keeps_asymmetric_contempt_shape(self):
        plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.62,
                "energy": 0.70,
                "arc": "steady",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        params = plan["basePose"]["params"]
        self.assertFalse(params["eyeSync"])
        self.assertGreater(params["eyeROpen"], params["eyeLOpen"])
        self.assertGreater(abs(params["eyeLOpen"] - params["eyeROpen"]), 0.08)

    def test_render_legacy_behavior_payload_returns_behavior_and_blink_payloads(self):
        plan = compile_expression_plan(
            {
                "emotion": "shy",
                "performance_mode": "awkward",
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

    def test_compile_expression_plan_coerces_invalid_hold_ms_and_speaking_rate_to_safe_defaults(self):
        plan = compile_expression_plan(
            {
                "emotion": "neutral",
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
                "emotion": "playful",
                "performance_mode": "smile",
                "blink_style": "laser",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["blinkPlan"]["style"], "normal")
        self.assertEqual(plan["blinkPlan"]["commands"], [])

    def test_build_expression_sequence_returns_empty_extension_point_by_default(self):
        sequence = build_expression_sequence(
            "neutral", "smile", 0.3, 0.3,
            {"arc": "steady"},
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
                "emotion": "neutral",
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
                "emotion": "playful",
                "performance_mode": "goofy_face",
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
                "emotion": "playful",
                "performance_mode": "goofy_face",
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

    def test_compile_expression_plan_debug_trace_shows_guard_info(self):
        plan = compile_expression_plan(
            {
                "emotion": "gloomy",
                "performance_mode": "goofy_face",
                "topic_guard": {
                    "must_preserve_theme": True,
                    "source_theme": "gloomy",
                },
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertIn("debug", plan)
        self.assertTrue(plan["debug"]["modeDowngraded"])
        self.assertEqual(plan["debug"]["sourceTheme"], "gloomy")
        self.assertTrue(plan["debug"]["guardActive"])

    def test_build_expression_sequence_returns_bright_talk_bounce(self):
        sequence = build_expression_sequence(
            "happy", "bright_talk", 0.6, 0.5,
            {"arc": "steady"},
        )

        self.assertGreaterEqual(len(sequence), 2)

    def test_select_base_pose_maps_emotion_performance_combos(self):
        self.assertEqual(select_base_pose("happy", "smile"), "happy_smile_soft")
        self.assertEqual(select_base_pose("happy", "bright_talk"), "happy_bright_talk")
        self.assertEqual(select_base_pose("playful", "goofy_face"), "playful_goofy_face")
        self.assertEqual(select_base_pose("teasing", "cheeky_wink"), "teasing_cheeky_wink")
        self.assertEqual(select_base_pose("teasing", "smug"), "teasing_smug")
        self.assertEqual(select_base_pose("gloomy", "deadpan"), "gloomy_deadpan")
        self.assertEqual(select_base_pose("sad", "tense_hold"), "sad_tense_hold")
        self.assertEqual(select_base_pose("angry", "meltdown"), "angry_meltdown")
        self.assertEqual(select_base_pose("conflicted", "volatile"), "conflicted_volatile")
        self.assertEqual(select_base_pose("shy", "awkward"), "awkward_stuck")
        self.assertEqual(select_base_pose("surprised", "shock_recoil"), "surprised_open")

    def test_resolve_topic_guard_downgrades_cheeky_wink_on_crying(self):
        result = resolve_topic_guard(
            "sad", "cheeky_wink",
            {"must_preserve_theme": True, "source_theme": "crying"},
        )
        self.assertEqual(result, "awkward")

    def test_resolve_topic_guard_downgrades_smile_on_gloomy(self):
        result = resolve_topic_guard(
            "gloomy", "smile",
            {"must_preserve_theme": True, "source_theme": "gloomy"},
        )
        self.assertEqual(result, "deadpan")

    def test_compile_expression_plan_backward_compat_primary_emotion(self):
        plan = compile_expression_plan(
            {
                "primary_emotion": "playful",
                "intensity": 0.5,
                "energy": 0.5,
                "arc": "steady",
                "hold_ms": 1800,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["type"], "expression_plan")

    def test_compile_expression_plan_fallback_to_neutral_on_unknown_emotion(self):
        plan = compile_expression_plan(
            {
                "emotion": "laser",
                "performance_mode": "smile",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["basePose"]["preset"], "calm_soft")

    def test_angry_deadpan_still_uses_angry_preset(self):
        plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "deadpan",
                "intensity": 0.6,
                "energy": 0.5,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["basePose"]["preset"], "angry_meltdown")

    def test_blush_level_increases_for_shy_emotion(self):
        plan = compile_expression_plan(
            {
                "emotion": "shy",
                "performance_mode": "shy",
                "intensity": 0.5,
                "energy": 0.5,
                "warmth": 0.8,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreater(plan["basePose"]["params"]["blushLevel"], 0.15)
        self.assertLess(plan["basePose"]["params"]["blushLevel"], 0.35)

    def test_blush_level_decreases_for_angry_cold(self):
        plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.7,
                "energy": 0.7,
                "warmth": 0.2,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertLessEqual(plan["basePose"]["params"]["blushLevel"], -0.5)
        self.assertGreaterEqual(plan["basePose"]["params"]["blushLevel"], -1.0)

    def test_happy_blush_is_subtle_not_saturated(self):
        plan = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "smile",
                "intensity": 0.8,
                "energy": 0.7,
                "warmth": 0.8,
                "playfulness": 0.4,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreater(plan["basePose"]["params"]["blushLevel"], 0.0)
        self.assertLess(plan["basePose"]["params"]["blushLevel"], 0.12)

    def test_sad_blush_stays_in_hiyori_negative_band(self):
        plan = compile_expression_plan(
            {
                "emotion": "sad",
                "performance_mode": "tense_hold",
                "intensity": 0.65,
                "energy": 0.45,
                "warmth": 0.2,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertLessEqual(plan["basePose"]["params"]["blushLevel"], -0.2)
        self.assertGreaterEqual(plan["basePose"]["params"]["blushLevel"], -1.0)

    def test_mouthform_goes_negative_for_sad_high_intensity(self):
        plan = compile_expression_plan(
            {
                "emotion": "sad",
                "performance_mode": "tense_hold",
                "intensity": 0.8,
                "energy": 0.6,
                "warmth": 0.3,
                "playfulness": 0.2,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertLess(plan["basePose"]["params"]["mouthForm"], -0.2)

    def test_mouthform_sad_is_lower_than_happy_same_intensity(self):
        happy_plan = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "smile",
                "intensity": 0.7,
                "energy": 0.5,
                "warmth": 0.5,
                "playfulness": 0.3,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )
        sad_plan = compile_expression_plan(
            {
                "emotion": "sad",
                "performance_mode": "tense_hold",
                "intensity": 0.7,
                "energy": 0.5,
                "warmth": 0.5,
                "playfulness": 0.3,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertLess(
            sad_plan["basePose"]["params"]["mouthForm"],
            happy_plan["basePose"]["params"]["mouthForm"],
        )

    def test_brow_form_negative_for_angry(self):
        plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.7,
                "energy": 0.6,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertLess(plan["basePose"]["params"]["browLForm"], 0)

    def test_brow_form_positive_for_surprised(self):
        plan = compile_expression_plan(
            {
                "emotion": "surprised",
                "performance_mode": "shock_recoil",
                "intensity": 0.5,
                "energy": 0.8,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreater(plan["basePose"]["params"]["browLForm"], 0)

    def test_brow_x_inward_for_angry(self):
        plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.7,
                "energy": 0.6,
                "dominance": 0.5,
                "playfulness": 0.2,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreater(plan["basePose"]["params"]["browLX"], 0)

    def test_eyesync_true_preserves_r_side_brow_params(self):
        plan = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "smile",
                "intensity": 0.7,
                "energy": 0.7,
                "playfulness": 0.5,
                "dominance": 0.5,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        params = plan["basePose"]["params"]
        self.assertEqual(params["browRForm"], 0.02)
        self.assertEqual(params["browRX"], 0.0)

    def test_blush_level_not_modified_by_default_neutral(self):
        plan = compile_expression_plan(
            {
                "emotion": "neutral",
                "performance_mode": "smile",
                "intensity": 0.5,
                "energy": 0.5,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertEqual(plan["basePose"]["params"]["blushLevel"], 0.0)

    def test_brow_y_raised_for_surprised(self):
        plan = compile_expression_plan(
            {
                "emotion": "surprised",
                "performance_mode": "shock_recoil",
                "intensity": 0.5,
                "energy": 0.8,
                "dominance": 0.5,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreater(plan["basePose"]["params"]["browLY"], 0.22)

    def test_brow_angle_sharper_for_angry(self):
        plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.8,
                "energy": 0.6,
                "dominance": 0.7,
                "playfulness": 0.2,
                "arc": "steady",
                "hold_ms": 1600,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreater(plan["basePose"]["params"]["browLAngle"], 0.42)


if __name__ == "__main__":
    unittest.main()
