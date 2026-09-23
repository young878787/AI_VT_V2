"""只摘要已採納的長期記憶；來源紀錄保持原樣。"""
import hashlib
import json
import os

from core.config import MEMORY_DIR, MEMORY_MODEL_NAME, MEMORY_PROVIDER
from infrastructure.ai_client import chat_create_with_fallback, no_thinking_extra_body
from infrastructure.memory_records import load_records
from infrastructure.memory_store import _atomic_write, _write_lock

SUMMARY_PATH = os.path.join(MEMORY_DIR, "long_term_summary.json")


async def consolidate_memory(expected_epoch: int | None = None, epoch_reader=None) -> bool:
    records = [item for item in load_records() if item["status"] == "active"][-20:]
    if len(records) < 3:
        return False
    source_ids = [item["id"] for item in records]
    fingerprint = hashlib.sha256("\n".join(source_ids).encode()).hexdigest()
    try:
        with open(SUMMARY_PATH, "r", encoding="utf-8") as file:
            existing = json.load(file)
        if existing.get("fingerprint") == fingerprint:
            return False
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    source = "\n".join(f"- [{item['id']}] {item['text'][:200]}" for item in records)
    response = await chat_create_with_fallback(
        role="memory", model=MEMORY_MODEL_NAME,
        messages=[
            {"role": "system", "content": "請將已驗證的長期記憶濃縮成繁體中文短摘要。只使用提供的事實，不推測、不新增事實；重複資訊合併。最多 250 字。"},
            {"role": "user", "content": source},
        ],
        temperature=0.2, max_tokens=350,
        extra_body=no_thinking_extra_body(MEMORY_PROVIDER),
    )
    content = response.choices[0].message.content if response.choices else ""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("長期記憶摘要為空")
    with _write_lock:
        if expected_epoch is not None and epoch_reader() != expected_epoch:
            return False
        _atomic_write(SUMMARY_PATH, json.dumps({
            "fingerprint": fingerprint, "source_ids": source_ids,
            "summary": content.strip()[:1000],
        }, ensure_ascii=False, indent=2))
    return True
