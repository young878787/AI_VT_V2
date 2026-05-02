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


def estimate_native_body_ranges(plan: dict) -> dict:
    params = plan["basePose"]["params"]
    profile = plan["basePose"]["bodyMotionProfile"]
    impulse = params["physicsImpulse"]
    sway = max(impulse * 5.8, abs(params["bodyAngleX"]) * 27.0) * profile["swayScale"]
    bob = max(impulse * 1.45, abs(params["bodyAngleY"]) * 7.0) * profile["bobScale"]
    twist = max(impulse * 4.8, abs(params["bodyAngleZ"]) * 22.0) * profile["twistScale"]
    return {
        "x": (
            params["bodyAngleX"] * 5.2 - sway,
            params["bodyAngleX"] * 5.2 + sway,
        ),
        "y": (
            params["bodyAngleY"] * 10.0 - bob,
            params["bodyAngleY"] * 10.0 + bob,
        ),
        "z": (
            params["bodyAngleZ"] * 5.0 - twist,
            params["bodyAngleZ"] * 5.0 + twist,
        ),
    }


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
        self.assertEqual(plan["idlePlan"]["name"], "happy_idle")

    def test_compile_expression_plan_includes_body_motion_inputs_for_physics(self):
        plan = compile_expression_plan(
            {
                "emotion": "surprised",
                "performance_mode": "shock_recoil",
                "intensity": 0.8,
                "energy": 0.9,
                "hold_ms": 1500,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        params = plan["basePose"]["params"]
        for key in ("bodyAngleX", "bodyAngleY", "bodyAngleZ", "breathLevel", "physicsImpulse"):
            self.assertIn(key, params)
            self.assertIsInstance(params[key], float)

        self.assertGreater(params["breathLevel"], 0.5)
        self.assertGreater(params["physicsImpulse"], 0.5)
        self.assertIn("bodyAngleX", plan["carryState"])
        self.assertIn("physicsImpulse", plan["idlePlan"]["settlePose"]["params"])

    def test_compile_expression_plan_makes_playful_motion_more_lively_than_gloomy(self):
        playful = compile_expression_plan(
            {
                "emotion": "playful",
                "performance_mode": "goofy_face",
                "intensity": 0.7,
                "energy": 0.75,
                "playfulness": 0.85,
            },
            model_name="Hiyori",
            previous_state=None,
        )
        gloomy = compile_expression_plan(
            {
                "emotion": "gloomy",
                "performance_mode": "deadpan",
                "intensity": 0.5,
                "energy": 0.35,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        self.assertGreater(playful["basePose"]["params"]["physicsImpulse"], 0.55)
        self.assertGreater(
            playful["basePose"]["params"]["physicsImpulse"],
            gloomy["basePose"]["params"]["physicsImpulse"],
        )
        self.assertGreater(abs(playful["basePose"]["params"]["bodyAngleX"]), 0.08)

    def test_compile_expression_plan_adds_emotion_specific_body_motion_profile(self):
        happy = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "bright_talk",
                "intensity": 0.55,
                "energy": 0.75,
                "playfulness": 0.45,
            },
            model_name="Hiyori",
            previous_state=None,
        )
        sad = compile_expression_plan(
            {
                "emotion": "sad",
                "performance_mode": "tense_hold",
                "intensity": 0.75,
                "energy": 0.25,
                "playfulness": 0.05,
            },
            model_name="Hiyori",
            previous_state=None,
        )
        angry = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.8,
                "energy": 0.7,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        happy_profile = happy["basePose"]["bodyMotionProfile"]
        sad_profile = sad["basePose"]["bodyMotionProfile"]
        angry_profile = angry["basePose"]["bodyMotionProfile"]

        self.assertEqual(happy_profile["style"], "bright_bounce")
        self.assertEqual(sad_profile["style"], "small_sad_bob")
        self.assertEqual(angry_profile["style"], "locked_tense")
        self.assertGreater(happy_profile["speed"], sad_profile["speed"])
        self.assertGreater(happy_profile["swayScale"], sad_profile["swayScale"])
        self.assertGreater(sad_profile["bobScale"], sad_profile["swayScale"])
        self.assertGreater(sad["basePose"]["params"]["physicsImpulse"], 0.22)
        self.assertLess(sad["basePose"]["params"]["bodyAngleY"], -0.25)
        self.assertGreater(sad_profile["headScale"], 0.7)
        self.assertGreater(angry_profile["twistScale"], angry_profile["swayScale"])

    def test_low_energy_emotions_still_have_visible_native_body_motion(self):
        sad = compile_expression_plan(
            {
                "emotion": "sad",
                "performance_mode": "tense_hold",
                "intensity": 0.75,
                "energy": 0.25,
            },
            model_name="Hiyori",
            previous_state=None,
        )
        gloomy = compile_expression_plan(
            {
                "emotion": "gloomy",
                "performance_mode": "deadpan",
                "intensity": 0.55,
                "energy": 0.30,
            },
            model_name="Hiyori",
            previous_state=None,
        )
        shy = compile_expression_plan(
            {
                "emotion": "shy",
                "performance_mode": "awkward",
                "intensity": 0.55,
                "energy": 0.45,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        sad_ranges = estimate_native_body_ranges(sad)
        gloomy_ranges = estimate_native_body_ranges(gloomy)
        shy_ranges = estimate_native_body_ranges(shy)

        self.assertLess(sad_ranges["y"][0], -6.0)
        self.assertLess(gloomy_ranges["y"][0], -3.0)
        self.assertGreater(shy_ranges["x"][1] - shy_ranges["x"][0], 2.5)
        self.assertGreater(shy_ranges["y"][1] - shy_ranges["y"][0], 1.5)

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
        self.assertIn("bodyAngleX", legacy["behavior_payload"])
        self.assertIn("breathLevel", legacy["behavior_payload"])
        self.assertIn("physicsImpulse", legacy["behavior_payload"])
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

    def test_build_blink_plan_keeps_set_interval_commands_complete(self):
        blink_plan = build_blink_plan(
            {"blink_style": "sleepy_slow"},
            model_name="Hiyori",
        )

        self.assertEqual(blink_plan["style"], "sleepy_slow")
        self.assertEqual(len(blink_plan["commands"]), 1)

        command = blink_plan["commands"][0]
        self.assertEqual(command["action"], "set_interval")
        self.assertIn("intervalMin", command)
        self.assertIn("intervalMax", command)
        self.assertLessEqual(command["intervalMin"], command["intervalMax"])

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
        self.assertEqual(sequence[0]["kind"], "bright_sway_left")
        self.assertEqual(sequence[1]["kind"], "bright_sway_right")
        self.assertGreaterEqual(sequence[0]["durationMs"], 900)
        self.assertEqual(sequence[0]["fadeInMs"], 180)
        self.assertEqual(sequence[0]["fadeOutMs"], 260)
        self.assertEqual(sequence[1]["fadeInMs"], 180)
        self.assertEqual(sequence[1]["fadeOutMs"], 260)
        self.assertNotIn("bodyAngleX", sequence[0]["patch"])
        self.assertNotIn("bodyAngleZ", sequence[0]["patch"])
        self.assertNotIn("bodyAngleX", sequence[1]["patch"])
        self.assertNotIn("bodyAngleZ", sequence[1]["patch"])
        self.assertGreater(sequence[0]["patch"]["physicsImpulse"], 0.85)

    def test_bright_talk_idle_timing_includes_sequential_sway_duration(self):
        plan = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "bright_talk",
                "intensity": 0.6,
                "energy": 0.6,
                "hold_ms": 1200,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        sequence_duration = sum(step["durationMs"] for step in plan["sequence"])
        source = plan["idlePlan"]["source"]
        self.assertGreaterEqual(sequence_duration, 1000)
        self.assertEqual(
            source["actionEnterAfterMs"],
            int(plan["basePose"]["durationSec"] * 1000)
            + sequence_duration
            + max(event["durationMs"] for event in plan["microEvents"]),
        )

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

    def test_compile_expression_plan_uses_previous_state_for_continuity(self):
        previous_state = {
            "emotion": "happy",
            "performanceMode": "smile",
            "signature": "happy_soft",
            "residue": 0.42,
            "eyeSync": True,
            "headIntensity": 0.34,
            "blushLevel": 0.22,
            "eyeLOpen": 0.93,
            "eyeROpen": 0.92,
            "mouthForm": 0.55,
            "browLY": 0.08,
            "browRY": 0.07,
            "browLAngle": 0.12,
            "browRAngle": 0.11,
            "browLForm": 0.16,
            "browRForm": 0.15,
            "eyeLSmile": 0.52,
            "eyeRSmile": 0.49,
            "browLX": 0.03,
            "browRX": -0.02,
        }
        intent = {
            "emotion": "happy",
            "performance_mode": "smile",
            "intensity": 0.25,
            "energy": 0.25,
            "warmth": 0.35,
            "playfulness": 0.1,
            "arc": "steady",
            "hold_ms": 1600,
        }

        without_previous = compile_expression_plan(
            intent,
            model_name="Hiyori",
            previous_state=None,
        )
        with_previous = compile_expression_plan(
            intent,
            model_name="Hiyori",
            previous_state=previous_state,
        )

        self.assertGreater(
            with_previous["basePose"]["params"]["mouthForm"],
            without_previous["basePose"]["params"]["mouthForm"],
        )
        self.assertGreater(
            with_previous["basePose"]["params"]["eyeLSmile"],
            without_previous["basePose"]["params"]["eyeLSmile"],
        )
        self.assertIn("carryState", with_previous)
        self.assertEqual(with_previous["carryState"]["emotion"], "happy")
        self.assertEqual(with_previous["carryState"]["performanceMode"], "smile")
        self.assertGreater(with_previous["carryState"]["residue"], 0.0)

    def test_previous_playful_state_does_not_override_high_intensity_angry_switch(self):
        previous_state = {
            "emotion": "playful",
            "performanceMode": "goofy_face",
            "signature": "goofy_asym",
            "residue": 0.45,
            "eyeSync": False,
            "eyeLOpen": 0.83,
            "eyeROpen": 1.08,
            "mouthForm": 0.46,
            "browLY": 0.14,
            "browRY": -0.08,
            "browLAngle": 0.18,
            "browRAngle": -0.10,
            "browLForm": 0.12,
            "browRForm": -0.08,
            "eyeLSmile": 0.31,
            "eyeRSmile": 0.08,
            "browLX": -0.04,
            "browRX": 0.05,
        }

        plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.92,
                "energy": 0.82,
                "arc": "steady",
            },
            model_name="Hiyori",
            previous_state=previous_state,
        )

        self.assertEqual(plan["debug"]["intentEmotion"], "angry")
        self.assertTrue(plan["basePose"]["params"]["eyeSync"])
        self.assertEqual(plan["carryState"]["emotion"], "angry")
        self.assertLess(plan["basePose"]["params"]["blushLevel"], 0.0)

    def test_compile_expression_plan_adds_idle_plan_that_does_not_return_to_zero(self):
        plan = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "smile",
                "intensity": 0.45,
                "energy": 0.45,
                "hold_ms": 1200,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        idle_plan = plan["idlePlan"]
        settle = idle_plan["settlePose"]["params"]
        self.assertEqual(idle_plan["name"], "happy_idle")
        self.assertEqual(idle_plan["mode"], "loop")
        self.assertTrue(idle_plan["interruptible"])
        self.assertGreaterEqual(idle_plan["enterAfterMs"], 1200)
        self.assertGreaterEqual(len(idle_plan["loopEvents"]), 1)
        self.assertGreater(settle["mouthForm"], 0.0)
        self.assertGreater(settle["eyeLSmile"], 0.0)

    def test_idle_plan_waits_for_estimated_spoken_line_before_settling(self):
        plan = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "bright_talk",
                "intensity": 0.6,
                "energy": 0.7,
                "hold_ms": 1200,
                "spoken_text": "哇～這句話其實還滿長的喔！如果待機太早進來，看起來就會像表情被切掉一樣。",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        source = plan["idlePlan"]["source"]
        self.assertGreater(source["speakingEnterAfterMs"], source["actionEnterAfterMs"])
        self.assertGreaterEqual(source["postSpeechHoldMs"], 6000)
        self.assertLessEqual(source["postSpeechHoldMs"], 10000)
        self.assertEqual(
            plan["idlePlan"]["enterAfterMs"],
            source["speakingEnterAfterMs"] + source["postSpeechHoldMs"],
        )

    def test_idle_plan_keeps_short_spoken_expression_visible_after_speaking(self):
        plan = compile_expression_plan(
            {
                "emotion": "happy",
                "performance_mode": "smile",
                "intensity": 0.45,
                "energy": 0.45,
                "hold_ms": 1200,
                "spoken_text": "好喔。",
            },
            model_name="Hiyori",
            previous_state=None,
        )

        source = plan["idlePlan"]["source"]
        base_enter_after_ms = max(
            400,
            source["actionEnterAfterMs"],
            source["speakingEnterAfterMs"],
        )
        self.assertGreaterEqual(source["postSpeechHoldMs"], 6000)
        self.assertLessEqual(source["postSpeechHoldMs"], 10000)
        self.assertEqual(
            plan["idlePlan"]["enterAfterMs"],
            base_enter_after_ms + source["postSpeechHoldMs"],
        )

    def test_compile_expression_plan_maps_requested_idle_families(self):
        cases = [
            (
                {
                    "emotion": "sad",
                    "performance_mode": "awkward",
                    "topic_guard": {
                        "must_preserve_theme": True,
                        "source_theme": "crying",
                        "allow_style_override": False,
                    },
                },
                "crying_idle",
            ),
            ({"emotion": "angry", "performance_mode": "meltdown"}, "angry_glare_idle"),
            ({"emotion": "shy", "performance_mode": "awkward"}, "shy_idle"),
            ({"emotion": "gloomy", "performance_mode": "deadpan"}, "gloomy_idle"),
        ]

        for intent, expected_idle_name in cases:
            with self.subTest(expected_idle_name=expected_idle_name):
                plan = compile_expression_plan(
                    intent,
                    model_name="Hiyori",
                    previous_state=None,
                )

                self.assertEqual(plan["idlePlan"]["name"], expected_idle_name)
                self.assertEqual(plan["debug"]["idlePlan"], expected_idle_name)
                self.assertGreaterEqual(len(plan["idlePlan"]["loopEvents"]), 1)

    def test_angry_idle_settle_keeps_hiyori_negative_blush_and_glare(self):
        plan = compile_expression_plan(
            {
                "emotion": "angry",
                "performance_mode": "meltdown",
                "intensity": 0.9,
                "energy": 0.8,
            },
            model_name="Hiyori",
            previous_state=None,
        )

        settle = plan["idlePlan"]["settlePose"]["params"]
        self.assertEqual(plan["idlePlan"]["name"], "angry_glare_idle")
        self.assertLess(settle["blushLevel"], 0.0)
        self.assertTrue(settle["eyeSync"])
        self.assertGreater(settle["eyeLOpen"], 1.0)
        self.assertGreater(settle["browLAngle"], 0.35)

    def test_idle_plan_families_have_distinct_contextual_motion(self):
        cases = {
            "happy_idle": {"emotion": "happy", "performance_mode": "smile"},
            "crying_idle": {
                "emotion": "sad",
                "performance_mode": "awkward",
                "topic_guard": {
                    "must_preserve_theme": True,
                    "source_theme": "crying",
                    "allow_style_override": False,
                },
            },
            "angry_glare_idle": {"emotion": "angry", "performance_mode": "meltdown"},
            "shy_idle": {"emotion": "shy", "performance_mode": "awkward"},
            "gloomy_idle": {"emotion": "gloomy", "performance_mode": "deadpan"},
        }

        plans = {
            idle_name: compile_expression_plan(
                intent,
                model_name="Hiyori",
                previous_state=None,
            )["idlePlan"]
            for idle_name, intent in cases.items()
        }

        event_kinds = {
            idle_name: plan["loopEvents"][0]["kind"]
            for idle_name, plan in plans.items()
        }
        self.assertEqual(len(set(event_kinds.values())), len(event_kinds))
        self.assertEqual(
            len({plan["loopIntervalMs"] for plan in plans.values()}),
            len(plans),
        )
        ambient_state_kinds = {
            tuple(state["kind"] for state in plan["ambientPlan"]["states"])
            for plan in plans.values()
        }
        self.assertEqual(
            ambient_state_kinds,
            {
                (
                    "ambient_idle_breath",
                    "ambient_idle_look_around",
                    "ambient_idle_active_shift",
                )
            },
        )
        for plan in plans.values():
            self.assertGreaterEqual(plan["ambientEnterAfterMs"], plan["enterAfterMs"] + 9000)
            self.assertLessEqual(plan["ambientEnterAfterMs"], plan["enterAfterMs"] + 14000)
            self.assertGreaterEqual(plan["ambientSwitchIntervalMs"], 5200)
            self.assertLessEqual(plan["ambientSwitchIntervalMs"], 9200)
            self.assertEqual(len(plan["ambientPlan"]["states"]), 3)

        happy = plans["happy_idle"]["settlePose"]["params"]
        self.assertGreater(happy["mouthForm"], 0.2)
        self.assertGreater(happy["eyeLSmile"], 0.35)
        self.assertGreater(happy["blushLevel"], 0.0)

        crying = plans["crying_idle"]["settlePose"]["params"]
        self.assertLess(crying["mouthForm"], -0.25)
        self.assertLess(crying["eyeLOpen"], 0.65)
        self.assertLess(crying["blushLevel"], -0.45)
        self.assertLess(crying["browLAngle"], 0.0)

        angry = plans["angry_glare_idle"]["settlePose"]["params"]
        self.assertGreater(angry["eyeLOpen"], 1.1)
        self.assertGreater(angry["browLAngle"], 0.55)
        self.assertLess(angry["browRAngle"], -0.55)
        self.assertLess(angry["blushLevel"], -0.5)

        shy = plans["shy_idle"]["settlePose"]["params"]
        self.assertFalse(shy["eyeSync"])
        self.assertGreater(shy["eyeROpen"] - shy["eyeLOpen"], 0.15)
        self.assertGreater(shy["blushLevel"], 0.3)

        gloomy = plans["gloomy_idle"]["settlePose"]["params"]
        self.assertLess(gloomy["eyeLOpen"], 0.6)
        self.assertLess(gloomy["mouthForm"], -0.1)
        self.assertLess(gloomy["browLY"], -0.2)

        ambient_states = plans["happy_idle"]["ambientPlan"]["states"]
        self.assertNotEqual(ambient_states[0]["params"]["eyeLOpen"], ambient_states[1]["params"]["eyeLOpen"])
        self.assertNotEqual(ambient_states[1]["params"]["mouthForm"], ambient_states[2]["params"]["mouthForm"])
        self.assertTrue(ambient_states[0]["params"]["eyeSync"])
        self.assertFalse(ambient_states[1]["params"]["eyeSync"])
        self.assertTrue(ambient_states[2]["params"]["eyeSync"])


if __name__ == "__main__":
    unittest.main()
