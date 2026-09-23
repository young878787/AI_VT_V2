import pathlib
import sys
import tempfile
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from core import prompt_logger


class Task2NamingTests(unittest.TestCase):
    def test_agent_b_prompts_only_contains_memory_agent(self):
        source = (BACKEND_ROOT / "domain" / "agent_b_prompts.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("【AI 角色的回覆】", source)
        self.assertNotIn("build_live2d_prompt", source)

    def test_chat_ws_only_uses_jev_decision_path(self):
        source = (BACKEND_ROOT / "api" / "routes" / "chat_ws.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("build_jev_questions", source)
        self.assertEqual(source.count("await call_jev("), 1)
        self.assertNotIn("EXPRESSION_DECIDER", source)
        self.assertNotIn("call_expression_agent", source)

    def test_log_turn_accepts_dialogue_agent_output_keyword(self):
        original_log_dir = prompt_logger._LOG_DIR
        original_log_file = prompt_logger._LOG_FILE

        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_dir = pathlib.Path(tmp_dir)
            prompt_logger._LOG_DIR = temp_dir
            prompt_logger._LOG_FILE = temp_dir / "prompt.log"

            try:
                prompt_logger.log_turn(
                    turn_count=3,
                    system_prompt="system",
                    user_message="hello",
                    dialogue_agent_output="reply",
                    tool_names=["save_memory_note"],
                    output_tokens=12,
                )
            finally:
                prompt_logger._LOG_DIR = original_log_dir
                prompt_logger._LOG_FILE = original_log_file

            content = (temp_dir / "prompt.log").read_text(encoding="utf-8")

        self.assertIn("[DIALOGUE AGENT OUTPUT]", content)
        self.assertIn("[MEMORY AGENT TOOL CALLS]", content)
        self.assertNotIn("[TOOL CALLS]", content)
        self.assertIn("save_memory_note", content)
        self.assertIn("reply", content)


if __name__ == "__main__":
    unittest.main()
