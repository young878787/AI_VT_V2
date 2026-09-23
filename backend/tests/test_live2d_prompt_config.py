import pathlib
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.tools.schema_loader import load_schema


class Live2DContractTests(unittest.TestCase):
    def test_existing_behavior_tool_schema_stays_partial_friendly(self):
        schema = load_schema("Hiyori")
        behavior = next(
            tool for tool in schema["openai_tools"]["live2d"]
            if tool["function"]["name"] == "set_ai_behavior"
        )
        self.assertEqual(behavior["function"]["parameters"].get("required", []), [])

    def test_memory_schema_remains_available_without_expression_agent_prompt(self):
        schema = load_schema("Hiyori")
        self.assertIn("memory", schema["prompt_config"])
        from domain import agent_b_prompts
        self.assertTrue(callable(agent_b_prompts.build_memory_prompt))
        self.assertFalse(hasattr(agent_b_prompts, "build_live2d_prompt"))


if __name__ == "__main__":
    unittest.main()
