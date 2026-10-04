"""明確啟用的真實 JEV、接收、圖書、embedding 與 DB 評估；每次清理隔離 schema。"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT.parent))
sys.path.insert(0, str(BACKEND_ROOT))
from backend.tests import test_memory_database_integration as database_tests
from domain.memory_settings import load_memory_settings
from domain.memory_routing import route_memory
from domain.jev_questions import build_memory_questions
from infrastructure.typesafe_client import call_jev
from infrastructure.memory_embedding_client import MemoryEmbeddingClient
from services.memory_llm import MemoryLLM
from services.memory_worker import MemoryWorker
from services.memory_retriever import MemoryRetriever
from domain.agent_a_prompts import build_agent_a_prompt
from domain.emotion_state import EMOTION_FIELDS
from tools.chat_test_cli import create_run_dir, timestamp

MEMORY_AGENT_RUNS_DIR = BACKEND_ROOT / "log" / "memory_agent_runs"


def _summary_cell(value, limit=240):
    text = "-" if value is None or value == "" else str(value)
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _write_artifacts(run_dir, report, manifest):
    output = run_dir / "memory_agents.json"
    summary = run_dir / "memory_agents_report.md"
    manifest_path = run_dir / "run.json"

    def atomic_write(path, content):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    rows = [
        "# Memory agent 六情境補充評估", "",
        f"- 狀態：**{manifest['status']}**；完成 {len(report['cases'])} / {manifest['planned_cases']} 案",
        f"- 開始：{manifest['started_at']}；更新：{manifest['updated_at']}",
        f"- schema cleanup：`{report['schema_cleaned']}`",
        "- 完整 agent diagnostics、候選、JEV 與 fresh recall：`memory_agents.json`。",
        "- 這是獨立記憶 agent 評估，不取代完整 Chat `/ws/chat` 測試，也不切換 Chat `latest`。", "",
        "| Case | Input | Route／Job | Fresh recall | Prompt 注入 | Error |",
        "|---|---|---|---|---|---|",
    ]
    by_case = {item["case"]: item for item in report["cases"]}
    for name in manifest["case_names"]:
        item = by_case.get(name)
        if item is None:
            rows.append(f"| {_summary_cell(name)} | - | - | - | - | 尚未執行 |")
            continue
        fresh = item.get("fresh_recall") or {}
        rows.append("| " + " | ".join([
            _summary_cell(item.get("case")), _summary_cell(item.get("input")),
            _summary_cell(f"{item.get('route') or '-'} / {item.get('status') or '-'}"),
            _summary_cell(fresh.get("relevant")),
            _summary_cell(item.get("prompt_contains_recall")),
            _summary_cell(item.get("error")),
        ]) + " |")
    atomic_write(output, json.dumps(report, ensure_ascii=False, indent=2))
    atomic_write(summary, "\n".join(rows) + "\n")
    atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))

async def evaluate():
    run_dir = create_run_dir(MEMORY_AGENT_RUNS_DIR)
    output = run_dir / "memory_agents.json"
    print(f"補充記憶測試結果：{output}", flush=True)
    test = database_tests.MemoryDatabaseIntegrationTests()
    clients = []
    report = {"cases": [], "schema_cleaned": False}
    case_names = ["noise", "mixed", "recall", "correct", "forget", "no_store"]
    manifest = {
        "test_type": "memory_agent_live_eval",
        "status": "running",
        "started_at": timestamp(),
        "updated_at": timestamp(),
        "planned_cases": len(case_names),
        "case_names": case_names,
        "schema_cleaned": False,
        "files": {"results": "memory_agents.json", "summary": "memory_agents_report.md"},
    }
    _write_artifacts(run_dir, report, manifest)
    setup_complete = False
    try:
        await test.asyncSetUp()
        setup_complete = True
        settings = load_memory_settings({**os.environ, 'AI_VT_TEST_MODE': 'true', 'MEMORY_DATABASE_SCHEMA': test.scope.schema_name})
        embedding, agent = MemoryEmbeddingClient(settings), MemoryLLM(settings)
        clients = [embedding.client, agent.client]
        test.repo.embedding_model, test.repo.embedding_contract = settings.embedding_model, settings.embedding_contract
        test.manager.embedding_model, test.manager.embedding_contract = settings.embedding_model, settings.embedding_contract
        test.manager.model = settings.memory_model
        worker = MemoryWorker(test.repo, embedding, agent, test.manager)
        cases = [
            ('noise', '哈哈哈', []),
            ('mixed', '左邊有人！對了，我平常最喜歡喝拿鐵，幫我記住這個飲料偏好。', []),
            ('recall', '你記得我平常最喜歡喝哪一種咖啡嗎？', []),
            ('correct', '更正我的咖啡偏好：我現在不喝拿鐵了，改成只喝美式咖啡。', []),
            ('forget', '忘記我的咖啡偏好，包含以前的版本。', []),
            ('no_store', '不要記住這件事，我的測試密語是合成代碼青松。', []),
        ]
        for index, (name, text, history) in enumerate(cases):
            event = await test.repo.accept(f'fresh-{index}', 'turn')
            answers = await call_jev({'current_user_input': text, 'recent_dialogue': history}, build_memory_questions())
            routing = route_memory(text, answers)
            await test.repo.route(event, routing, text, history)
            async with asyncio.timeout(330):
                while True:
                    await worker.process_one()
                    status = (await test._query('SELECT status FROM memory_jobs WHERE id = %s', (event,)))[0][0]
                    if status not in {"pending", "running", "retry"}:
                        break
                    await asyncio.sleep(1)
            row = (await test._query('SELECT route, status, error, agent_diagnostics, missing_context FROM memory_jobs WHERE id = %s', (event,)))[0]
            profile, relevant = await MemoryRetriever(test.repo, embedding).retrieve('你記得我的拿鐵與美式咖啡偏好嗎？', event_id=event)
            prompt = build_agent_a_prompt(profile, relevant, {field: 0.0 for field in EMOTION_FIELDS})
            result = {'prompt_contains_recall': bool(relevant) and relevant in prompt, 'case': name, 'input': text,
                      'jev': answers, 'route': row[0], 'status': row[1], 'error': row[2],
                      'agents': row[3], 'missing_context': row[4], 'fresh_recall': {'profile': profile, 'relevant': relevant}}
            report['cases'].append(result)
            manifest["updated_at"] = timestamp()
            _write_artifacts(run_dir, report, manifest)
            print(json.dumps({key: result[key] for key in ('case','route','status','error','fresh_recall')}, ensure_ascii=False), flush=True)
        manifest["status"] = "completed"
    except Exception:
        manifest["status"] = "failed"
        raise
    finally:
        for client in clients:
            await client.close()
        if setup_complete:
            await test.asyncTearDown()
            report['schema_cleaned'] = True
        manifest["schema_cleaned"] = report["schema_cleaned"]
        manifest["updated_at"] = timestamp()
        _write_artifacts(run_dir, report, manifest)

    return report


@unittest.skipUnless(os.getenv("MEMORY_LIVE_EVAL") == "1" and database_tests.VALID_TEST_DATABASE,
                     "真實模型評估需 MEMORY_LIVE_EVAL=1 與隔離測試 DB")
class MemoryLiveEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_single_agent_and_fresh_recall(self):
        report = await evaluate()
        self.assertTrue(report["schema_cleaned"])
        cases = {row["case"]: row for row in report["cases"]}
        for name in ("noise", "recall", "no_store"):
            self.assertEqual(cases[name]["status"], "ignored", cases[name])
        for name in ("mixed", "correct", "forget"):
            self.assertEqual(cases[name]["status"], "done", cases[name])
        self.assertEqual(cases["noise"]["jev"]["memory_noise"]["choice"], "noise")
        self.assertIn("拿鐵", cases["recall"]["fresh_recall"]["relevant"])
        self.assertTrue(cases["recall"]["prompt_contains_recall"])
        self.assertIn("美式", cases["correct"]["fresh_recall"]["relevant"])
        self.assertEqual(cases["forget"]["fresh_recall"], {"profile": {}, "relevant": ""})


class MemoryLiveArtifactTests(unittest.TestCase):
    def test_summary_and_snapshot_are_separate_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = pathlib.Path(directory)
            report = {"cases": [{"case": "noise", "input": "哈哈哈", "route": "none",
                                  "status": "ignored", "error": None,
                                  "fresh_recall": {"relevant": ""},
                                  "prompt_contains_recall": False}],
                      "schema_cleaned": True}
            manifest = {"status": "completed", "started_at": "now", "updated_at": "now",
                        "planned_cases": 1, "case_names": ["noise"], "schema_cleaned": True}
            _write_artifacts(run_dir, report, manifest)
            self.assertTrue((run_dir / "memory_agents.json").exists())
            self.assertTrue((run_dir / "memory_agents_report.md").exists())
            self.assertTrue((run_dir / "run.json").exists())
            self.assertNotIn("```json", (run_dir / "memory_agents_report.md").read_text())


if __name__ == "__main__":
    unittest.main()
