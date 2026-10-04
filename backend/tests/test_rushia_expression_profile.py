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
from domain.input_event import normalize_chat_input
from domain.rushia_expression_profile import FAMILY_POSES
from services.expression_compiler import compile_expression_plan


class RushiaExpressionProfileTests(unittest.TestCase):
    def compile(self, family="calm", *, previous=None, seed=12, **extra):
        emotion = "happy" if family in {"soft_smile", "closed_smile"} else family
        if family in {"calm", "listening", "thinking"}:
            emotion = "neutral"
        return compile_expression_plan(
            {"emotion": emotion, "expression_family": family, **extra},
            "Rushia", previous, seed=seed,
        )

    def test_seed_replays_without_reading_or_mutating_global_random_state(self):
        before = random.getstate()
        first = self.compile("thinking")
        second = self.compile("thinking")
        self.assertEqual(first, second)
        self.assertEqual(before, random.getstate())

    def test_variants_do_not_repeat_across_turns_even_with_same_seed(self):
        for family in ("calm", "listening", "thinking", "soft_smile", "closed_smile"):
            with self.subTest(family=family):
                first = self.compile(family)
                second = self.compile(family, previous=first["carryState"])
                self.assertNotEqual(first["debug"]["expressionVariant"], second["debug"]["expressionVariant"])
                variants = {self.compile(family, seed=seed)["debug"]["expressionVariant"] for seed in range(16)}
                self.assertEqual(len(variants), 3)

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
                self.assertEqual(result["summary"]["expressionFamily"], family)
                self.assertEqual(result, asyncio.run(compile_debug_expression_plan(payload)))

    def test_chat_input_always_uses_rushia(self):
        for requested in (None, "Hiyori", "Haru", "../other", "Rushia"):
            self.assertEqual(normalize_chat_input({"content": "你好", "model_name": requested})["model_name"], "Rushia")

    def test_neutral_retains_jev_interaction_attitude(self):
        expected = {"smile": "calm", "bright_talk": "calm", "awkward": "listening",
                    "tense_hold": "listening", "smug": "teasing", "cheeky_wink": "playful",
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
        plan = self.compile("listening")
        self.assertEqual(plan["idlePlan"]["source"]["actionEnterAfterMs"], 2620)
        self.assertLessEqual(plan["idlePlan"]["enterAfterMs"] - 2620, 1000)

    def test_debug_scenario_overrides_original_family(self):
        for scenario, families in (("speaking_micro", {"soft_smile", "closed_smile"}),
                                   ("brow_eye_micro", {"teasing"})):
            with self.subTest(scenario=scenario):
                result = asyncio.run(compile_debug_expression_plan(
                    ExpressionPlanDebugRequest(kind="thinking", scenario=scenario, seed=42),
                ))
                self.assertIn(result["summary"]["expressionFamily"], families)

    def test_thinking_variants_keep_visible_gaze_and_brow_asymmetry(self):
        for seed in range(16):
            plan = self.compile("thinking", seed=seed)
            pose = plan["basePose"]["params"]
            peak = {**pose, **plan["sequence"][0]["patch"]}
            self.assertGreaterEqual(abs(peak["eyeBallX"]), 0.5)
            self.assertGreater(peak["browLY"] - peak["browRY"], 0.35)
            self.assertGreater(pose["eyeROpen"] - pose["eyeLOpen"], 0.1)
        calm = self.compile("calm")["basePose"]["params"]
        listening = self.compile("listening")["basePose"]["params"]
        self.assertGreater(listening["mouthForm"], calm["mouthForm"] + 0.1)
        self.assertEqual(listening["browLY"], listening["browRY"])


if __name__ == "__main__":
    unittest.main()
