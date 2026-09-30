import pathlib
import sys
import unittest
from uuid import UUID, uuid4

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.memory_decisions import validate_decisions
from domain.memory_routing import route_memory
from domain.memory_scope import MemoryScope, conversation_id, message_id
from domain.memory_embedding import format_embedding_input, normalize_embedding, query_document
from domain.memory_settings import load_memory_settings
from domain.jev_questions import build_memory_questions


def answers(route="none", confidence=0.7, importance=0, explicit=0):
    return {
        "memory_route": {"choice": route, "confidence": confidence},
        "memory_type": {"choice": "project", "confidence": 0.7},
        "explicit_memory": {"noul": explicit},
        "importance": {"score": importance, "confidence": 0.7},
    }


class MemoryContractTests(unittest.TestCase):
    def test_single_jev_contract_has_bounded_evidence_instructions(self):
        questions = build_memory_questions()
        self.assertEqual(set(questions), {"memory_route", "memory_type", "explicit_memory", "importance"})
        for question in questions.values():
            self.assertIn("current_user_input 與 recent_dialogue", question["instructions"])
            self.assertIn("relevant_memory 不得單獨", question["instructions"])

    def test_route_boundaries_and_invalid_output(self):
        self.assertEqual(route_memory("hello", answers("process", 0.65)).route, "process")
        self.assertEqual(route_memory("hello", answers("process", 0.649, 2.5)).route, "buffer")
        self.assertEqual(route_memory("hello", answers("buffer", 0.1)).route, "buffer")
        self.assertEqual(route_memory("hello", answers(explicit=0.80)).route, "process")
        self.assertEqual(route_memory("hello", answers(importance=2.499)).route, "none")
        for invalid in (None, {}, answers(confidence=True), answers(importance=float("nan")), answers(route="bad")):
            self.assertEqual(route_memory("hello", invalid).route, "none")
            self.assertEqual(route_memory("請記住我喜歡茶", invalid).route, "process")

    def test_scope_and_stable_ids(self):
        scope = MemoryScope(uuid4(), uuid4(), "test_" + uuid4().hex)
        self.assertTrue(scope.schema_name.startswith("test_"))
        self.assertEqual(conversation_id("session"), conversation_id("session"))
        self.assertEqual(message_id("session", "turn"), message_id("session", "turn"))
        self.assertNotEqual(message_id("session", "turn"), message_id("session2", "turn"))
        with self.assertRaises(ValueError):
            MemoryScope(uuid4(), uuid4(), "public;drop")

    def test_decision_target_and_forget_gate(self):
        target = uuid4()
        decision = {"action": "FORGET", "target_memory_ids": [str(target)], "reason": "user_request"}
        with self.assertRaises(ValueError):
            validate_decisions({"decisions": [decision]}, {target})
        self.assertEqual(validate_decisions({"decisions": [decision]}, {target}, True), [decision])
        with self.assertRaises(ValueError):
            validate_decisions({"decisions": [decision]}, {UUID(int=0)}, True)
        with self.assertRaises(ValueError):
            validate_decisions({"decisions": [{**decision, "target_memory_ids": ["bad"]}]}, {target}, True)

    def test_embedding_and_settings_contract(self):
        vector = normalize_embedding([3.0, 4.0] + [0.0] * 1022)
        self.assertAlmostEqual(sum(item * item for item in vector), 1.0)
        self.assertTrue(query_document("hello").endswith("hello"))
        self.assertEqual(format_embedding_input("hello", query=True, query_prefix="Query: ", document_prefix="Document: "), "Query: hello")
        self.assertEqual(format_embedding_input("hello", query=False, query_prefix="Query: ", document_prefix="Document: "), "Document: hello")
        with self.assertRaises(ValueError):
            normalize_embedding([1.0])
        with self.assertRaises(ValueError):
            normalize_embedding([float("nan")] + [0.0] * 1023)
        with self.assertRaises(RuntimeError):
            load_memory_settings({})

    def test_settings_accept_a_served_alias_and_keep_prefix_whitespace(self):
        env = {
            "MEMORY_DATABASE_URL": "postgresql://localhost/memory",
            "MEMORY_DEFAULT_USER_ID": str(uuid4()),
            "MEMORY_DEFAULT_CHARACTER_ID": str(uuid4()),
            "MEMORY_DATABASE_SCHEMA": "ai_vt_memory",
            "MEMORY_AI_API_KEY": "test-key",
            "MEMORY_AI_BASE_URL": "https://memory.example/v1",
            "MEMORY_AI_MODEL": "test-memory-model",
            "EMBEDDING_AI_API_KEY": "local-vllm",
            "EMBEDDING_AI_BASE_URL": "http://127.0.0.1:18000/v1",
            "EMBEDDING_AI_MODEL": "jinaai/jina-embeddings-v5-text-small-retrieval",
            "EMBEDDING_AI_SERVING_MODEL": "jina-retrieval",
            "EMBEDDING_AI_DIMENSION": "1024",
            "EMBEDDING_AI_QUERY_PREFIX": "Query: ",
            "EMBEDDING_AI_DOCUMENT_PREFIX": "Document: ",
        }
        settings = load_memory_settings(env)
        self.assertEqual(settings.embedding_model, "jinaai/jina-embeddings-v5-text-small-retrieval")
        self.assertEqual(settings.embedding_serving_model, "jina-retrieval")
        self.assertEqual(settings.embedding_query_prefix, "Query: ")
        self.assertEqual(settings.embedding_document_prefix, "Document: ")

    def test_test_mode_rejects_wrong_database_or_schema(self):
        settings = {
            "MEMORY_DATABASE_URL": "postgresql://localhost/test_db",
            "MEMORY_TEST_DATABASE_URL": "postgresql://localhost/other_db",
            "MEMORY_DEFAULT_USER_ID": str(uuid4()),
            "MEMORY_DEFAULT_CHARACTER_ID": str(uuid4()),
            "MEMORY_DATABASE_SCHEMA": "test_" + uuid4().hex,
            "AI_VT_TEST_MODE": "true",
        }
        with self.assertRaisesRegex(RuntimeError, "只能連接"):
            load_memory_settings(settings)
        settings["MEMORY_TEST_DATABASE_URL"] = settings["MEMORY_DATABASE_URL"]
        settings["MEMORY_DATABASE_SCHEMA"] = "public"
        with self.assertRaisesRegex(RuntimeError, "測試模式"):
            load_memory_settings(settings)


if __name__ == "__main__":
    unittest.main()
