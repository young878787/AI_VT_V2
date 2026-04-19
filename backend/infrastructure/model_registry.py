"""
Model Registry：model_registry.json 的讀寫、Live2D 模型目錄搜尋、內建模型清單。
"""
import os
import json

from core.config import MODEL_REGISTRY_PATH, RESOURCES_DIR

# 內建模型清單（與前端 LAppDefine.ts 同步）
BUILTIN_MODELS: list[dict] = [
    {
        "name": "Hiyori",
        "directory": "Hiyori",
        "fileName": "Hiyori.model3.json",
        "displayName": "Hiyori（日和）",
        "description": "溫柔可愛的少女",
    },
    {
        "name": "Haru",
        "directory": "Haru",
        "fileName": "Haru.model3.json",
        "displayName": "Haru（春）",
        "description": "元氣少女，活潑開朗",
    },
]


def load_model_registry() -> list[dict]:
    """讀取持久化的匯入模型清單（不含內建模型）。"""
    try:
        with open(MODEL_REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def save_model_registry(registry: list[dict]) -> None:
    """寫入持久化的匯入模型清單。"""
    with open(MODEL_REGISTRY_PATH, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


def find_model3_json(directory: str) -> tuple[str | None, str | None]:
    """
    在目錄（含子目錄）中尋找 .model3.json。
    回傳 (相對於 RESOURCES_DIR 的資料夾路徑, 檔名)。
    """
    for root, _, files in os.walk(directory):
        for name in files:
            if name.endswith(".model3.json"):
                rel_dir = os.path.relpath(root, RESOURCES_DIR).replace("\\", "/")
                return rel_dir, name
    return None, None
