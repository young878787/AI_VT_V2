"""明確啟用的真實 JEV、接收、圖書、embedding 與 DB 評估；每次清理隔離 schema。"""
import json
import os
import pathlib
import sys
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
from services.memory_intake import MemoryIntake
from services.memory_llm import MemoryLLM
from services.memory_worker import MemoryWorker
from services.memory_retriever import MemoryRetriever
from domain.agent_a_prompts import build_agent_a_prompt
from domain.emotion_state import EMOTION_FIELDS

async def evaluate():
    test = database_tests.MemoryDatabaseIntegrationTests()
    await test.asyncSetUp()
    clients = []
    report = {"cases": [], "schema_cleaned": False}
    output = BACKEND_ROOT / "log" / "memory_agents_latest.json"
    output.parent.mkdir(exist_ok=True)
    try:
        settings = load_memory_settings({**os.environ, 'AI_VT_TEST_MODE': 'true', 'MEMORY_DATABASE_SCHEMA': test.scope.schema_name})
        embedding, intake, librarian = MemoryEmbeddingClient(settings), MemoryIntake(settings), MemoryLLM(settings)
        clients = [embedding.client, intake.client, librarian.client]
        test.repo.embedding_model, test.repo.embedding_contract = settings.embedding_model, settings.embedding_contract
        test.manager.embedding_model, test.manager.embedding_contract = settings.embedding_model, settings.embedding_contract
        test.manager.model = settings.memory_model
        worker = MemoryWorker(test.repo, embedding, librarian, test.manager, intake)
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
            for _ in range(7):
                if not await worker.process_one():
                    break
            row = (await test._query('SELECT route, status, error, agent_diagnostics, reviewed_candidates, missing_context FROM memory_jobs WHERE id = %s', (event,)))[0]
            profile, relevant = await MemoryRetriever(test.repo, embedding).retrieve('你記得我的拿鐵與美式咖啡偏好嗎？', event_id=event)
            prompt = build_agent_a_prompt(profile, relevant, {field: 0.0 for field in EMOTION_FIELDS})
            result = {'prompt_contains_recall': bool(relevant) and relevant in prompt, 'case': name, 'jev': answers, 'route': row[0], 'status': row[1], 'error': row[2],
                      'agents': row[3], 'candidates': row[4], 'missing_context': row[5], 'fresh_recall': {'profile': profile, 'relevant': relevant}}
            report['cases'].append(result)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({key: result[key] for key in ('case','route','status','error','fresh_recall')}, ensure_ascii=False), flush=True)
    finally:
        for client in clients:
            await client.close()
        await test.asyncTearDown()
        report['schema_cleaned'] = True
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    return report


@unittest.skipUnless(os.getenv("MEMORY_LIVE_EVAL") == "1" and database_tests.VALID_TEST_DATABASE,
                     "真實模型評估需 MEMORY_LIVE_EVAL=1 與隔離測試 DB")
class MemoryLiveEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_intake_librarian_and_fresh_recall(self):
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


if __name__ == "__main__":
    unittest.main()
