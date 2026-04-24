import pathlib
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.agent_b_prompts import build_live2d_prompt
from domain.tools.schema_loader import load_schema


class Live2DPromptConfigTests(unittest.TestCase):
    def test_set_ai_behavior_schema_is_partial_friendly(self):
        schema = load_schema("Hiyori")
        live2d_tools = schema["openai_tools"]["live2d"]
        behavior_tool = next(
            tool for tool in live2d_tools if tool["function"]["name"] == "set_ai_behavior"
        )

        required = behavior_tool["function"]["parameters"].get("required", [])

        self.assertEqual(required, [])

    def test_live2d_prompt_explicitly_separates_daily_chat_and_exaggerated_faces(self):
        prompt = build_live2d_prompt(
            user_message="做個鬼臉",
            agent_a_reply="做個鬼臉給你看，哼。",
            previous_expression_state={
                "mouth_form": 0.08,
                "eye_sync": True,
                "summary": "上一輪是輕微傲嬌笑，變化不大",
            },
            jpaf_state={"active_function": "Ne", "suggested_persona": "tsundere"},
            emotion_state={
                "primary_emotion": "playful",
                "intensity": "high",
                "expression_arc": "teasing_to_face",
            },
            model_name="Hiyori",
        )

        self.assertIn("日常聊天時表情可以自然", prompt)
        self.assertIn("做鬼臉、生氣、驚訝、強烈吐槽時", prompt)
        self.assertIn("至少讓眼睛、眉毛、嘴角三者中的兩者有明顯變化", prompt)
        self.assertIn("若模型視覺變化偏小，優先把 eye_*_open、eye_*_smile、brow_*、mouth_form 拉開", prompt)

    def test_live2d_prompt_prioritizes_direct_user_expression_request(self):
        prompt = build_live2d_prompt(
            user_message="生氣一下",
            agent_a_reply="哼，我才沒有真的生氣。",
            previous_expression_state={
                "mouth_form": 0.2,
                "eye_sync": True,
                "summary": "上一輪偏平靜，只帶一點嘴硬",
            },
            jpaf_state={"active_function": "Ti", "suggested_persona": "tsundere"},
            emotion_state={
                "primary_emotion": "neutral",
                "intensity": "medium",
            },
            model_name="Hiyori",
        )

        self.assertIn("【用戶的直接表情要求】", prompt)
        self.assertIn("生氣一下", prompt)
        self.assertIn("若用戶直接指定表情或動作", prompt)
        self.assertIn("優先滿足該表演要求，而不是只回到 persona 的安全基底", prompt)

    def test_live2d_prompt_uses_previous_expression_and_drops_persona_parameter_templates(self):
        prompt = build_live2d_prompt(
            user_message="正常聊天就好",
            agent_a_reply="嗯，今天就慢慢聊吧。",
            previous_expression_state={
                "summary": "上一輪是淡淡微笑、雙眼自然、眉毛幾乎不動",
                "mouth_form": 0.12,
                "eye_sync": True,
            },
            jpaf_state={"active_function": "Fi", "suggested_persona": "tsundere"},
            emotion_state={
                "primary_emotion": "calm",
                "intensity": "low",
            },
            model_name="Hiyori",
        )

        self.assertIn("【上一個表情摘要】", prompt)
        self.assertIn("淡淡微笑", prompt)
        self.assertIn("優先參考上一個表情摘要來維持連續性", prompt)
        self.assertNotIn("## persona 表情對應", prompt)
        self.assertNotIn("**tsundere（傲嬌）**", prompt)


if __name__ == "__main__":
    unittest.main()
