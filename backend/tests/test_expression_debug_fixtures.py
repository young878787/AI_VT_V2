import unittest

from domain.expression_debug_fixtures import build_fake_expression_debug_case
from services.expression_compiler import compile_expression_plan
from services.expression_intent_parser import parse_expression_intent


class ExpressionDebugFixturesTest(unittest.TestCase):
    def _compile_case(self, **kwargs):
        case = build_fake_expression_debug_case(**kwargs)
        intent = parse_expression_intent(
            case["rawReply"],
            emotion_state=None,
            previous_state=None,
            user_message=case["spokenText"],
        )
        intent["spoken_text"] = case["spokenText"]
        if case["expressionFamily"]:
            intent["expression_family"] = case["expressionFamily"]
        return case, intent, compile_expression_plan(intent, model_name="Rushia", previous_state=None)

    def test_fake_motion_debug_case_flows_through_parser_and_compiler(self):
        _case, intent, plan = self._compile_case(motion_kind="locked_glare", intensity="strong")

        self.assertEqual(intent["motion_theme"], "angry_tension")
        self.assertEqual(intent["motion_variant"], "locked_glare")
        self.assertEqual(plan["type"], "expression_plan")
        self.assertEqual(plan["motionPlan"]["theme"], "angry_tension")
        self.assertEqual(plan["motionPlan"]["variant"], "locked_glare")
        self.assertEqual(plan["debug"]["intentEmotion"], "angry")

    def test_speaking_scenario_describes_real_rushia_sequence(self):
        case, intent, plan = self._compile_case(scenario="speaking_micro", intensity="normal")

        self.assertEqual(intent["emotion"], "happy")
        self.assertEqual(case["expressionVariant"], "warm_nod")
        self.assertEqual(intent["must_include"], [])
        self.assertEqual(plan["microEvents"], [])
        event = next(event for event in plan["sequence"] if event["kind"] == "rushia_phrase_emphasis")
        self.assertGreater(event["patch"]["browLY"], plan["basePose"]["params"]["browLY"])
        self.assertGreater(plan["idlePlan"]["source"]["speakingEnterAfterMs"], 0)

    def test_legacy_fixture_names_normalize_to_canonical_family(self):
        for old, canonical in (("listening", "thinking"), ("teasing", "playful")):
            with self.subTest(old=old):
                old_case = build_fake_expression_debug_case(kind=old, seed=7)
                canonical_case = build_fake_expression_debug_case(kind=canonical, seed=7)
                self.assertEqual(old_case, canonical_case)
                self.assertEqual(old_case["kind"], canonical)


if __name__ == "__main__":
    unittest.main()
