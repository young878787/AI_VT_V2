"""Memory migrations; credentials only come from environment variables."""

import os
import re
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
_x = context.get_x_argument(as_dictionary=True)
_provided_connection = context.config.attributes.get("connection")
_database = _x.get("database", "test")
if _database not in {"test", "production"}:
    raise RuntimeError("database 必須為 test 或 production")

if _database == "test":
    _url = os.getenv("MEMORY_TEST_DATABASE_URL", "").strip()
    _production_url = os.getenv("MEMORY_DATABASE_URL", "").strip()
    if not _url or not _production_url or make_url(_url).database == make_url(_production_url).database:
        raise RuntimeError("MEMORY_TEST_DATABASE_URL 必須設定且不同於正式 DB")
else:
    _url = os.getenv("MEMORY_DATABASE_URL", "").strip()
    if not _url:
        raise RuntimeError("MEMORY_DATABASE_URL 未設定")

if _provided_connection is not None:
    connected_database = _provided_connection.exec_driver_sql("SELECT current_database()").scalar_one()
    if connected_database != make_url(_url).database:
        raise RuntimeError("Alembic connection 的資料庫與指定 database 不符")

_schema = context.config.attributes.get("schema") or _x.get("schema") or os.getenv("MEMORY_DATABASE_SCHEMA", "").strip()
if not re.fullmatch(r"[a-z_][a-z0-9_]*", _schema):
    raise RuntimeError("Memory schema 名稱無效")
if _database == "test" and not re.fullmatch(r"test_[0-9a-f]{32}", _schema):
    raise RuntimeError("測試 schema 必須符合 test_<32 lowercase hex>")


def run_migrations_online() -> None:
    def migrate(connection) -> None:
        connection.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{_schema}"')
        connection.exec_driver_sql(f'SET LOCAL search_path TO "{_schema}", public')
        context.configure(connection=connection, version_table_schema=_schema)
        with context.begin_transaction():
            context.run_migrations()

    if _provided_connection is not None:
        migrate(_provided_connection)
    else:
        engine = create_engine(make_url(_url).set(drivername="postgresql+psycopg"))
        try:
            with engine.begin() as connection:
                migrate(connection)
        finally:
            engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Memory migration 不支援 offline SQL；請連接專用測試 DB")
run_migrations_online()
