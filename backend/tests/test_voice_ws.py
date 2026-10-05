"""Voice WebSocket 啟動預載與 ready 握手測試。"""
import pathlib
import sys
import unittest
from unittest.mock import Mock, patch

from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketDisconnected

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from api.routes import voice_ws


class VoiceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_recognizer = voice_ws._recognizer
        self.original_error = voice_ws._recognizer_error
        voice_ws._recognizer = None
        voice_ws._recognizer_error = None

    def tearDown(self) -> None:
        voice_ws._recognizer = self.original_recognizer
        voice_ws._recognizer_error = self.original_error

    async def test_startup_preloads_enabled_recognizer(self):
        recognizer = object()
        with patch.object(voice_ws, "ASR_ENABLED", True), \
             patch.object(voice_ws, "build_online_recognizer", return_value=recognizer) as build:
            ready = await voice_ws.initialize_voice_runtime()

        self.assertTrue(ready)
        self.assertIs(voice_ws._recognizer, recognizer)
        self.assertIsNone(voice_ws.voice_runtime_error())
        build.assert_called_once_with(voice_ws.ASR_MODEL_DIR)

    async def test_startup_failure_is_reported_without_raising(self):
        with patch.object(voice_ws, "ASR_ENABLED", True), \
             patch.object(voice_ws, "build_online_recognizer", side_effect=RuntimeError("bad model")), \
             self.assertLogs("api.routes.voice_ws", level="ERROR"):
            ready = await voice_ws.initialize_voice_runtime()

        self.assertFalse(ready)
        self.assertEqual(voice_ws.voice_runtime_error(), "bad model")

    async def test_disabled_runtime_does_not_load_model(self):
        with patch.object(voice_ws, "ASR_ENABLED", False), \
             patch.object(voice_ws, "build_online_recognizer") as build:
            ready = await voice_ws.initialize_voice_runtime()

        self.assertFalse(ready)
        build.assert_not_called()

    async def test_websocket_announces_ready_after_runtime_is_available(self):
        voice_ws._recognizer = object()
        for disconnect_type in (WebSocketDisconnect, WebSocketDisconnected):
            with self.subTest(disconnect_type=disconnect_type.__name__):
                payloads: list[dict] = []

                class Socket:
                    async def accept(self):
                        pass

                    async def send_json(self, payload: dict):
                        payloads.append(payload)

                    async def receive(self):
                        raise disconnect_type()

                session = Mock()
                with patch.object(voice_ws, "ASR_ENABLED", True), \
                     patch.object(voice_ws, "_VoiceSession", return_value=session):
                    await voice_ws.voice_endpoint(Socket())

                self.assertEqual(payloads, [{"type": "asr_state", "state": "ready"}])
                session.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
