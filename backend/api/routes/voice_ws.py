"""
Voice WebSocket 端點（即時語音輸入）。

協議（見 docs/2026-09-21-語音即時整合計劃.md §8）：
- C→S binary：raw Int16LE PCM 16kHz mono，建議 ~100ms/幀
- C→S JSON：{"type":"mic_state","active":true|false}（半雙工閘門，false 時丟棄音訊）
- S→C JSON：{"type":"asr_state","state":"listening"|"processing"|"idle"}
            {"type":"asr_final","text":"...","durationMs":1234}
            {"type":"error","message":"..."}

同步 CPU 工作（AGC/VAD/ASR）跑在每連線一條 worker thread：
SpeechSegmenter 非執行緒安全，必須單執行緒餵入；辨識結果以
run_coroutine_threadsafe 送回事件迴圈，避免卡住 asyncio。
"""
import asyncio
import json
import queue
import threading

import numpy as np
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.config import (
    ASR_ENABLED,
    ASR_MODEL_DIR,
    ASR_SAMPLE_RATE,
    ASR_SILENCE_SEC,
    ASR_USE_AGC,
    VAD_MODEL_DIR,
)
from infrastructure.asr_engine import build_online_recognizer, transcribe_online_streaming
from infrastructure.vad_engine import AutoGain, SpeechSegmenter, VadWrapper

router = APIRouter()

# ASR 模型載入耗時（約 1s），所有連線共用一個 recognizer；
# decode 保守起見以鎖保護（本應用實際上為單人單連線）。
_recognizer = None
_recognizer_lock = threading.Lock()
_ASR_DECODE_LOCK = threading.Lock()

_STOP = object()  # worker 收尾哨兵
_FRAME_QUEUE_MAX = 250  # 幀佇列上限（100ms/幀 ≈ 25s 音訊），滿時丟幀保護記憶體


def _get_recognizer():
    global _recognizer
    if _recognizer is None:
        with _recognizer_lock:
            if _recognizer is None:
                _recognizer = build_online_recognizer(ASR_MODEL_DIR)
    return _recognizer


class _VoiceSession:
    """單一 /ws/voice 連線的音訊處理 session（worker thread 持有 VAD/AGC 狀態）。"""

    def __init__(self, websocket: WebSocket, loop: asyncio.AbstractEventLoop) -> None:
        self._ws = websocket
        self._loop = loop
        self._sample_rate = ASR_SAMPLE_RATE
        self._mic_active = False
        self._frames: queue.Queue = queue.Queue(maxsize=_FRAME_QUEUE_MAX)
        vad = VadWrapper(model_path=VAD_MODEL_DIR, sample_rate=self._sample_rate)
        agc = AutoGain(sample_rate=self._sample_rate) if ASR_USE_AGC else None
        self._segmenter = SpeechSegmenter(
            vad,
            sample_rate=self._sample_rate,
            silence_sec=ASR_SILENCE_SEC,
            min_speech_sec=0.2,
            pre_roll_sec=0.5,
            tail_pad_sec=0.2,
            agc=agc,
        )
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    # ---------- 事件迴圈側 ----------

    def put_frame(self, frame: bytes) -> None:
        try:
            self._frames.put_nowait(frame)
        except queue.Full:
            pass  # 佇列滿：丟幀保護，ASR 端點判定不受單幀缺失影響

    def set_mic_active(self, active: bool) -> None:
        self._mic_active = bool(active)
        self._segmenter.reset()  # 閘門切換時丟掉殘留狀態（pre-roll/半開段）
        self._send({"type": "asr_state", "state": "listening" if active else "idle"})

    def handle_control(self, text: str) -> None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self._send({"type": "error", "message": "無效的 JSON 控制訊息"})
            return
        if data.get("type") == "mic_state":
            self.set_mic_active(bool(data.get("active")))
        else:
            self._send({"type": "error", "message": f"未知的控制訊息: {data.get('type')}"})

    def close(self) -> None:
        self._frames.put(_STOP)
        self._thread.join(timeout=5)

    # ---------- worker thread 側 ----------

    def _send(self, payload: dict) -> None:
        asyncio.run_coroutine_threadsafe(self._ws.send_json(payload), self._loop)

    def _worker_loop(self) -> None:
        while True:
            item = self._frames.get()
            if item is _STOP:
                return
            if not self._mic_active:
                continue
            x = np.frombuffer(item, dtype=np.int16).astype(np.float32) / 32768.0
            try:
                for seg in self._segmenter.feed(x):
                    self._on_segment(seg)
            except Exception as e:  # 語音處理出錯不關連線，回報後繼續
                self._send({"type": "error", "message": f"語音處理錯誤: {e}"})

    def _on_segment(self, seg: np.ndarray) -> None:
        dur_ms = int(len(seg) / self._sample_rate * 1000)
        self._send({"type": "asr_state", "state": "processing"})
        try:
            with _ASR_DECODE_LOCK:
                text = transcribe_online_streaming(
                    seg, _get_recognizer(), self._sample_rate).strip()
        except Exception as e:
            self._send({"type": "error", "message": f"ASR 錯誤: {e}"})
            return
        self._send({"type": "asr_state", "state": "listening"})
        if text:
            self._send({"type": "asr_final", "text": text, "durationMs": dur_ms})


@router.websocket("/ws/voice")
async def voice_endpoint(websocket: WebSocket):
    await websocket.accept()
    if not ASR_ENABLED:
        await websocket.send_json({
            "type": "error",
            "message": "ASR_ENABLED=false，語音輸入未啟用（請在 .env 設定後重啟後端）",
        })
        await websocket.close()
        return

    loop = asyncio.get_running_loop()
    session = _VoiceSession(websocket, loop)
    try:
        await websocket.send_json({"type": "asr_state", "state": "idle"})
        while True:
            message = await websocket.receive()
            if message.get("bytes"):
                session.put_frame(message["bytes"])
            elif message.get("text"):
                session.handle_control(message["text"])
    except WebSocketDisconnect:
        pass
    finally:
        session.close()
