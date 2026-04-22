"""
AI Tool 定義：提供給 LLM 的 function calling 工具清單。
資料來源：tools_schema.json（單一來源）。
此檔案讀取 JSON 並導出向後相容的變數名稱。
"""

import json
import os

# 載入統一 Schema（與本檔案同目錄）
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "tools_schema.json")
with open(_SCHEMA_PATH, "r", encoding="utf-8") as _f:
    _SCHEMA = json.load(_f)

# ============================================================
# Live2D 表情控制工具（Agent B-1）
# ============================================================
# 從 Schema 取出 OpenAI function calling 格式
live2d_tools: list[dict] = _SCHEMA["openai_tools"]["live2d"]

# ============================================================
# 記憶管理工具（Agent B-2）
# ============================================================
memory_tools: list[dict] = _SCHEMA["openai_tools"]["memory"]

# ============================================================
# 向後相容：合併清單（如有其他地方仍引用 tools）
# ============================================================
tools: list[dict] = live2d_tools + memory_tools

# 如需在其他 Python 模組存取 Schema 內的提示詞設定，可直接 import schema：
# from domain.tools import _SCHEMA
# （前綴底線表示內部使用，但開放供 domain 層讀取）
