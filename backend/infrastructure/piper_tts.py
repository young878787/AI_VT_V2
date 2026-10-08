"""Piper TTS 封裝：逐句合成 → 記憶體內 WAV（播放交給前端）。

port 自 voice_txt/src/voice_txt/tts.py，差異：
  - 移除 sounddevice 播放與 TTSWorker（後端改為合成後經 WebSocket 送前端播放）
  - 合成輸出改為 synthesize_to_wav()：回傳 (wav_bytes, sample_rate)
  - SentenceSplitter 與正式句段切分共用 domain/speech_segments

目前聊天鏈路：完整 LLM 回覆 → TTSService → PiperTTS 合成 WAV
→ `voice` 訊息（base64 WAV）→ 前端播放與口型同步。
正式合成逐段保留樣本時間，再串接成單一 WAV。
"""
from __future__ import annotations

import io
import time
import wave
from pathlib import Path

import numpy as np

from domain.speech_segments import SentenceSplitter as SentenceSplitter, split_speech_segments

_BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = _BACKEND_DIR / "models" / "zh_TW-multi-voice.onnx"

class PiperTTS:
    """piper1-gpl 封裝：載入一次，逐句合成為 16-bit mono WAV bytes。

    piper 為 lazy import：本模組可在未安裝 piper 的環境匯入（SentenceSplitter 測試），
    僅在實例化 PiperTTS 時才需要 piper 套件與模型檔。
    """

    def __init__(self, model_path: str | Path | None = None, speaker_id: int = 1,
                 length_scale: float = 1.0) -> None:
        from piper import PiperVoice

        self.model_path = Path(model_path) if model_path else DEFAULT_MODEL
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"TTS 模型不存在：{self.model_path}（請將 piper 模型與 .onnx.json 放入 backend/models/）")
        self.config_json = Path(f"{self.model_path}.json")
        if not self.config_json.exists():
            raise FileNotFoundError(
                f"缺少 piper 設定檔：{self.config_json}（phoneme_id_map/sr/speakers 必需）")
        self.speaker_id = speaker_id
        self.length_scale = length_scale
        t0 = time.perf_counter()
        # g2pW 中文前端資源位於模型同目錄的 g2pW/（需預先放置，避免啟動時依賴網路下載）
        self.voice = PiperVoice.load(str(self.model_path),
                                     download_dir=str(self.model_path.parent))
        self.load_ms = (time.perf_counter() - t0) * 1000
        self.sample_rate = int(self.voice.config.sample_rate)
        self.num_speakers = int(getattr(self.voice.config, "num_speakers", 1) or 1)
        if self.speaker_id >= self.num_speakers:
            raise ValueError(f"speaker_id={self.speaker_id} 超出模型範圍（0~{self.num_speakers - 1}）")
        # 暖機：g2pW 中文前端建立 + ONNX session 首次前向較慢，
        # 在載入階段做掉，避免第一個真實句的首音被冷啟動拖慢。
        t_w = time.perf_counter()
        next(self.voice.synthesize("你好。", syn_config=self._syn_config()), None)
        self.warmup_ms = (time.perf_counter() - t_w) * 1000

    def _syn_config(self, length_scale: float | None = None):
        from piper import SynthesisConfig

        return SynthesisConfig(speaker_id=self.speaker_id,
                               length_scale=self.length_scale if length_scale is None else length_scale,
                               normalize_audio=True)

    def synthesize_float32(self, text: str, length_scale: float | None = None) -> tuple[np.ndarray, int]:
        """合成一句 → (float32 mono 樣本, sample_rate)。"""
        chunks = [c.audio_float_array for c in self.voice.synthesize(
            text, syn_config=self._syn_config(length_scale))]
        if not chunks:
            return np.zeros(0, dtype=np.float32), self.sample_rate
        audio = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        return np.asarray(audio, dtype=np.float32), self.sample_rate

    def synthesize_to_wav(self, text: str, length_scale: float | None = None) -> tuple[bytes, int]:
        """合成一句 → (16-bit mono WAV bytes, sample_rate)，可直接餵瀏覽器 decodeAudioData。"""
        audio, sr = self.synthesize_float32(text, length_scale)
        return self._encode_wav(audio, sr), sr

    def synthesize_to_wav_with_segments(
        self, text: str, length_scale: float | None = None,
    ) -> tuple[bytes, int, list[dict]]:
        """Synthesize each shared segment once and retain its actual WAV sample bounds."""
        arrays = []
        segments = []
        frames = 0
        for segment_id, segment in enumerate(split_speech_segments(text)):
            audio, sr = self.synthesize_float32(segment, length_scale)
            if sr != self.sample_rate:
                raise ValueError("Piper sample rate changed during synthesis")
            if not audio.size:
                continue
            start_ms = round(frames / sr * 1000, 3)
            frames += audio.size
            arrays.append(audio)
            segments.append({"id": segment_id, "startMs": start_ms,
                             "endMs": round(frames / sr * 1000, 3)})
        if not arrays:
            return b"", self.sample_rate, []
        audio = np.concatenate(arrays) if len(arrays) > 1 else arrays[0]
        return self._encode_wav(audio, self.sample_rate), self.sample_rate, segments

    @staticmethod
    def _encode_wav(audio: np.ndarray, sr: int) -> bytes:
        pcm16 = (np.clip(audio, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm16)
        return buf.getvalue()
