import pathlib
import sys
import unittest
from unittest.mock import patch


BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routes.expression_debug_router import router
from domain.expression_blink_strategies import BLINK_STRATEGIES
from domain.expression_eye_motion_library import EYE_MOTION_PRESETS
from domain.expression_motion_library import MOTION_BRANCH_LIBRARY
from domain.rushia_expression_profile import FAMILY_VARIANTS, RUSHIA_IDLE_FAMILIES
from services.expression_compiler import compile_expression_plan


class ExpressionDebugRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def compile(self, **payload):
        response = self.client.post("/api/debug/expression-plan", json={"seed": 7, **payload})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_catalog_matches_authoritative_libraries(self):
        response = self.client.get("/api/debug/expression-catalog")
        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        self.assertEqual(catalog["modelName"], "Rushia")
        self.assertEqual({item["id"] for item in catalog["expressionFamilies"]}, set(FAMILY_VARIANTS))
        self.assertEqual({variant["id"] for item in catalog["expressionFamilies"] for variant in item["variants"]},
                         {variant for variants in FAMILY_VARIANTS.values() for variant, _patch in variants})
        self.assertEqual(len(catalog["expressionFamilies"]), 11)
        self.assertEqual(sum(len(item["variants"]) for item in catalog["expressionFamilies"]), 32)
        self.assertEqual({item["id"] for item in catalog["motions"]},
                         {branch["variant"] for branches in MOTION_BRANCH_LIBRARY.values() for branch in branches})
        self.assertEqual(len(catalog["motions"]), 13)
        for key, allowed in (("eyeStyles", EYE_MOTION_PRESETS), ("blinkStyles", BLINK_STRATEGIES),
                             ("idleStyles", RUSHIA_IDLE_FAMILIES)):
            self.assertEqual({item["id"] for item in catalog[key]}, set(allowed))
            self.assertTrue(all(item["label"] != item["id"] for item in catalog[key]))
        self.assertTrue(all(variant["label"] != variant["id"]
                            for family in catalog["expressionFamilies"] for variant in family["variants"]))

    def test_all_32_variants_can_be_selected_and_replayed_after_previous_same_variant(self):
        for family, variants in FAMILY_VARIANTS.items():
            for variant, _patch in variants:
                with self.subTest(family=family, variant=variant):
                    payload = {"kind": family, "expressionVariant": variant,
                               "previousState": {"expressionVariant": variant}}
                    first = self.compile(**payload)
                    self.assertEqual(first["summary"]["expressionFamily"], family)
                    self.assertEqual(first["summary"]["expressionVariant"], variant)
                    self.assertEqual(first["plan"]["sequence"][0]["kind"], f"rushia_{variant}")
                    self.assertEqual(first, self.compile(**payload))

    def test_all_13_motion_kinds_resolve_to_requested_branch(self):
        for theme, branches in MOTION_BRANCH_LIBRARY.items():
            for branch in branches:
                with self.subTest(variant=branch["variant"]):
                    result = self.compile(motionKind=branch["variant"])
                    self.assertEqual(result["plan"]["motionPlan"]["theme"], theme)
                    self.assertEqual(result["plan"]["motionPlan"]["variant"], branch["variant"])
                    self.assertEqual(result, self.compile(motionKind=branch["variant"]))

    def test_invalid_and_conflicting_selectors_return_errors(self):
        bad_payloads = (
            {"kind": "missing"}, {"motionKind": "missing"}, {"scenario": "missing"},
            {"kind": "calm", "expressionVariant": "missing"},
            {"kind": "calm", "expressionVariant": "warm_nod"},
            {"kind": "calm", "eyeMotionStyle": "missing"}, {"blinkStyle": "missing"},
            {"idleStyle": "missing"}, {"intent": {"expression_family": "missing"}},
            {"intent": {"emotion": "neutral"}, "kind": "missing"},
            {"intent": {"emotion": "neutral"}, "motionKind": "missing"},
            {"intent": {"emotion": "neutral"}, "scenario": "missing"},
            {"kind": ""}, {"motionKind": ""}, {"scenario": ""},
            {"intent": {"emotion": "angry", "expression_family": "calm", "expression_variant": "small_nod"}},
            {"intent": {"emotion": "neutral", "motion_theme": "playful_tease", "motion_variant": "peek_shift"}},
            {"intent": {"emotion": "playful", "motion_theme": "low_mood", "motion_variant": "peek_shift"}},
            {"intent": {"motion_variant": "missing"}},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                response = self.client.post("/api/debug/expression-plan", json=payload)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertTrue(response.json()["detail"])

    def test_eye_debug_override_bypasses_quiet_restriction_only_in_debug(self):
        for style in EYE_MOTION_PRESETS:
            with self.subTest(style=style):
                result = self.compile(kind="calm", eyeMotionStyle=style)
                self.assertEqual(result["plan"]["eyeMotionPlan"]["style"], style)
                self.assertEqual(result["summary"]["eyeMotionStyle"], style)
                if style == "none":
                    self.assertEqual(result["plan"]["eyeMotionPlan"]["intensity"], 0)
                    self.assertEqual(result["plan"]["eyeMotionPlan"]["amplitudeX"], 0)
        normal = compile_expression_plan({"emotion": "neutral", "expression_family": "calm",
                                          "eye_motion_style": "alert_scan"}, "Rushia", None, seed=7)
        self.assertEqual(normal["eyeMotionPlan"]["style"], "soft_saccade")
        self.assertLessEqual(normal["eyeMotionPlan"]["amplitudeX"], 0.09)

    def test_blink_overrides_include_strategy_and_preserve_closed_eye_pause(self):
        for style, commands in BLINK_STRATEGIES.items():
            with self.subTest(style=style):
                plan = self.compile(kind="closed_smile", expressionVariant="closed_smile_nod", blinkStyle=style)["plan"]
                self.assertEqual(plan["blinkPlan"]["style"], style)
                for command in commands:
                    self.assertIn(command, plan["blinkPlan"]["commands"])
                self.assertIn({"action": "pause", "durationSec": 0.95}, plan["blinkPlan"]["commands"])
        normal = self.compile(kind="calm", blinkStyle="normal")["plan"]["blinkPlan"]["commands"]
        self.assertIn({"action": "set_interval", "intervalMin": 2.5, "intervalMax": 5.0}, normal)

    def test_idle_override_changes_actual_settle_pose_not_only_name(self):
        for name, family in RUSHIA_IDLE_FAMILIES.items():
            with self.subTest(name=name):
                plan = self.compile(kind="calm", idleStyle=name)["plan"]
                self.assertEqual(plan["idlePlan"]["name"], name)
                self.assertEqual(plan["idlePlan"]["settlePose"]["preset"], f"rushia_{family}_rest")
                self.assertEqual(plan["idlePlan"]["settlePose"]["params"]["mouthOpenBias"], 0)
                self.assertTrue(plan["idlePlan"]["interruptible"])
        sad = self.compile(kind="calm", idleStyle="crying_idle")["plan"]["idlePlan"]["settlePose"]
        happy = self.compile(kind="sad", idleStyle="happy_idle")["plan"]["idlePlan"]["settlePose"]
        self.assertLess(sad["params"]["mouthForm"], 0)
        self.assertGreater(happy["params"]["mouthForm"], 0)
        self.assertEqual(happy["bodyMotionProfile"]["style"], "calm_sway")

    def test_legacy_scenario_ids_match_real_rushia_sequence(self):
        for scenario, family, variant in (("speaking_micro", "soft_smile", "warm_nod"),
                                          ("brow_eye_micro", "playful", "tease_left")):
            with self.subTest(scenario=scenario):
                plan = self.compile(kind="thinking", scenario=scenario)["plan"]
                self.assertEqual(plan["debug"]["expressionFamily"], family)
                self.assertEqual(plan["debug"]["expressionVariant"], variant)
                self.assertEqual(plan["microEvents"], [])
                self.assertTrue(plan["sequence"][0]["patch"])
        plan = self.compile(scenario="speaking_micro")["plan"]
        self.assertTrue(any(event["kind"] == "rushia_phrase_emphasis" for event in plan["sequence"]))

    def test_direct_intent_supports_exact_variant_without_changing_regular_compiler(self):
        intent = {"emotion": "neutral", "expression_family": "calm", "expression_variant": "small_nod"}
        self.assertEqual(self.compile(intent=intent)["summary"]["expressionVariant"], "small_nod")
        ordinary = compile_expression_plan(intent, "Rushia", None, seed=7)
        self.assertEqual(ordinary["debug"]["expressionVariant"], "quiet_glance")

    def test_legacy_kind_aliases_match_canonical_debug_requests(self):
        for old, canonical in (("listening", "thinking"), ("teasing", "playful")):
            for variant, _patch in FAMILY_VARIANTS[canonical]:
                with self.subTest(old=old, variant=variant):
                    legacy = self.compile(kind=old, expressionVariant=variant)
                    current = self.compile(kind=canonical, expressionVariant=variant)
                    self.assertEqual(legacy, current)
                    self.assertEqual(legacy["summary"]["expressionFamily"], canonical)
                    self.assertEqual(legacy["plan"]["carryState"]["expressionFamily"], canonical)

    def test_direct_intent_aliases_are_canonicalized_without_mutating_raw_emotion(self):
        for old, canonical, variant, emotion in (("listening", "thinking", "attentive_nod", "neutral"),
                                                 ("teasing", "playful", "tease_left", "teasing")):
            with self.subTest(old=old):
                intent = {"emotion": emotion, "expression_family": old, "expression_variant": variant}
                legacy = self.compile(intent=intent)
                current = self.compile(intent={**intent, "expression_family": canonical})
                self.assertEqual(legacy, current)
                self.assertEqual(legacy["summary"]["expressionFamily"], canonical)
                self.assertEqual(legacy["plan"]["debug"]["intentEmotion"], emotion)
                self.assertEqual(intent["expression_family"], old)
                self.assertEqual(intent["emotion"], emotion)

    def test_debug_disabled_applies_to_catalog_and_compiler(self):
        with patch("api.routes.expression_debug_router.env_flag", return_value=False):
            self.assertEqual(self.client.get("/api/debug/expression-catalog").status_code, 404)
            self.assertEqual(self.client.post("/api/debug/expression-plan", json={"kind": "calm"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
