# -*- coding: utf-8 -*-
"""
TTS 服務模組 - Google Cloud Text-to-Speech Chirp 3 HD
支援情緒化語音合成與 Live2D 口型同步
"""

import os
import base64
import asyncio
from typing import Optional, AsyncIterator
from dotenv import load_dotenv

load_dotenv(dotenv_path="../.env")

# 延遲載入 Google Cloud TTS（避免未安裝時影響其他功能）
_texttospeech = None
_tts_available = False


def _lazy_import_tts():
    """延遲載入 Google Cloud TTS SDK"""
    global _texttospeech, _tts_available
    if _texttospeech is not None:
        return _tts_available

    try:
        from google.cloud import texttospeech_v1beta1 as texttospeech

        _texttospeech = texttospeech
        _tts_available = True
        print("[TTS] Google Cloud TTS SDK 載入成功")
    except ImportError:
        print("[TTS] 警告: google-cloud-texttospeech 未安裝，TTS 功能停用")
        print("[TTS] 請執行: pip install google-cloud-texttospeech")
        _tts_available = False

    return _tts_available


class TTSService:
    """
    Google Cloud TTS Chirp 3 HD 服務

    特性：
    - 支援高品質 HD 語音
    - 支援多語言（日文、中文、英文等）
    - 語速控制 (0.25 - 2.0)
    - 停頓標記 [pause short], [pause], [pause long]
    """

    def __init__(self):
        self.client = None
        self.enabled = False
        self.voice = None
        self.default_audio_config = None

        # 檢查是否啟用 TTS
        tts_enabled = os.getenv("TTS_ENABLED", "false").lower() == "true"
        if not tts_enabled:
            print("[TTS] TTS_ENABLED=false，TTS 服務未啟用")
            return

        # 延遲載入 SDK
        if not _lazy_import_tts():
            return

        try:
            # SDK 會自動去 ADC 快取找 gcloud auth application-default login 的身分
            self.client = _texttospeech.TextToSpeechClient()

            # 語音配置
            language = os.getenv("TTS_LANGUAGE", "ja-JP")
            voice_name = os.getenv("TTS_VOICE_NAME", f"{language}-Chirp3-HD-Aoede")

            self.voice = _texttospeech.VoiceSelectionParams(
                language_code=language,
                name=voice_name,
            )

            self.default_audio_config = _texttospeech.AudioConfig(
                audio_encoding=_texttospeech.AudioEncoding.MP3,
                speaking_rate=1.0,
            )

            self.enabled = True
            print(f"[TTS] 服務已啟用 | 語言: {language} | 語音: {voice_name}")

        except Exception as e:
            print(f"[TTS] 初始化失敗: {e}")
            self.enabled = False

    def is_enabled(self) -> bool:
        """檢查 TTS 服務是否可用"""
        return self.enabled and self.client is not None

    async def synthesize(
        self, text: str, speaking_rate: float = 1.0, use_markup: bool = False
    ) -> Optional[dict]:
        """
        合成語音

        Args:
            text: 要轉換的文字
            speaking_rate: 語速 (0.25 - 2.0)，預設 1.0
            use_markup: 是否使用 markup 模式（支援 [pause] 標記）

        Returns:
            {
                "audio_base64": str,  # Base64 編碼的 MP3 音訊
                "duration_ms": int,   # 估算的音訊時長（毫秒）
                "format": "mp3"
            }
            或 None（如果失敗）
        """
        if not self.is_enabled():
            return None

        if not text or not text.strip():
            return None

        try:
            # 處理停頓標記
            # Chirp 3 HD 的 markup 模式支援: [pause short], [pause], [pause long]
            processed_text = text.strip()

            if use_markup:
                synthesis_input = _texttospeech.SynthesisInput(markup=processed_text)
            else:
                synthesis_input = _texttospeech.SynthesisInput(text=processed_text)

            # 動態調整語速
            clamped_rate = max(0.25, min(2.0, speaking_rate))
            audio_config = _texttospeech.AudioConfig(
                audio_encoding=_texttospeech.AudioEncoding.MP3,
                speaking_rate=clamped_rate,
            )

            # 在 thread pool 中執行（避免阻塞 asyncio）
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.client.synthesize_speech(
                    input=synthesis_input, voice=self.voice, audio_config=audio_config
                ),
            )

            audio_bytes = response.audio_content
            audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")

            # 估算音訊時長
            # MP3 128kbps ≈ 16 bytes/ms
            # 但 Chirp 3 HD 品質較高，估算值需調整
            duration_ms = int(len(audio_bytes) / 20)  # 粗略估算

            print(
                f"[TTS] 合成完成 | 文字長度: {len(text)} | 音訊大小: {len(audio_bytes)} bytes | 預估時長: {duration_ms}ms"
            )

            return {
                "audio_base64": audio_base64,
                "duration_ms": duration_ms,
                "format": "mp3",
            }

        except Exception as e:
            print(f"[TTS] 合成失敗: {e}")
            return None

    async def synthesize_streaming(
        self, text_chunks: list[str], speaking_rate: float = 1.0
    ) -> AsyncIterator[bytes]:
        """
        串流合成語音（搭配 LLM 串流輸出使用）

        Args:
            text_chunks: 文字片段列表
            speaking_rate: 語速

        Yields:
            音訊片段 bytes
        """
        if not self.is_enabled():
            return

        try:
            streaming_config = _texttospeech.StreamingSynthesizeConfig(voice=self.voice)

            config_request = _texttospeech.StreamingSynthesizeRequest(
                streaming_config=streaming_config
            )

            def request_generator():
                yield config_request
                for chunk in text_chunks:
                    if chunk and chunk.strip():
                        yield _texttospeech.StreamingSynthesizeRequest(
                            input=_texttospeech.StreamingSynthesisInput(text=chunk)
                        )

            # 串流合成
            streaming_responses = self.client.streaming_synthesize(request_generator())

            for response in streaming_responses:
                if response.audio_content:
                    yield response.audio_content

        except Exception as e:
            print(f"[TTS] 串流合成失敗: {e}")


# ============================================================
# 單例模式
# ============================================================
_tts_service: Optional[TTSService] = None


def get_tts_service() -> TTSService:
    """取得 TTS 服務單例"""
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service


# ============================================================
# 測試用
# ============================================================
if __name__ == "__main__":
    import asyncio

    async def test():
        service = get_tts_service()

        if not service.is_enabled():
            print("TTS 服務未啟用，請檢查設定")
            return

        # 測試合成
        result = await service.synthesize(
            "哇！你好呀！今天天氣真好呢！", speaking_rate=1.1
        )

        if result:
            # 儲存測試音訊
            audio_bytes = base64.b64decode(result["audio_base64"])
            with open("test_output.mp3", "wb") as f:
                f.write(audio_bytes)
            print(f"測試音訊已儲存: test_output.mp3 ({result['duration_ms']}ms)")
        else:
            print("合成失敗")

    asyncio.run(test())
