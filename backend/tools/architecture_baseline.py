"""P0 隔離基線入口：固定 suite、禁外連、最新產物；不啟動服務或 migration。"""

import contextlib
from datetime import datetime
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "backend/log/architecture_baseline/latest"
SUITE = (
    "backend.tests.test_architecture_integration",
    "backend.tests.test_memory_agents",
    "backend.tests.test_memory_contract",
    "backend.tests.test_memory_retriever",
    "backend.tests.test_memory_embedding_client",
    "backend.tests.test_chat_test_modes",
    "backend.tests.test_jev_expression_mapper",
    "backend.tests.test_emotion_chat_ws",
)
SUPPLEMENT = (
    "backend.tests.test_architecture_baseline",
    "backend.tests.test_memory_source_policy",
    "backend.tests.test_memory_testset",
    "backend.tests.test_ai_request_params",
)


def isolated_environment(directory: str) -> dict[str, str]:
    """白名單重建環境，不能繼承正式 key、DB、proxy 或 live 開關。"""
    return {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
        "PYTHON_DOTENV_DISABLED": "1", "AI_VT_MEMORY_DIR": directory,
        "AI_VT_TEST_MODE": "false", "CHAT_SESSION_MAX_MESSAGES": "20",
        "ASR_ENABLED": "false", "TTS_ENABLED": "false",
        **{f"{role}_AI_{field}": value
           for role in ("CHAT", "JEV", "MEMORY", "EMBEDDING")
           for field, value in (("API_KEY", "baseline-placeholder"),
                                ("BASE_URL", "http://127.0.0.1:1/v1"),
                                ("MODEL", f"baseline-{role.lower()}"))},
        "EMBEDDING_AI_DIMENSION": "1024",
    }


@contextlib.contextmanager
def block_external_io():
    """Python socket 與 libpq 均封鎖；失敗即中止，不能降級為真實連線。"""
    import psycopg

    attempts = []

    def denied(*args, **kwargs):
        # 不保存參數，避免連線 URL／憑證進入證據。
        attempts.append("external_io_blocked")
        raise AssertionError("P0 baseline forbids network, DB and child processes")

    with contextlib.ExitStack() as stack:
        for target in ("socket.create_connection", "socket.socket.connect",
                       "socket.socket.connect_ex", "socket.socket.sendto",
                       "socket.getaddrinfo", "subprocess.Popen"):
            stack.enter_context(patch(target, side_effect=denied))
        stack.enter_context(patch.object(psycopg.Connection, "connect", side_effect=denied))
        stack.enter_context(patch.object(psycopg.AsyncConnection, "connect", side_effect=denied))
        yield attempts


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def configured_profile() -> dict:
    """只盤點本機非秘密契約；不載入到環境，也不把 endpoint／key／DB URL 寫入產物。"""
    from dotenv import dotenv_values
    from core.ai_request_params import provider_from_url
    from domain.memory_settings import MemorySettings, RETRIEVAL_INSTRUCTION
    from domain.memory_scope import MemoryScope
    from uuid import UUID

    values = dotenv_values(ROOT / ".env", interpolate=False)

    def value(name, default=""):
        return os.environ.get(name, values.get(name) or default)

    roles = {}
    for role in ("CHAT", "JEV", "MEMORY", "EMBEDDING"):
        url = value(f"{role}_AI_BASE_URL", "https://openrouter.ai/api/v1/systemone" if role == "JEV" else "")
        roles[role] = {"provider": provider_from_url(url) if url else "unconfigured",
                       "model": value(f"{role}_AI_MODEL", "jev-latest" if role == "JEV" else "").strip()}
    model = roles["EMBEDDING"]["model"]
    jina = model == "jinaai/jina-embeddings-v5-text-small-retrieval"
    dimension = int(value("EMBEDDING_AI_DIMENSION", "1024"))
    query_prefix = value("EMBEDDING_AI_QUERY_PREFIX", "Query: " if jina else RETRIEVAL_INSTRUCTION)
    document_prefix = value("EMBEDDING_AI_DOCUMENT_PREFIX", "Document: " if jina else "")
    # 使用現行 contract property，scope 與憑證都是 placeholder，不解析正式 URL。
    settings = MemorySettings("", MemoryScope(UUID(int=1), UUID(int=2), "baseline"),
                              "", "", "", "", "", model,
                              value("EMBEDDING_AI_SERVING_MODEL").strip() or model,
                              dimension, query_prefix, document_prefix)
    return {"source": "local_env_and_non_interpolated_dotenv_metadata_not_live_verification",
            "roles": roles, "embedding": {
                "model": model, "serving_model": settings.embedding_serving_model, "dimension": dimension,
                "query_prefix": query_prefix, "document_prefix": document_prefix,
                "normalization": "l2-v1", "contract": settings.embedding_contract,
            }}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, text=True,
                          capture_output=True).stdout.strip()


