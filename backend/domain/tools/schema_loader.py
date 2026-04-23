"""
模型 Schema 載入器：依 model_name 動態載入 tools/{model_name}.json。
- 若檔案不存在，自動建立空白 skeleton 並以 fallback 補齊缺少的區段。
- 記憶體快取（_cache），同一 model_name 只讀一次磁碟。
- 預設模型（DEFAULT_MODEL）的 schema 作為所有缺少區段的 fallback。
"""
import json
import os

_TOOLS_DIR = os.path.dirname(__file__)
DEFAULT_MODEL = "Hiyori"

_cache: dict[str, dict] = {}

_EMPTY_SKELETON: dict = {
    "version": "1.0.0",
}


def _load_default() -> dict:
    """載入預設模型（Hiyori）schema，快取後重用。"""
    if DEFAULT_MODEL not in _cache:
        path = os.path.join(_TOOLS_DIR, f"{DEFAULT_MODEL}.json")
        with open(path, "r", encoding="utf-8") as f:
            _cache[DEFAULT_MODEL] = json.load(f)
    return _cache[DEFAULT_MODEL]


def load_schema(model_name: str) -> dict:
    """
    載入指定模型的 schema。
    - 若 {model_name}.json 不存在，自動建立空白 skeleton。
    - 任何缺少的頂層或子區段，回退至預設模型補齊。
    - 結果快取於 _cache，重啟前不重讀磁碟。
    """
    if model_name in _cache:
        return _cache[model_name]

    path = os.path.join(_TOOLS_DIR, f"{model_name}.json")

    if not os.path.exists(path):
        skeleton = {**_EMPTY_SKELETON, "description": f"Auto-generated schema for {model_name}."}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(skeleton, f, ensure_ascii=False, indent=2)
        print(f"[SchemaLoader] 自動建立空白 schema: {model_name}.json")

    with open(path, "r", encoding="utf-8") as f:
        schema = json.load(f)

    default = _load_default()

    for top_key in ("openai_tools", "ui_config", "prompt_config"):
        if top_key not in schema or not schema[top_key]:
            schema[top_key] = default.get(top_key, {})
        else:
            for sub_key, sub_val in default.get(top_key, {}).items():
                if sub_key not in schema[top_key] or not schema[top_key][sub_key]:
                    schema[top_key][sub_key] = sub_val

    _cache[model_name] = schema
    return schema


def invalidate_cache(model_name: str | None = None) -> None:
    """使快取失效（修改 JSON 後呼叫以重新載入）。None 表示清除全部。"""
    if model_name:
        _cache.pop(model_name, None)
    else:
        _cache.clear()
