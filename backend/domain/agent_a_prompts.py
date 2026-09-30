"""露西亞純對話 Prompt；情緒決策由 JEV Emotion 提供。"""

from domain.emotion_state import EMOTION_FIELDS, PERSONALITY
from domain.tools.schema_loader import DEFAULT_MODEL


def build_agent_a_prompt(
    user_profile: dict,
    memory_notes: str,
    emotion_state: dict,
    model_name: str = DEFAULT_MODEL,
) -> str:
    traits = "、".join(PERSONALITY["traits"])
    scores = "\n".join(f"- {field}: {emotion_state[field]:.2f}" for field in EMOTION_FIELDS)
    return f"""你是虛擬主播{PERSONALITY['name']}。固定性格：{traits}。
你是使用者親近的聊天夥伴。以自然口語回應，平常 1～4 句；需要詳細解答時可以多說。
你說出的文字會原封不動由 TTS 唸出，不要加入括號旁白、舞台指示、動作標記或工具呼叫。
Live2D 表情由獨立系統控制。只輸出使用者會聽見的純文字回覆，不輸出 JSON、XML 或任何狀態更新。

使用者資料：
{_build_profile_section(user_profile, model_name)}

共同回憶：
{_build_memory_section(memory_notes)}

本輪 JEV 情緒判斷（六個分數可以同時偏高；依性格自然表現，不逐項唸出）：
{scores}"""


def _build_profile_section(profile: dict, model_name: str = DEFAULT_MODEL) -> str:
    parts = []
    if profile.get("core_traits"):
        parts.append(f"- 特徵：{', '.join(profile['core_traits'])}")
    if profile.get("communication_style"):
        parts.append(f"- 溝通風格：{profile['communication_style']}")
    if profile.get("dislikes"):
        parts.append(f"- 討厭：{', '.join(profile['dislikes'])}")
    if profile.get("recent_interests"):
        parts.append(f"- 最近感興趣：{', '.join(profile['recent_interests'])}")
    if profile.get("custom_notes"):
        parts.extend(f"- {note}" for note in profile["custom_notes"])
    return "\n".join(parts) if parts else "還不太了解使用者。"


def _build_memory_section(memory_notes: str) -> str:
    return memory_notes if memory_notes.strip() else "還沒有共同回憶。"
