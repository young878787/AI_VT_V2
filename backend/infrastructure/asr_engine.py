"""sherpa-onnx 線上（串流）辨識器工廠與整段轉寫。

port 自 voice_txt/src/voice_txt/asr.py，差異：
  - 預設模型目錄改為 backend/models/x-asr-zh-tw-en-streaming-ft75m（可由參數覆寫）
  - 移除 offline 辨識器（voice_txt 中即為實驗性、未使用）

模型：Luigi/x-asr-zh-tw-en-streaming-ft75m（zh-TW/En streaming transducer）
"""
from pathlib import Path

import numpy as np

MODEL_FILES = ("encoder.int8.onnx", "decoder.onnx", "joiner.int8.onnx", "tokens.txt")

_BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = _BACKEND_DIR / "models" / "x-asr-zh-tw-en-streaming-ft75m"


def check_model_files(model_dir: str | Path) -> Path:
    d = Path(model_dir)
    missing = [f for f in MODEL_FILES if not (d / f).exists()]
    if missing:
        raise FileNotFoundError(f"ASR 模型檔缺失 {missing}（目錄：{d}）")
    return d


def build_online_recognizer(model_dir: str | Path | None = None, num_threads: int = 2,
                            provider: str = "cpu"):
    import sherpa_onnx

    d = check_model_files(model_dir or DEFAULT_MODEL_DIR)
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=str(d / "tokens.txt"),
        encoder=str(d / "encoder.int8.onnx"),
        decoder=str(d / "decoder.onnx"),
        joiner=str(d / "joiner.int8.onnx"),
        num_threads=num_threads,
        provider=provider,
        decoding_method="greedy_search",
    )


def _result_text(stream) -> str:
    if hasattr(stream, "result"):
        return stream.result.text
    get_result = getattr(stream, "get_result", None)
    if callable(get_result):
        return get_result().text
    raise AttributeError("無法從 stream 取得辨識結果")


def transcribe_online_streaming(samples: np.ndarray, recognizer, sample_rate: int = 16000,
                                chunk_sec: float = 0.5, tail_silence_sec: float = 2.0) -> str:
    """分塊餵入模擬串流；尾端補靜音把 chunk 吐完（模型卡建議約 2 秒）。

    結尾會呼叫 stream.input_finished()（若版本支援）再做最後 decode，
    否則短段（2 秒左右）容易因未觸發端點而回空字。
    """
    s = recognizer.create_stream()
    chunk = int(sample_rate * chunk_sec)
    for i in range(0, len(samples), chunk):
        s.accept_waveform(sample_rate, samples[i:i + chunk])
        recognizer.decode_streams([s])
    tail = np.zeros(int(sample_rate * tail_silence_sec), dtype=np.float32)
    s.accept_waveform(sample_rate, tail)
    recognizer.decode_streams([s])
    fin = getattr(s, "input_finished", None)
    if callable(fin):
        try:
            fin()
        except Exception:
            pass
        try:
            recognizer.decode_streams([s])
        except Exception:
            pass
    if hasattr(recognizer, "get_result"):
        r = recognizer.get_result(s)
        return r if isinstance(r, str) else r.text
    return _result_text(s)
