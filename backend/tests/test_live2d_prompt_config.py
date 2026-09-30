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

    def test_memory_tools_are_removed_from_live2d_schema(self):
        schema = load_schema("Hiyori")
        self.assertNotIn("memory", schema["openai_tools"])
        self.assertNotIn("memory", schema["prompt_config"])


if __name__ == "__main__":
    unittest.main()
