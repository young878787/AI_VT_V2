import pathlib
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routes.chat_ws import (
    _effective_incoming_session_id,
    _reset_messages_for_session,
    _summarize_previous_expression_state,
    _reset_behavior_payload_for_session,
)
from domain.agent_b_prompts import build_live2d_prompt
from domain.tools.schema_loader import load_schema


class Live2DPromptConfigTests(unittest.TestCase):
    def test_normal_chat_frame_without_session_id_keeps_active_session(self):
        self.assertEqual(
            "session-a",
            _effective_incoming_session_id(
                {"message": "continue talking"},
                current_session_id="session-a",
            ),
        )

    def test_reset_control_frame_without_session_id_keeps_active_session(self):
        self.assertEqual(
            "session-a",
            _effective_incoming_session_id(
                {"type": "reset"},
                current_session_id="session-a",
            ),
        )

        self.assertIsNone(
            _effective_incoming_session_id(
                {"type": "reset"},
                current_session_id=None,
            )
        )

    def test_session_switch_clears_in_memory_messages_without_persistence(self):
        existing_messages = [
            {"role": "system", "content": "prompt"},
            {"role": "user", "content": "session-a message"},
        ]

        self.assertEqual(
            [],
            _reset_messages_for_session(
                current_session_id="session-a",
                incoming_session_id="session-b",
                messages=existing_messages,
                persistence_enabled=False,
            ),
        )
        self.assertEqual(
            existing_messages,
            _reset_messages_for_session(
                current_session_id="session-a",
                incoming_session_id="session-a",
                messages=existing_messages,
                persistence_enabled=False,
            ),
        )

    def test_session_switch_clears_previous_behavior_payload(self):
        previous_behavior_payload = {
            "type": "behavior",
            "mouthForm": 0.42,
            "eyeSync": False,
        }

        self.assertIsNone(
            _reset_behavior_payload_for_session(
                current_session_id="session-a",
                incoming_session_id="session-b",
                last_behavior_payload=previous_behavior_payload,
            )
        )
        self.assertEqual(
            previous_behavior_payload,
            _reset_behavior_payload_for_session(
                current_session_id="session-a",
                incoming_session_id="session-a",
                last_behavior_payload=previous_behavior_payload,
            ),
        )

    def test_set_ai_behavior_schema_is_partial_friendly(self):
        schema = load_schema("Hiyori")
        live2d_tools = schema["openai_tools"]["live2d"]
        behavior_tool = next(
            tool for tool in live2d_tools if tool["function"]["name"] == "set_ai_behavior"
        )

        required = behavior_tool["function"]["parameters"].get("required", [])

        self.assertEqual(required, [])

    def test_live2d_task_description_drops_jpaf_state_wording(self):
        schema = load_schema("Hiyori")
        task_description = schema["prompt_config"]["live2d"]["task_description"]

        self.assertNotIn("JPAF 狀態", task_description)

        prompt = build_live2d_prompt(
            user_message="表情自然一點",
            ai_role_reply="好啊，就自然一點。",
            previous_expression_state=None,
            emotion_state=None,
            model_name="Hiyori",
        )

        self.assertNotIn("JPAF 狀態", prompt)

    def test_live2d_prompt_explicitly_separates_daily_chat_and_exaggerated_faces(self):
        prompt = build_live2d_prompt(
            user_message="做個鬼臉",
            ai_role_reply="做個鬼臉給你看，哼。",
            previous_expression_state={
                "mouth_form": 0.08,
                "eye_sync": True,
                "summary": "上一輪是輕微傲嬌笑，變化不大",
            },
            emotion_state={
                "primary_emotion": "playful",
                "secondary_emotion": "teasing",
                "energy": 0.84,
                "intensity": 0.91,
                "pace": "medium_fast",
                "blink_suggestion": "force_blink",
                "asymmetry_bias": "strong",
                "expression_arc": "teasing_to_face",
            },
            model_name="Hiyori",
        )

        self.assertIn("【emotion_state】", prompt)
        self.assertIn("- energy: 0.84", prompt)
        self.assertIn("- intensity: 0.91", prompt)
        self.assertIn("- asymmetry_bias: strong", prompt)
        self.assertIn("【上一個表情摘要】", prompt)
        self.assertIn("energy / intensity 數值偏高（例如接近 0.7 以上）", prompt)
        self.assertNotIn("high energy / high intensity", prompt)
        self.assertIn("做鬼臉、生氣、驚訝、強烈吐槽時", prompt)
        self.assertIn("至少讓眼睛、眉毛、嘴角三者中的兩者有明顯變化", prompt)
        self.assertIn("優先把 eye_*_open、eye_*_smile、brow_*、mouth_form 拉開", prompt)

    def test_live2d_prompt_prioritizes_direct_user_expression_request(self):
        prompt = build_live2d_prompt(
            user_message="生氣一下",
            ai_role_reply="哼，我才沒有真的生氣。",
            previous_expression_state={
                "mouth_form": 0.2,
                "eye_sync": True,
                "summary": "上一輪偏平靜，只帶一點嘴硬",
            },
            emotion_state={
                "primary_emotion": "neutral",
                "secondary_emotion": "angry",
                "energy": 0.36,
                "intensity": 0.58,
                "pace": "medium",
                "blink_suggestion": "none",
                "asymmetry_bias": "slight",
                "expression_arc": "neutral_to_irritated",
            },
            model_name="Hiyori",
        )

        self.assertIn("【用戶的直接表情要求】", prompt)
        self.assertIn("生氣一下", prompt)
        self.assertIn("請優先根據用戶的直接表情要求", prompt)
        self.assertIn("不要依賴任何上游內部人格狀態欄位", prompt)
        self.assertIn("若用戶直接指定表情或動作", prompt)
        self.assertIn("優先滿足該表演要求", prompt)

    def test_live2d_prompt_uses_previous_expression_and_drops_dialogue_internal_state(self):
        prompt = build_live2d_prompt(
            user_message="正常聊天就好",
            ai_role_reply="嗯，今天就慢慢聊吧。",
            previous_expression_state={
                "summary": "上一輪是淡淡微笑、雙眼自然、眉毛幾乎不動",
                "mouth_form": 0.12,
                "eye_sync": True,
                "brow_l_form": 0.22,
                "brow_r_form": 0.18,
                "brow_l_x": -0.14,
                "brow_r_x": 0.11,
            },
            emotion_state={
                "primary_emotion": "calm",
                "secondary_emotion": "neutral",
                "energy": 0.22,
                "intensity": 0.18,
                "pace": "slow",
                "blink_suggestion": "resume",
                "asymmetry_bias": "none",
                "expression_arc": "calm_hold",
            },
            model_name="Hiyori",
        )

        self.assertIn("【上一個表情摘要】", prompt)
        self.assertIn("淡淡微笑", prompt)
        self.assertIn("- brow_l_form: 0.22", prompt)
        self.assertIn("- brow_r_form: 0.18", prompt)
        self.assertIn("- brow_l_x: -0.14", prompt)
        self.assertIn("- brow_r_x: 0.11", prompt)
        self.assertIn("優先參考上一個表情摘要來維持連續性", prompt)
        self.assertNotIn("active_function", prompt)
        self.assertNotIn("persona", prompt)

    def test_previous_expression_summary_preserves_fields_used_by_prompt(self):
        summary = _summarize_previous_expression_state({
            "mouthForm": -0.33,
            "eyeSync": False,
            "eyeLOpen": 0.72,
            "eyeROpen": 0.94,
            "eyeLSmile": 0.41,
            "eyeRSmile": 0.05,
            "browLY": 0.27,
            "browRY": -0.09,
            "browLAngle": 0.36,
            "browRAngle": -0.31,
            "browLForm": 0.44,
            "browRForm": -0.28,
            "browLX": -0.19,
            "browRX": 0.17,
        })

        self.assertIsNotNone(summary)
        self.assertEqual(summary["mouth_form"], -0.33)
        self.assertFalse(summary["eye_sync"])
        self.assertEqual(summary["brow_l_form"], 0.44)
        self.assertEqual(summary["brow_r_form"], -0.28)
        self.assertEqual(summary["brow_l_x"], -0.19)
        self.assertEqual(summary["brow_r_x"], 0.17)
        self.assertIn("左右表情不對稱", summary["summary"])


if __name__ == "__main__":
    unittest.main()