def snapshot() -> dict:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    files = [ROOT / "backend/requirements.txt", ROOT / "vtuber-web-app/package.json",
             ROOT / "vtuber-web-app/package-lock.json"]
    for directory in ("api", "core", "domain", "infrastructure", "services", "tests", "tools", "alembic"):
        files.extend(sorted((ROOT / "backend" / directory).rglob("*.py")))
    files.append(ROOT / "backend/main.py")
    return {
        "commit": git("rev-parse", "HEAD"),
        "worktree_status": git("status", "--short"),
        "python": sys.version.split()[0],
        "packages": dict(sorted((d.metadata["Name"], d.version)
                                for d in importlib.metadata.distributions())),
        "file_sha256": {str(p.relative_to(ROOT)): digest(p) for p in files if p.is_file()},
        "repository_schema_heads": ScriptDirectory.from_config(
            Config(str(ROOT / "backend/alembic.ini"))).get_heads(),
        "database_revision": "not_checked_no_database_access",
    }


def contracts() -> dict:
    from core import config
    from domain.agent_a_prompts import build_agent_a_prompt
    from domain.emotion_state import NEUTRAL_EMOTION_STATE
    from domain.jev_questions import build_jev_questions
    from domain.memory_decisions import agent_tools
    from domain.memory_settings import load_memory_settings
    from services.memory_llm import PROMPT
    from tools.memory_testset import CORE_PATH, validate_cases

    def fingerprint(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode()).hexdigest()

    settings = load_memory_settings({
        **os.environ, "MEMORY_DATABASE_URL": "postgresql://127.0.0.1:1/baseline",
        "MEMORY_DEFAULT_USER_ID": "00000000-0000-0000-0000-000000000001",
        "MEMORY_DEFAULT_CHARACTER_ID": "00000000-0000-0000-0000-000000000002",
        "MEMORY_DATABASE_SCHEMA": "baseline",
    })
    core = validate_cases(json.loads(CORE_PATH.read_text(encoding="utf-8")), source="Gold", count=23)
    return {
        "configuration_source": "synthetic_isolated_environment_not_production",
        "roles": {role: {"provider": "custom", "model": os.environ[f"{role}_AI_MODEL"]}
                  for role in ("CHAT", "JEV", "MEMORY", "EMBEDDING")},
        "prompt_sha256": {
            "chat_empty_context": fingerprint(build_agent_a_prompt({}, "", NEUTRAL_EMOTION_STATE, "Rushia")),
            "jev_questions": fingerprint(build_jev_questions()),
            "memory_system": fingerprint(PROMPT),
            "memory_tools_observe": fingerprint(agent_tools(False)),
            "memory_tools_forget": fingerprint(agent_tools(True)),
        },
        "embedding": {"model": settings.embedding_model, "serving_model": settings.embedding_serving_model,
                      "dimension": settings.embedding_dimension, "query_prefix": settings.embedding_query_prefix,
                      "document_prefix": settings.embedding_document_prefix, "normalization": "l2-v1",
                      "contract": settings.embedding_contract},
        "core_cases": {"count": len(core), "manifest_sha256": digest(CORE_PATH),
                       "extension_ids": [case["case_id"] for case in core[-3:]],
                       "generated_cases_executed": 0, "semantic_review": "not_executed"},
        "context_token_budget": config.CHAT_CONTEXT_TOKEN_BUDGET,
    }


