"""P0 入口隔離防護；不讀取 .env，不建立真實 socket／DB／子程序。"""

import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.architecture_baseline import block_external_io, configured_profile, isolated_environment, SUITE, SUPPLEMENT


class ArchitectureBaselineTests(unittest.TestCase):
    def test_environment_removes_credentials_database_proxy_and_live_switches(self):
        with patch.dict(os.environ, {
            "CHAT_AI_API_KEY": "inherited-placeholder", "MEMORY_TEST_DATABASE_URL": "inherited-placeholder",
            "HTTP_PROXY": "inherited-placeholder", "GOOGLE_APPLICATION_CREDENTIALS": "inherited-placeholder",
            "MEMORY_LIVE_EVAL": "1", "ASR_ENABLED": "true", "TTS_ENABLED": "true",
        }):
            with patch.dict(os.environ, isolated_environment("/tmp/baseline-test"), clear=True):
                for name in ("MEMORY_DATABASE_URL", "MEMORY_TEST_DATABASE_URL", "HTTP_PROXY",
                             "GOOGLE_APPLICATION_CREDENTIALS", "MEMORY_LIVE_EVAL"):
                    self.assertNotIn(name, os.environ)
                self.assertEqual(os.environ["PYTHON_DOTENV_DISABLED"], "1")
                self.assertEqual(os.environ["CHAT_AI_API_KEY"], "baseline-placeholder")
                self.assertEqual(os.environ["ASR_ENABLED"], "false")
                self.assertEqual(os.environ["TTS_ENABLED"], "false")

    def test_guards_block_socket_dns_libpq_and_child_process_paths(self):
        import psycopg

        with block_external_io() as attempts:
            with socket.socket() as tcp, socket.socket(socket.AF_UNIX) as local:
                calls = (
                    lambda: socket.create_connection(("127.0.0.1", 1)),
                    lambda: socket.getaddrinfo("example.invalid", 443),
                    lambda: tcp.connect(("127.0.0.1", 1)),
                    lambda: tcp.connect_ex(("127.0.0.1", 1)),
                    lambda: tcp.sendto(b"probe", ("127.0.0.1", 1)),
                    lambda: local.connect("/tmp/baseline-denied.sock"),
                    lambda: psycopg.Connection.connect("postgresql://127.0.0.1:1/baseline"),
                    lambda: asyncio.run(psycopg.AsyncConnection.connect("postgresql://127.0.0.1:1/baseline")),
                    lambda: subprocess.Popen(["false"]),
                )
                for index, call in enumerate(calls):
                    with self.subTest(path=index), self.assertRaises(AssertionError):
                        call()
            self.assertEqual(len(attempts), len(calls))
            self.assertEqual(set(attempts), {"external_io_blocked"})

    def test_suite_excludes_database_and_live_evaluation(self):
        self.assertEqual(len(SUITE), 8)
        for module in (*SUITE, *SUPPLEMENT):
            self.assertNotIn("database_integration", module)
            self.assertNotIn("agents_live", module)

    def test_dotenv_cannot_restore_a_credential_in_isolated_execution(self):
        from dotenv import load_dotenv

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("MEMORY_AI_API_KEY=dotenv-placeholder\nEXTERNAL_CREDENTIAL=dotenv-placeholder\n")
            with patch.dict(os.environ, isolated_environment(directory), clear=True):
                self.assertFalse(load_dotenv(path, override=True))
                self.assertNotIn("EXTERNAL_CREDENTIAL", os.environ)
                self.assertEqual(os.environ["MEMORY_AI_API_KEY"], "baseline-placeholder")

    def test_configuration_inventory_does_not_export_secret_fields(self):
        # 檔案盤點不應將 key、URL、owner、schema 或其他 dotenv 欄位複製到報告。
        metadata = {"CHAT_AI_BASE_URL": "https://api.openai.com/v1", "CHAT_AI_MODEL": "snapshot-model",
                    "CHAT_AI_API_KEY": "private-sentinel", "MEMORY_DATABASE_URL": "private-sentinel",
                    "EMBEDDING_AI_MODEL": "jinaai/jina-embeddings-v5-text-small-retrieval"}
        with patch("dotenv.dotenv_values", return_value=metadata), patch.dict(os.environ, {}, clear=True):
            profile = configured_profile()
        import json
        self.assertNotIn("private-sentinel", json.dumps(profile))
        self.assertNotIn("https://", json.dumps(profile))
        self.assertEqual(profile["roles"]["CHAT"], {"provider": "openai", "model": "snapshot-model"})
        self.assertEqual(profile["embedding"]["query_prefix"], "Query: ")
        self.assertEqual(profile["embedding"]["document_prefix"], "Document: ")


if __name__ == "__main__":
    unittest.main()
