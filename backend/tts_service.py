"""本地 Piper TTS 服務：合成 WAV 後經 WebSocket 交給前端播放。"""
from __future__ import annotations

import asyncio
import base64
import io
import threading
import wave

from core.config import (
    PIPER_LENGTH_SCALE,
    PIPER_MODEL_PATH,
    PIPER_SPEAKER_ID,
    TTS_ENABLED,
)
from infrastructure.piper_tts import PiperTTS


class TTSService:
    """以本地 Piper ONNX 模型合成單段語音。"""

    def __init__(self) -> None:
        self.voice: PiperTTS | None = None
        self.enabled = False
        self._synthesis_lock = threading.Lock()

        if not TTS_ENABLED:
            print("[TTS] TTS_ENABLED=false，本地 TTS 服務未啟用")
            return

        try:
            self.voice = PiperTTS(
                model_path=PIPER_MODEL_PATH,
                speaker_id=PIPER_SPEAKER_ID,
                length_scale=PIPER_LENGTH_SCALE,
            )
            self.enabled = True
            print(
                f"[TTS] 本地 Piper 已啟用 | model: {PIPER_MODEL_PATH} "
                f"| sample_rate: {self.voice.sample_rate}"
            )
        except Exception as exc:
            print(f"[TTS] 本地 Piper 初始化失敗，語音功能停用: {exc}")

    def is_enabled(self) -> bool:
        """檢查本地 TTS 是否可用。"""
        return self.enabled and self.voice is not None

    def _synthesize_sync(self, text: str, speaking_rate: float) -> tuple[bytes, int, list[dict]] | None:
        voice = self.voice
        if voice is None:
            return None

        # Action 的 speaking_rate 沿用既有契約；Piper 以較小的 length_scale 加快語速。
        rate = max(0.25, min(2.0, float(speaking_rate)))
        length_scale = max(0.01, PIPER_LENGTH_SCALE / rate)
        # Piper/ONNX session 以單一模型實例序列化，避免取消中的背景執行緒與下一輪並行前向。
        with self._synthesis_lock:
            audio_bytes, _sample_rate, segments = voice.synthesize_to_wav_with_segments(
                text,
                length_scale=length_scale,
            )

        if not audio_bytes:
            return None

        with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
            duration_ms = round(
                wav_file.getnframes() / wav_file.getframerate() * 1000
            )
        return audio_bytes, duration_ms, segments

    async def synthesize(self, text: str, speaking_rate: float = 1.0) -> dict | None:
        """合成一句本地 WAV，回傳既有 WebSocket voice payload 所需欄位。"""
        if not self.is_enabled() or not text or not text.strip():
            return None

        try:
            result = await asyncio.to_thread(
                self._synthesize_sync,
                text.strip(),
                speaking_rate,
            )
            if result is None:
                return None

            audio_bytes, duration_ms, segments = result
            return {
                "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
                "duration_ms": duration_ms,
                "format": "wav",
                "segments": segments,
            }
        except Exception as exc:
            print(f"[TTS] 本地 Piper 合成失敗: {exc}")
            return None


_tts_service: TTSService | None = None
_tts_initialization_lock = threading.Lock()


def get_tts_service() -> TTSService:
    """取得本地 TTS 服務單例。"""
    global _tts_service
    # 取消等待不會停止模型載入的 thread；下一輪必須沿用同一個 singleton。
    with _tts_initialization_lock:
        if _tts_service is None:
            _tts_service = TTSService()
    return _tts_service


if __name__ == "__main__":
    async def test() -> None:
        service = get_tts_service()
        if not service.is_enabled():
            print("本地 Piper TTS 未啟用，請檢查 TTS_ENABLED 與模型檔")
            return

        result = await service.synthesize("哇！你好呀！今天天氣真好呢！", speaking_rate=1.1)
        if result:
            with open("test_output.wav", "wb") as output_file:
                output_file.write(base64.b64decode(result["audio_base64"]))
            print(f"測試音訊已儲存: test_output.wav ({result['duration_ms']}ms)")

    asyncio.run(test())
