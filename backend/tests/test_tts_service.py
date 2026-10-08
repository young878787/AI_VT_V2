"""本地 Piper TTS 服務契約測試，不載入實際 ONNX 模型。"""
import asyncio
import base64
import io
import pathlib
import sys
import threading
import wave
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import tts_service
from services.chat_service import synthesize_and_send_voice


def _wav_bytes(sample_rate: int = 22050, duration_ms: int = 100) -> bytes:
    buffer = io.BytesIO()
    frame_count = sample_rate * duration_ms // 1000
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * frame_count)
    return buffer.getvalue()


class LocalTTSServiceTests(unittest.TestCase):
    def test_disabled_service_does_not_load_local_model(self):
        with patch.object(tts_service, "TTS_ENABLED", False), patch.object(tts_service, "PiperTTS") as piper:
            service = tts_service.TTSService()

        self.assertFalse(service.is_enabled())
        piper.assert_not_called()

    def test_synthesize_returns_local_wav_payload(self):
        audio = _wav_bytes()
        voice = SimpleNamespace(
            sample_rate=22050,
            synthesize_to_wav_with_segments=Mock(return_value=(audio, 22050, [
                {"id": 0, "startMs": 0, "endMs": 100},
            ])),
        )
        with patch.object(tts_service, "TTS_ENABLED", True), \
             patch.object(tts_service, "PIPER_MODEL_PATH", "local-model.onnx"), \
             patch.object(tts_service, "PIPER_SPEAKER_ID", 1), \
             patch.object(tts_service, "PIPER_LENGTH_SCALE", 1.0), \
             patch.object(tts_service, "PiperTTS", return_value=voice):
            service = tts_service.TTSService()
            result = asyncio.run(service.synthesize("  你好。  ", speaking_rate=1.25))

        self.assertTrue(service.is_enabled())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["format"], "wav")
        self.assertEqual(base64.b64decode(result["audio_base64"]), audio)
        self.assertEqual(result["duration_ms"], 100)
        self.assertEqual(result["segments"], [{"id": 0, "startMs": 0, "endMs": 100}])
        voice.synthesize_to_wav_with_segments.assert_called_once_with("你好。", length_scale=0.8)

    def test_chat_forwarder_keeps_voice_contract_for_local_wav(self):
        payloads: list[dict] = []

        class Socket:
            async def send_json(self, payload: dict) -> None:
                payloads.append(payload)

        local_service = SimpleNamespace(
            is_enabled=Mock(return_value=True),
            synthesize=AsyncMock(return_value={
                "audio_base64": "local-wav",
                "duration_ms": 120,
                "format": "wav",
            }),
        )
        with patch.object(tts_service, "get_tts_service", return_value=local_service):
            asyncio.run(synthesize_and_send_voice(Socket(), "你好。", 1.0, "turn-1"))

        self.assertEqual(payloads, [{
            "type": "voice",
            "audio": "local-wav",
            "durationMs": 120,
            "format": "wav",
            "turn_id": "turn-1",
        }])

    def test_chat_forwarder_reports_unavailable_for_disabled_empty_and_failed_tts(self):
        for reason in ("disabled", "empty", "error", "initialization_error"):
            with self.subTest(reason=reason):
                send = AsyncMock()
                service = SimpleNamespace(
                    is_enabled=Mock(return_value=reason != "disabled"),
                    synthesize=AsyncMock(return_value=None),
                )
                if reason == "error":
                    service.synthesize.side_effect = RuntimeError("synthesis failed")
                with patch.object(tts_service, "get_tts_service", return_value=service) as get_service:
                    if reason == "initialization_error":
                        get_service.side_effect = RuntimeError("initialization failed")
                    asyncio.run(synthesize_and_send_voice(None, "你好。", 1.0, "turn-1", send))
                send.assert_awaited_once_with({
                    "type": "voice_unavailable", "turn_id": "turn-1",
                    "reason": "error" if reason == "initialization_error" else reason,
                })
                if reason == "disabled":
                    service.synthesize.assert_not_awaited()

    def test_chat_forwarder_bounds_synthesis_and_does_not_send_late_voice(self):
        send = AsyncMock()

        async def synthesize(**kwargs):
            await asyncio.Event().wait()

        service = SimpleNamespace(is_enabled=Mock(return_value=True), synthesize=AsyncMock(side_effect=synthesize))
        with patch.object(tts_service, "get_tts_service", return_value=service), \
                patch("services.chat_service._TTS_TIMEOUT_SEC", 0.02):
            asyncio.run(synthesize_and_send_voice(None, "你好。", 1.0, "turn-1", send))
        send.assert_awaited_once_with({"type": "voice_unavailable", "reason": "timeout", "turn_id": "turn-1"})

    def test_chat_forwarder_bounds_initialization_without_blocking_event_loop(self):
        release = threading.Event()
        send = AsyncMock()

        def initialize():
            release.wait(timeout=1)
            return SimpleNamespace(is_enabled=lambda: False)

        async def run():
            try:
                await synthesize_and_send_voice(None, "你好。", 1.0, "turn-1", send)
                self.assertFalse(release.is_set())
            finally:
                release.set()

        with patch.object(tts_service, "get_tts_service", side_effect=initialize), \
                patch("services.chat_service._TTS_TIMEOUT_SEC", 0.02):
            asyncio.run(run())
        send.assert_awaited_once_with({"type": "voice_unavailable", "reason": "timeout", "turn_id": "turn-1"})

    def test_cancelled_chat_forwarder_sends_no_terminal_event(self):
        send = AsyncMock()

        async def run():
            started = asyncio.Event()

            async def synthesize(**kwargs):
                started.set()
                await asyncio.Event().wait()

            service = SimpleNamespace(is_enabled=Mock(return_value=True), synthesize=AsyncMock(side_effect=synthesize))
            with patch.object(tts_service, "get_tts_service", return_value=service):
                task = asyncio.create_task(synthesize_and_send_voice(None, "你好。", 1.0, "turn-1", send))
                await asyncio.wait_for(started.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(run())
        send.assert_not_awaited()

    def test_send_failure_does_not_emit_a_second_terminal_event(self):
        send = AsyncMock(side_effect=RuntimeError("socket closed"))
        service = SimpleNamespace(is_enabled=Mock(return_value=False))
        with patch.object(tts_service, "get_tts_service", return_value=service):
            with self.assertRaisesRegex(RuntimeError, "socket closed"):
                asyncio.run(synthesize_and_send_voice(None, "你好。", 1.0, "turn-1", send))
        self.assertEqual(send.await_count, 1)


if __name__ == "__main__":
    unittest.main()
