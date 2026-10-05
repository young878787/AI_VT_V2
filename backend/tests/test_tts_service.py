"""本地 Piper TTS 服務契約測試，不載入實際 ONNX 模型。"""
import asyncio
import base64
import io
import pathlib
import sys
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
            synthesize_to_wav=Mock(return_value=(audio, 22050)),
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
        voice.synthesize_to_wav.assert_called_once_with("你好。", length_scale=0.8)

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


if __name__ == "__main__":
    unittest.main()
