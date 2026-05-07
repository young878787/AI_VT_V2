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
        return case, intent, compile_expression_plan(intent, model_name="Hiyori", previous_state=None)

    def test_fake_motion_debug_case_flows_through_parser_and_compiler(self):
        _case, intent, plan = self._compile_case(motion_kind="locked_glare", intensity="strong")

        self.assertEqual(intent["motion_theme"], "angry_tension")
        self.assertEqual(intent["motion_variant"], "locked_glare")
        self.assertEqual(plan["type"], "expression_plan")
        self.assertEqual(plan["motionPlan"]["theme"], "angry_tension")
        self.assertEqual(plan["motionPlan"]["variant"], "locked_glare")
        self.assertEqual(plan["debug"]["intentEmotion"], "angry")

    def test_speaking_micro_debug_case_uses_backend_micro_event_bias(self):
        _case, intent, plan = self._compile_case(scenario="speaking_micro", intensity="normal")

        self.assertEqual(intent["emotion"], "happy")
        self.assertIn("brow_micro_dual_lift", intent["must_include"])
        self.assertTrue(plan["microEvents"])
        event_kinds = {event["kind"] for event in plan["microEvents"]}
        self.assertIn("brow_micro_dual_lift", event_kinds)
        self.assertGreater(plan["idlePlan"]["source"]["speakingEnterAfterMs"], 0)


if __name__ == "__main__":
    unittest.main()
