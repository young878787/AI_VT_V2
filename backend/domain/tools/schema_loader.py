"""
模型 Schema 載入器：依 model_name 動態載入 tools/{model_name}.json。
- 若檔案不存在，直接回退至預設模型 schema，不建立新檔案。
- 記憶體快取（_cache），同一 model_name 只讀一次磁碟。
- 預設模型（DEFAULT_MODEL）的 schema 作為所有缺少區段的 fallback。
"""
import json
import os
import re

_TOOLS_DIR = os.path.dirname(__file__)
DEFAULT_MODEL = "Hiyori"
_SAFE_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

_cache: dict[str, dict] = {}


def _merge_schema_value(custom_value, default_value):
    if not custom_value:
        return default_value

    if isinstance(custom_value, dict) and isinstance(default_value, dict):
        merged = dict(custom_value)
        for key, default_subvalue in default_value.items():
            merged[key] = _merge_schema_value(merged.get(key), default_subvalue)
        return merged

    if isinstance(custom_value, list) and isinstance(default_value, list):
        if _is_named_tool_list(custom_value) and _is_named_tool_list(default_value):
            return _merge_named_tool_lists(custom_value, default_value)
        return custom_value

    return custom_value


def _is_named_tool_list(value: object) -> bool:
    if not isinstance(value, list):
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("function"), dict)
        and isinstance(item["function"].get("name"), str)
        for item in value
    )


def _merge_named_tool_lists(custom_tools: list[dict], default_tools: list[dict]) -> list[dict]:
    custom_by_name = {
        tool["function"]["name"]: tool
        for tool in custom_tools
    }
    merged_tools: list[dict] = []
    seen_names: set[str] = set()

    for default_tool in default_tools:
        tool_name = default_tool["function"]["name"]
        seen_names.add(tool_name)
        custom_tool = custom_by_name.get(tool_name)
        if custom_tool is None:
            merged_tools.append(default_tool)
        else:
            merged_tools.append(_merge_schema_value(custom_tool, default_tool))

    for custom_tool in custom_tools:
        tool_name = custom_tool["function"]["name"]
        if tool_name not in seen_names:
            merged_tools.append(custom_tool)

    return merged_tools

def _load_default() -> dict:
    """載入預設模型（Hiyori）schema，快取後重用。"""
    if DEFAULT_MODEL not in _cache:
        path = os.path.join(_TOOLS_DIR, f"{DEFAULT_MODEL}.json")
        with open(path, "r", encoding="utf-8") as f:
            _cache[DEFAULT_MODEL] = json.load(f)
    return _cache[DEFAULT_MODEL]


def normalize_model_name(model_name: object) -> str:
    if not isinstance(model_name, str):
        return DEFAULT_MODEL

    cleaned = model_name.strip()
    if not cleaned or not _SAFE_MODEL_NAME_RE.fullmatch(cleaned):
        return DEFAULT_MODEL

    return cleaned


def load_schema(model_name: str) -> dict:
    """
    載入指定模型的 schema。
    - 若 {model_name}.json 不存在，直接回退預設模型。
    - 任何缺少的頂層或子區段，回退至預設模型補齊。
    - 結果快取於 _cache，重啟前不重讀磁碟。
    """
    model_name = normalize_model_name(model_name)
    if model_name in _cache:
        return _cache[model_name]

    path = os.path.join(_TOOLS_DIR, f"{model_name}.json")
    default = _load_default()

    if not os.path.exists(path):
        _cache[model_name] = default
        return default

    with open(path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    for top_key in ("openai_tools", "ui_config", "prompt_config"):
        schema[top_key] = _merge_schema_value(schema.get(top_key), default.get(top_key, {}))

    _cache[model_name] = schema
    return schema


def invalidate_cache(model_name: str | None = None) -> None:
    """使快取失效（修改 JSON 後呼叫以重新載入）。None 表示清除全部。"""
    if model_name:
        _cache.pop(model_name, None)
    else:
        _cache.clear()
