"""
AI Tool 定義：提供給 LLM 的 function calling 工具清單。
資料來源：tools/{model_name}.json（每個模型獨立設定）。
向後相容：module-level globals live2d_tools / memory_tools 使用預設模型（Hiyori）。
"""
from domain.tools.schema_loader import load_schema, DEFAULT_MODEL

_default_schema = load_schema(DEFAULT_MODEL)

# ============================================================
# 向後相容：預設模型的工具清單（供舊有程式碼直接 import）
# ============================================================
live2d_tools: list[dict] = _default_schema["openai_tools"]["live2d"]
memory_tools: list[dict] = _default_schema["openai_tools"]["memory"]
tools: list[dict] = live2d_tools + memory_tools


# ============================================================
# 動態工具取得（依 model_name 載入對應 schema）
# ============================================================
def get_live2d_tools(model_name: str) -> list[dict]:
    """取得指定模型的 Live2D function calling 工具清單。"""
    return load_schema(model_name)["openai_tools"]["live2d"]


def get_memory_tools() -> list[dict]:
    """Memory tools 與模型無關，永遠使用預設模型。"""
    return memory_tools
