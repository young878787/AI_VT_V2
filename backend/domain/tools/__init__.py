"""Live2D function calling 工具定義。"""
from domain.tools.schema_loader import load_schema, DEFAULT_MODEL

_default_schema = load_schema(DEFAULT_MODEL)

# ============================================================
# 預設模型的 Live2D 工具清單
# ============================================================
live2d_tools: list[dict] = _default_schema["openai_tools"]["live2d"]
tools: list[dict] = live2d_tools


# ============================================================
# 動態工具取得（依 model_name 載入對應 schema）
# ============================================================
def get_live2d_tools(model_name: str) -> list[dict]:
    """取得指定模型的 Live2D function calling 工具清單。"""
    return load_schema(model_name)["openai_tools"]["live2d"]
