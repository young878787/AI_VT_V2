"""舊記憶一次性匯入；預設只輸出不含原文的 dry-run 摘要。"""

import argparse
import asyncio
import os
import re
import shutil
import sys
from pathlib import Path
from sqlalchemy.engine import make_url

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.memory_scope import MemoryScope
from domain.memory_settings import load_memory_settings
from infrastructure.memory_database import check_schema, make_pool
from infrastructure.memory_embedding_client import MemoryEmbeddingClient
from services.memory_db_manager import MemoryDBManager
from services.memory_import import import_summary, read_legacy_entries


async def apply_import(entries, memory_dir: Path, database: str, schema: str | None) -> int:
    settings = load_memory_settings()
    if database == "test":
        database_url = os.getenv("MEMORY_TEST_DATABASE_URL", "").strip()
        if not database_url or make_url(database_url).database == make_url(settings.database_url).database:
            raise RuntimeError("MEMORY_TEST_DATABASE_URL 必須設定且不同於正式 DB")
        if not schema or not re.fullmatch(r"test_[0-9a-f]{32}", schema):
            raise RuntimeError("測試 schema 必須是 test_<32 lowercase hex>")
    else:
        database_url = settings.database_url
        schema = schema or settings.scope.schema_name
    scope = MemoryScope(settings.scope.user_id, settings.scope.character_id, schema)
    pool = await make_pool(database_url)
    embedding = MemoryEmbeddingClient(settings)
    try:
        await check_schema(pool, scope)
        for filename in ("user_profile.json", "memory_records.json", "memory.md"):
            source = memory_dir / filename
            backup = memory_dir / f"{filename}.pre-postgres.bak"
            if source.exists() and not backup.exists():
                with backup.open("xb") as destination, source.open("rb") as origin:
                    shutil.copyfileobj(origin, destination)
        vectors = {entry.id: await embedding.embed(entry.canonical_text) for entry in entries}
        manager = MemoryDBManager(pool, scope, settings.memory_model)
        return await manager.import_legacy(entries, vectors)
    finally:
        await embedding.client.close()
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Legacy memory import (dry-run by default)")
    parser.add_argument("--memory-dir", type=Path, default=BACKEND_ROOT / "memory")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database", choices=("test", "production"), default="test")
    parser.add_argument("--schema")
    args = parser.parse_args()
    entries = read_legacy_entries(args.memory_dir)
    print(import_summary(entries))
    if args.apply:
        count = asyncio.run(apply_import(entries, args.memory_dir, args.database, args.schema))
        print({"imported": count, "unchanged": len(entries) - count})


if __name__ == "__main__":
    main()
