import pathlib
import sys
import tempfile
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from core import prompt_logger


class Task2NamingTests(unittest.TestCase):
    def test_agent_b_prompts_uses_task2_surface_naming(self):
        source = (BACKEND_ROOT / "domain" / "agent_b_prompts.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("【AI 角色的回覆】", source)
        self.assertIn("emotion（主題情緒主軸）", source)
        self.assertNotIn("agent_a_reply", source)
        self.assertNotIn("agent_a_reply 的語氣", source)

    def test_chat_ws_uses_chat_orchestrator_wording_consistently(self):
        source = (BACKEND_ROOT / "api" / "routes" / "chat_ws.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("Chat Orchestrator", source)
        self.assertIn(
            "Chat Orchestrator: parallel Expression Agent + Memory Agent...",
            source,
        )
        self.assertNotIn(
            "Chat Orchestrator: parallel expression + memory...",
            source,
        )

        legacy_labels = (
            "Agent orchestration",
            "Agent A",
            "Agent B",
        )
        for legacy_label in legacy_labels:
            with self.subTest(legacy_label=legacy_label):
                self.assertNotIn(legacy_label, source)

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
                    tool_names=["set_ai_behavior"],
                    output_tokens=12,
                )
            finally:
                prompt_logger._LOG_DIR = original_log_dir
                prompt_logger._LOG_FILE = original_log_file

            content = (temp_dir / "prompt.log").read_text(encoding="utf-8")

        self.assertIn("[DIALOGUE AGENT OUTPUT]", content)
        self.assertIn("[EXPRESSION AGENT / MEMORY AGENT TOOL CALLS]", content)
        self.assertNotIn("[TOOL CALLS]", content)
        self.assertIn("set_ai_behavior", content)
        self.assertIn("reply", content)


if __name__ == "__main__":
    unittest.main()
