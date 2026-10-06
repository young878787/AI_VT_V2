import asyncio
import math
import pathlib
import random
import sys
import unittest


BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routes.expression_debug_router import ExpressionPlanDebugRequest, compile_debug_expression_plan
from domain.expression_eye_motion_library import EYE_MOTION_PRESETS
from domain.input_event import normalize_chat_input
from domain.jev_questions import map_answers_to_intent
from domain.rushia_expression_profile import FAMILY_POSES, FAMILY_VARIANTS, canonical_rushia_family
from services.expression_compiler import compile_expression_plan


class RushiaExpressionProfileTests(unittest.TestCase):
    def compile(self, family="calm", *, previous=None, seed=12, variant=None, **extra):
        emotion = "happy" if family in {"soft_smile", "closed_smile"} else family
        if family in {"calm", "listening", "thinking"}:
            emotion = "neutral"
        return compile_expression_plan(
            {"emotion": emotion, "expression_family": family, **extra},
            "Rushia", previous, seed=seed,
            debug_overrides={"expressionVariant": variant} if variant else None,
        )

    def test_seed_replays_without_reading_or_mutating_global_random_state(self):
        before = random.getstate()
        first = self.compile("thinking")
        second = self.compile("thinking")
        self.assertEqual(first, second)
        self.assertEqual(before, random.getstate())

    def test_variants_do_not_repeat_across_turns_even_with_same_seed(self):
        for family in ("calm", "listening", "thinking", "playful", "teasing", "soft_smile", "closed_smile"):
            with self.subTest(family=family):
                first = self.compile(family)
                second = self.compile(family, previous=first["carryState"])
                self.assertNotEqual(first["debug"]["expressionVariant"], second["debug"]["expressionVariant"])
                variants = {self.compile(family, seed=seed)["debug"]["expressionVariant"] for seed in range(128)}
                self.assertEqual(variants, {item[0] for item in FAMILY_VARIANTS[canonical_rushia_family(family)]})

    def test_every_pose_and_event_obeys_rushia_native_ranges(self):
        for family in FAMILY_POSES:
            with self.subTest(family=family):
                plan = self.compile(family, intensity=1.0, spoken_text="這是一段較長的回覆。" * 10)
                poses = [plan["basePose"]["params"], plan["idlePlan"]["settlePose"]["params"]]
                events = plan["microEvents"] + plan["sequence"] + plan["idlePlan"]["loopEvents"]
                poses.extend(event["patch"] for event in events)
                for pose in poses:
                    for key in ("eyeLOpen", "eyeROpen", "mouthOpenBias"):
                        if key in pose:
                            self.assertGreaterEqual(pose[key], 0)
                            self.assertLessEqual(pose[key], 1)
                    self.assertGreaterEqual(pose.get("mouthForm", 0), -1)
                    self.assertLessEqual(pose.get("mouthForm", 0), 1)
                    self.assertEqual(pose.get("eyeLSmile", 0), 0)
                    self.assertEqual(pose.get("eyeRSmile", 0), 0)
                for event in events:
                    self.assertLessEqual(event["fadeInMs"] + event["fadeOutMs"], event["durationMs"])
                self.assertEqual(plan["idlePlan"]["settlePose"]["params"]["mouthOpenBias"], 0)

    def test_neutral_without_text_has_quiet_gesture_and_short_settle(self):
        plan = compile_expression_plan({"emotion": "neutral", "performance_mode": "smile"}, "Rushia", None)
        self.assertEqual(plan["debug"]["expressionFamily"], "calm")
        self.assertTrue(any(event["patch"] for event in plan["sequence"]))
        self.assertEqual(plan["motionPlan"]["body"]["spring"], 0)
        self.assertNotIn("happy", plan["motionPlan"]["theme"])
        self.assertLess(plan["idlePlan"]["enterAfterMs"], 4000)
        self.assertLessEqual(plan["idlePlan"]["source"]["postSpeechHoldMs"], 1000)

    def test_closed_smile_reopens_in_base_and_idle(self):
        plan = self.compile("closed_smile")
        event = plan["sequence"][0]
        self.assertEqual(event["patch"]["eyeLOpen"], 0)
        self.assertEqual(event["patch"]["eyeROpen"], 0)
        self.assertTrue(event["returnToBase"])
        self.assertLessEqual(event["durationMs"], 800)
        self.assertGreater(plan["basePose"]["params"]["eyeLOpen"], 0.8)
        self.assertGreater(plan["idlePlan"]["settlePose"]["params"]["eyeLOpen"], 0.8)

    def test_surprised_mouth_open_is_limited_to_reaction(self):
        plan = self.compile("surprised")
        self.assertGreater(plan["sequence"][0]["patch"]["mouthOpenBias"], 0.4)
        self.assertEqual(plan["basePose"]["params"]["mouthOpenBias"], 0)
        self.assertEqual(plan["idlePlan"]["settlePose"]["params"]["mouthOpenBias"], 0)

    def test_negative_emotion_and_topic_guard_cannot_be_replaced_by_smile(self):
        for emotion in ("sad", "gloomy", "angry", "conflicted"):
            with self.subTest(emotion=emotion):
                plan = self.compile("closed_smile", emotion=emotion)
                self.assertEqual(plan["debug"]["expressionFamily"], emotion)
                self.assertLess(plan["basePose"]["params"]["mouthForm"], 0)
                self.assertLess(plan["idlePlan"]["settlePose"]["params"]["mouthForm"], 0)
                self.assertNotIn("closed_smile", str(plan["sequence"]))
        plan = self.compile("soft_smile", topic_guard={"source_theme": "crying", "must_preserve_theme": True})
        self.assertEqual(plan["debug"]["expressionFamily"], "sad")

    def test_debug_route_fixed_model_and_seed_support_new_families(self):
        for family in ("calm", "listening", "thinking", "soft_smile", "closed_smile"):
            with self.subTest(family=family):
                payload = ExpressionPlanDebugRequest(kind=family, modelName="Hiyori", seed=42)
                result = asyncio.run(compile_debug_expression_plan(payload))
                self.assertEqual(result["plan"]["modelHints"]["modelName"], "Rushia")
                self.assertEqual(result["summary"]["expressionFamily"], canonical_rushia_family(family))
                self.assertEqual(result, asyncio.run(compile_debug_expression_plan(payload)))

    def test_chat_input_always_uses_rushia(self):
        for requested in (None, "Hiyori", "Haru", "../other", "Rushia"):
            self.assertEqual(normalize_chat_input({"content": "你好", "model_name": requested})["model_name"], "Rushia")

    def test_neutral_retains_jev_interaction_attitude(self):
        expected = {"smile": "calm", "bright_talk": "calm", "awkward": "thinking",
                    "tense_hold": "thinking", "smug": "playful", "cheeky_wink": "playful",
                    "goofy_face": "playful", "deadpan": "gloomy", "gloomy": "gloomy",
                    "meltdown": "angry", "volatile": "conflicted", "shock_recoil": "surprised"}
        for mode, family in expected.items():
            with self.subTest(mode=mode):
                plan = compile_expression_plan({"emotion": "neutral", "performance_mode": mode}, "Rushia", None, seed=42)
                self.assertEqual(plan["debug"]["expressionFamily"], family)
                self.assertEqual(plan["carryState"]["emotion"], "neutral")
                self.assertEqual(plan["carryState"]["performanceMode"], mode)
                if mode == "cheeky_wink":
                    self.assertIn("wink", plan["debug"]["expressionVariant"])
                if mode == "deadpan":
                    self.assertGreater(plan["basePose"]["params"]["mouthForm"], -0.2)

    def test_cheeky_wink_uses_both_sides_without_changing_negative_emotion(self):
        first = compile_expression_plan({"emotion": "neutral", "performance_mode": "cheeky_wink"}, "Rushia", None, seed=42)
        second = compile_expression_plan({"emotion": "neutral", "performance_mode": "cheeky_wink"}, "Rushia", first["carryState"], seed=42)
        first_patch = first["sequence"][0]["patch"]
        second_patch = second["sequence"][0]["patch"]
        self.assertNotEqual(first_patch["eyeLOpen"], second_patch["eyeLOpen"])
        self.assertNotEqual(first_patch["eyeROpen"], second_patch["eyeROpen"])
        sad = compile_expression_plan({"emotion": "sad", "performance_mode": "cheeky_wink"}, "Rushia", None, seed=42)
        self.assertEqual(sad["debug"]["expressionFamily"], "sad")
        self.assertNotIn("wink", str(sad["sequence"]))

    def test_native_brow_angles_are_not_overwritten_by_legacy_eye_sync(self):
        for family in FAMILY_POSES:
            with self.subTest(family=family):
                plan = self.compile(family)
                self.assertFalse(plan["basePose"]["params"]["eyeSync"])
                self.assertFalse(plan["idlePlan"]["settlePose"]["params"]["eyeSync"])
                self.assertTrue(all(event["patch"].get("eyeSync", False) is False for event in plan["sequence"]))
        # The native parameter atlas establishes mirrored numerical signs:
        # angry brows point inward/down (+/-), sad/shy inward/up (-/+).
        for family, left_sign in (("angry", 1), ("sad", -1), ("shy", -1)):
            pose = self.compile(family)["basePose"]["params"]
            self.assertGreater(pose["browLAngle"] * left_sign, 0)
            self.assertLess(pose["browRAngle"] * left_sign, 0)

    def test_eye_closure_survives_frontend_smoothing_and_reopens(self):
        plan = self.compile("closed_smile")
        event = plan["sequence"][0]
        base = plan["basePose"]["params"]["eyeLOpen"]
        # Reproduce the frontend's 60 Hz envelope + exponential filter. This
        # catches poses that request zero but never visibly reach closed eyes.
        eye = 1.0
        minimum = eye
        for frame in range(90):
            elapsed = frame * 1000 / 60
            fade = max(0, min(1, elapsed / event["fadeInMs"],
                              (event["durationMs"] - elapsed) / event["fadeOutMs"]))
            target = base * (1 - fade)
            eye += (target - eye) * (1 - math.exp(-8 / 60))
            minimum = min(minimum, eye)
        self.assertLess(minimum, 0.02)
        self.assertGreater(eye, 0.85)
        self.assertIn({"action": "pause", "durationSec": 0.95}, plan["blinkPlan"]["commands"])

    def test_idle_timer_accounts_for_runtime_sequence_fade_overlap(self):
        plan = self.compile("listening", variant="listen_left")
        # Directional reaction 1300 + rest 1300 + return 800, with two
        # 240 ms crossfades. Idle must wait for the actual final event.
        self.assertEqual(plan["idlePlan"]["source"]["actionEnterAfterMs"], 2920)
        self.assertLessEqual(plan["idlePlan"]["enterAfterMs"] - 2920, 1000)

    def test_debug_scenario_overrides_original_family(self):
        for scenario, families in (("speaking_micro", {"soft_smile", "closed_smile"}),
                                   ("brow_eye_micro", {"playful"})):
            with self.subTest(scenario=scenario):
                result = asyncio.run(compile_debug_expression_plan(
                    ExpressionPlanDebugRequest(kind="thinking", scenario=scenario, seed=42),
                ))
                self.assertIn(result["summary"]["expressionFamily"], families)

    def test_thinking_variants_keep_visible_gaze_and_brow_asymmetry(self):
        for variant in ("look_up_left", "look_up_right", "consider_then_return"):
            plan = self.compile("thinking", variant=variant)
            pose = plan["basePose"]["params"]
            peak = {**pose, **plan["sequence"][0]["patch"]}
            self.assertGreaterEqual(abs(peak["eyeBallX"]), 0.5)
            self.assertGreater(peak["browLY"] - peak["browRY"], 0.35)
            self.assertGreater(pose["eyeROpen"] - pose["eyeLOpen"], 0.1)
        calm = self.compile("calm")["basePose"]["params"]
        listening = self.compile("thinking", variant="attentive_nod")["basePose"]["params"]
        self.assertGreater(listening["mouthForm"], calm["mouthForm"] + 0.1)
        self.assertEqual(listening["browLY"], listening["browRY"])

    def test_left_and_right_glances_use_consistent_screen_direction(self):
        for family, left, right in (
            ("listening", "listen_left", "listen_right"),
            ("thinking", "look_up_left", "look_up_right"),
            ("teasing", "tease_left", "tease_right"),
            ("conflicted", "question_left", "question_right"),
        ):
            with self.subTest(family=family):
                left_patch = self.compile(family, variant=left)["sequence"][0]["patch"]
                right_patch = self.compile(family, variant=right)["sequence"][0]["patch"]
                self.assertLessEqual(left_patch["eyeBallX"], -0.5)
                self.assertGreaterEqual(right_patch["eyeBallX"], 0.5)
                self.assertLess(left_patch["bodyAngleZ"], 0)
                self.assertGreater(right_patch["bodyAngleZ"], 0)
                if family == "thinking":
                    self.assertGreaterEqual(left_patch["eyeBallY"], 0.3)
                    self.assertGreaterEqual(right_patch["eyeBallY"], 0.3)

    def test_glance_hold_survives_smoothing_and_returns_to_base(self):
        for family, variant in (("calm", "quiet_glance"), ("thinking", "look_up_left"),
                                ("shy", "shy_look_away"), ("playful", "playful_peek")):
            with self.subTest(family=family):
                plan = self.compile(family, variant=variant)
                event = plan["sequence"][0]
                base = plan["basePose"]["params"]["eyeBallX"]
                gaze = base
                held_frames = 0
                for frame in range(121):
                    elapsed = frame * 1000 / 60
                    fade = max(0, min(1, elapsed / event["fadeInMs"],
                                      (event["durationMs"] - elapsed) / event["fadeOutMs"]))
                    target = base + (event["patch"]["eyeBallX"] - base) * fade
                    # Gaze now has its own 20/s filter; eyelids retain 8/s.
                    gaze += (target - gaze) * (1 - math.exp(-20 / 60))
                    if abs(gaze - event["patch"]["eyeBallX"]) < 0.05:
                        held_frames += 1
                self.assertGreaterEqual(held_frames / 60, 0.6)
                self.assertAlmostEqual(gaze, base, delta=0.005)
                self.assertTrue(event["returnToBase"])

    def test_non_directional_reactions_and_winks_keep_existing_timing(self):
        for family, variant, duration in (("calm", "small_nod", 1000),
                                          ("angry", "firm_glare", 1000),
                                          ("surprised", "small_gasp", 680),
                                          ("playful", "playful_wink_left", 780),
                                          ("playful", "playful_wink_right", 780)):
            with self.subTest(variant=variant):
                event = self.compile(family, variant=variant)["sequence"][0]
                self.assertEqual(event["durationMs"], duration)
                self.assertLessEqual(event["fadeInMs"] + event["fadeOutMs"], duration)
        restrained = self.compile("angry", variant="restrained_turn")["sequence"][0]
        self.assertEqual(restrained["patch"]["eyeBallX"], 0.14)

    def test_directional_glance_does_not_become_permanent_base_or_carry(self):
        for family, variant in (("calm", "quiet_glance"), ("listening", "listen_left"),
                                ("thinking", "look_up_right"), ("teasing", "tease_left"),
                                ("shy", "shy_look_away")):
            with self.subTest(family=family):
                plan = self.compile(family, variant=variant)
                self.assertEqual(plan["basePose"]["params"]["eyeBallX"], 0)
                self.assertEqual(plan["idlePlan"]["settlePose"]["params"]["eyeBallX"], 0)
                self.assertEqual(plan["carryState"]["eyeBallX"], 0)
                self.assertNotEqual(plan["sequence"][0]["patch"]["eyeBallX"], 0)

    def test_quiet_eye_motion_has_real_bounded_amplitude_and_debug_none_stays_zero(self):
        for family in ("calm", "listening", "thinking", "soft_smile", "closed_smile"):
            with self.subTest(family=family):
                plan = self.compile(family)
                eye = plan["eyeMotionPlan"]
                self.assertEqual(eye["style"], "soft_saccade")
                self.assertGreater(eye["amplitudeX"], 0)
                self.assertGreater(eye["amplitudeY"], 0)
                self.assertLessEqual(eye["amplitudeX"], 0.09)
                self.assertLessEqual(eye["amplitudeY"], 0.035)
                self.assertEqual(eye["intensity"], 0.2)
                emotion = "happy" if family in {"soft_smile", "closed_smile"} else "neutral"
                disabled = compile_expression_plan(
                    {"emotion": emotion, "expression_family": family}, "Rushia", None, seed=12,
                    debug_overrides={"eyeMotionStyle": "none"},
                )["eyeMotionPlan"]
                self.assertEqual(disabled["style"], "none")
                self.assertEqual(disabled["amplitudeX"], 0)
                self.assertEqual(disabled["amplitudeY"], 0)
                self.assertEqual(disabled["intensity"], 0)

    def test_quiet_eye_motion_uses_soft_preset_even_with_an_alert_attitude(self):
        for mode in ("goofy_face", "shock_recoil", "meltdown", "awkward"):
            with self.subTest(mode=mode):
                eye = self.compile("thinking", performance_mode=mode)["eyeMotionPlan"]
                self.assertEqual(eye["style"], "soft_saccade")
                self.assertEqual(eye["blendInMs"], EYE_MOTION_PRESETS["soft_saccade"]["blendInMs"])
                self.assertEqual(eye["blendOutMs"], EYE_MOTION_PRESETS["soft_saccade"]["blendOutMs"])
                self.assertEqual(eye["frequencyHz"], 0.42)
                self.assertEqual(eye["intensity"], 0.2)

    def test_canonical_families_merge_all_existing_variants(self):
        self.assertEqual(set(FAMILY_POSES), set(FAMILY_VARIANTS))
        self.assertEqual(len(FAMILY_POSES), 11)
        self.assertNotIn("listening", FAMILY_POSES)
        self.assertNotIn("teasing", FAMILY_POSES)
        self.assertEqual({variant for variant, _patch in FAMILY_VARIANTS["thinking"]},
                         {"look_up_left", "look_up_right", "consider_then_return",
                          "attentive_nod", "listen_left", "listen_right"})
        self.assertEqual({variant for variant, _patch in FAMILY_VARIANTS["playful"]},
                         {"playful_peek", "playful_wink_left", "playful_wink_right", "tease_left", "tease_right"})
        self.assertEqual(sum(len(variants) for variants in FAMILY_VARIANTS.values()), 32)

    def test_merged_variants_preserve_authored_pose_blink_and_sequence_style(self):
        attentive = self.compile("thinking", variant="attentive_nod")
        considering = self.compile("thinking", variant="look_up_left")
        self.assertEqual(attentive["basePose"]["preset"], "rushia_listening")
        self.assertEqual(considering["basePose"]["preset"], "rushia_thinking")
        attentive_pose = attentive["basePose"]["params"]
        considering_pose = considering["basePose"]["params"]
        self.assertEqual(attentive_pose["browLY"], attentive_pose["browRY"])
        self.assertGreater(attentive_pose["mouthForm"], 0.1)
        self.assertGreater(considering_pose["browLY"] - considering_pose["browRY"], 0.35)
        self.assertLess(considering_pose["mouthForm"], 0)
        for plan in (attentive, considering):
            self.assertEqual(plan["debug"]["expressionFamily"], "thinking")
            self.assertEqual(plan["carryState"]["expressionFamily"], "thinking")
            self.assertEqual(plan["motionPlan"]["theme"], "rushia_thinking")
            self.assertTrue(any(event["kind"] == "rushia_return_attention" for event in plan["sequence"]))

        teasing = self.compile("playful", variant="tease_left", blink_style="teasing_pause")
        wink = self.compile("playful", variant="playful_wink_left", blink_style="teasing_pause")
        self.assertEqual(teasing["basePose"]["preset"], "rushia_teasing")
        self.assertEqual(wink["basePose"]["preset"], "rushia_playful")
        self.assertLess(teasing["basePose"]["params"]["mouthForm"], wink["basePose"]["params"]["mouthForm"])
        self.assertEqual(teasing["sequence"][0]["durationMs"], 1300)
        self.assertEqual(wink["sequence"][0]["durationMs"], 780)
        self.assertIn({"action": "pause", "durationSec": 1.0}, teasing["blinkPlan"]["commands"])
        self.assertIn({"action": "pause", "durationSec": 0.95}, wink["blinkPlan"]["commands"])
        for plan in (teasing, wink):
            self.assertEqual(plan["debug"]["expressionFamily"], "playful")
            self.assertEqual(plan["carryState"]["expressionFamily"], "playful")
            self.assertEqual(plan["motionPlan"]["theme"], "playful_tease")

    def test_jev_raw_emotion_and_attitude_are_preserved_with_canonical_rushia_families(self):
        for emotion, attitude, family in (("neutral", "smug", "playful"),
                                         ("happy", "smug", "playful"),
                                         ("neutral", "awkward", "thinking"),
                                         ("sad", "smug", "sad")):
            with self.subTest(emotion=emotion, attitude=attitude):
                answers = {
                    "base_emotion": {"type": "choice", "choice": emotion, "confidence": 0.9},
                    "interaction_attitude": {"type": "choice", "choice": attitude, "confidence": 0.9},
                }
                intent = map_answers_to_intent(answers)
                plan = compile_expression_plan(intent, "Rushia", None, seed=7)
                self.assertEqual(answers["base_emotion"]["choice"], emotion)
                self.assertEqual(intent["emotion"], emotion)
                self.assertEqual(intent["performance_mode"], attitude)
                self.assertEqual(plan["debug"]["intentEmotion"], emotion)
                self.assertEqual(plan["carryState"]["emotion"], emotion)
                self.assertEqual(plan["debug"]["expressionFamily"], family)
                self.assertEqual(plan["carryState"]["expressionFamily"], family)
                if family == "playful":
                    self.assertEqual(plan["motionPlan"]["theme"], "playful_tease")


if __name__ == "__main__":
    unittest.main()