def run_suite(modules: tuple[str, ...], stream) -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {"modules": list(modules), "tests": result.testsRun,
            "failures": [test.id() for test, _ in result.failures],
            "errors": [test.id() for test, _ in result.errors],
            "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
            "expected_failures": [test.id() for test, _ in result.expectedFailures],
            "unexpected_successes": [test.id() for test in result.unexpectedSuccesses],
            "status": "passed" if result.wasSuccessful() and not result.skipped
                      and not result.expectedFailures else "failed"}


def main() -> int:
    if len(sys.argv) != 1:
        raise SystemExit("用法：backend/.venv/bin/python backend/tools/architecture_baseline.py")
    sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {"status": "failed", "started_at": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
              "not_executed": [
        "DB integration / live models / ASR / TTS / browser / deployment / backup restore",
        "F01 public access authorization (separate runtime change)",
    ]}
    output = io.StringIO()
    try:
        sys.path.insert(0, str(ROOT / "backend"))
        report["configured_profile"] = configured_profile()
    except Exception as exc:
        report["configured_profile"] = {"status": "unavailable", "error_type": type(exc).__name__}
    with tempfile.TemporaryDirectory(prefix="ai-vt-baseline-") as directory, \
            patch.dict(os.environ, isolated_environment(directory), clear=True), \
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            report["snapshot"] = snapshot()
            with block_external_io() as attempts:
                from core import prompt_logger
                # 一般模式 reset_log 也必須只寫暫存檔。
                with patch.object(prompt_logger, "_LOG_DIR", Path(directory)), \
                        patch.object(prompt_logger, "_LOG_FILE", Path(directory) / "prompt.log"):
                    report["contracts"] = contracts()
                    from services import chat_service
                    if chat_service._encoding is None:
                        raise RuntimeError("cl100k_base cache unavailable; tokenizer fallback is not a reproducible baseline")
                    report["baseline_suite"] = run_suite(SUITE, output)
                    report["supplement_suite"] = run_suite(SUPPLEMENT, output)
                    from tools.architecture_baseline_probes import run_probes
                    report["known_gaps"] = run_probes()
                report["isolation"] = {"dotenv_disabled": True, "inherited_environment_removed": True,
                                       "temporary_state_cleaned_on_exit": True,
                                       "blocked_io_attempts": len(attempts)}
            if "error_type" not in report["configured_profile"] and not attempts \
                    and all(report[key]["status"] == "passed" for key in ("baseline_suite", "supplement_suite")) \
                    and all(row["status"] in {"reproduced", "not_reproduced"} for row in report["known_gaps"]):
                report["status"] = "baseline_established"
        except Exception as exc:
            report["error_type"] = type(exc).__name__
            output.write(f"\nBaseline aborted: {type(exc).__name__}\n")
    # 固定路徑覆寫，不追加歷史；這不是 runtime 修正成功標記。
    (OUTPUT / "baseline.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT / "tests.log").write_text(output.getvalue(), encoding="utf-8")
    rows = ["# P0 隔離基線", "", f"狀態：`{report['status']}`。缺口重現不代表已修正。", "",
            "環境、套件版本、程式／prompt 指紋與 suite 結果見 `baseline.json`；執行紀錄見 `tests.log`。", ""]
    for key in ("baseline_suite", "supplement_suite"):
        if key in report:
            suite = report[key]
            rows.append(f"- {key}: {suite['tests']} tests，{suite['status']}，"
                        f"failures={len(suite['failures'])}、errors={len(suite['errors'])}、skips={len(suite['skipped'])}")
    rows.extend(["", "| 檢查 | 結果 | 實際觀察 |", "|---|---|---|"])
    for row in report.get("known_gaps", []):
        rows.append(f"| {row['id']} | {row['status']} | {json.dumps(row.get('observed', {}), ensure_ascii=False)} |")
    rows.extend(["", "未執行：", "", *[f"- {item}" for item in report["not_executed"]]])
    (OUTPUT / "baseline_report.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    print(f"P0 baseline: {report['status']} ({OUTPUT / 'baseline_report.md'})")
    return 0 if report["status"] == "baseline_established" else 1


if __name__ == "__main__":
    raise SystemExit(main())
