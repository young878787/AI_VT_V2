"""即時語音端點偵測（VAD）：麥克風常開 → 自動切段 → 逐段送 ASR。

port 自 voice_txt/src/voice_txt/vad.py，差異：
  - 預設模型目錄改為 backend/models/silero-vad（路徑可由參數覆寫）
  - 移除模型下載 URL 常數（模型已預先放置）

設計（分段式端點，MVP 不做打斷）：
  mic 16k mono → VadWrapper（sherpa Silero / 能量保底）→ SpeechSegmenter 狀態機
  → 完整一段 np.ndarray → asr_engine 轉文字

- sherpa_onnx.VadModel 視窗固定（V4 世代 512 點 / V6 576 點 @16k）；Segmenter 一律
  按模型回報窗切分餵入，保證 mic 即時流與整檔走同一路徑（語音偵測連貫）。
- 模型缺檔或載入失敗時自動降級能量 VAD（RMS 門檻），無模型/CI 也能跑。
- AutoGain：麥訊號太小聲（如 -29dBFS）會超出 Silero 訓練分佈，實測起音延遲可達
  0.76s、句中連續漏接 672ms、短音節整區漏；線性放大不變 SNR，只把電平拉回常見範圍。
"""
from __future__ import annotations

import math
from collections import deque
from pathlib import Path

import numpy as np

_BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_VAD_DIR = _BACKEND_DIR / "models" / "silero-vad"
VAD_MODEL_FILENAME = "silero_vad_v6.onnx"  # 預設 V6（官方 snakers4/silero-vad）
VAD_MODEL_FILENAME_V4 = "silero_vad.onnx"  # 舊版相容（k2-fsa 匯出 V4 世代）


def get_vad_model_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        return p if p.suffix else p / VAD_MODEL_FILENAME
    v6 = DEFAULT_VAD_DIR / VAD_MODEL_FILENAME
    if v6.exists():
        return v6
    v4 = DEFAULT_VAD_DIR / VAD_MODEL_FILENAME_V4  # 已有舊檔沿用，不強迫重下
    return v4 if v4.exists() else v6


class VadWrapper:
    """sherpa Silero VAD，缺模型時降級為能量 VAD。執行緒不安全，請單執行緒餵入。"""

    def __init__(
        self,
        model_path: str | Path | None = None,
        threshold: float = 0.5,
        min_silence_sec: float = 0.5,
        min_speech_sec: float = 0.25,
        max_speech_sec: float = 20.0,
        sample_rate: int = 16000,
        energy_threshold: float = 0.004,  # 實測 test.m4a（max 0.036/p90 0.005）約切 8 段
        force_energy: bool = False,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.threshold = float(threshold)
        self.energy_threshold = float(energy_threshold)
        self.backend = "energy"
        self._model = None
        self._window = 512
        self.model_path: Path | None = None

        cand = get_vad_model_path(model_path)
        if not force_energy and cand.exists():
            try:
                import sherpa_onnx

                cfg = sherpa_onnx.VadModelConfig(
                    silero_vad=sherpa_onnx.SileroVadModelConfig(
                        model=str(cand),
                        threshold=float(threshold),
                        min_silence_duration=float(min_silence_sec),
                        min_speech_duration=float(min_speech_sec),
                        max_speech_duration=float(max_speech_sec),
                    ),
                    sample_rate=int(sample_rate),
                    num_threads=1,
                )
                self._model = sherpa_onnx.VadModel.create(cfg)
                self._window = int(self._model.window_size())
                self.backend = "silero"
                self.model_path = cand
            except Exception:
                self._model = None  # 降級能量 VAD，不炸掉即時迴圈
        if self._model is None:
            self.backend = "energy"
            self._window = 512  # 固定窗，檔/mic 行為一致

    @property
    def window_samples(self) -> int:
        return self._window

    def reset(self) -> None:
        if self._model is not None:
            try:
                self._model.reset()
            except Exception:
                pass

    def is_speech(self, window: np.ndarray) -> bool:
        x = np.asarray(window, dtype=np.float32).reshape(-1)
        if self._model is not None and len(x) >= self._window:
            try:
                return bool(self._model.is_speech(x[: self._window]))
            except Exception:
                pass  # 掉回能量判斷
        if len(x) == 0:
            return False
        rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
        return rms > self.energy_threshold


class AutoGain:
    """簡易 AGC：把太小聲的輸入線性放大到 Silero 習慣的電平。

    - 電平估計 = 近期窗 RMS 的 p90（ring buffer 約 5s）：跟著「說話電平」走，
      單一突發大聲不會把增益卡死；純靜音/剛啟動時增益直接拉上限，
      放大靜音無害，第一個字就吃得到增益。
    - 只放大不縮小、峰值 clip 保護；增益平滑避免跳變。
    - 放大在切段前生效，mic 與整檔同路徑，ASR 也拿到正常電平。
    實測（test.m4a，peak 0.036 ≈ -29dBFS）：×1 起音延遲 0.76s／覆蓋 53%，
    ×8 起音延遲 0.29s／覆蓋 82%（其餘 0.26~0.33s 為 Silero 模型起音慣性，
    交給 pre_roll 覆蓋）。
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        window: int = 512,
        target_rms: float = 0.04,
        max_gain: float = 8.0,
        ring_windows: int = 150,
        smooth_half_windows: int = 6,
    ) -> None:
        self.window = max(int(window), 1)
        self.sample_rate = int(sample_rate)
        self.target = float(target_rms)
        self.max_gain = float(max_gain)
        self._ring: deque[float] = deque(maxlen=max(int(ring_windows), 8))
        self._alpha_half = max(int(smooth_half_windows), 1)  # 增益平滑半衰窗數
        self.gain = 1.0

    def _level_est(self) -> float:
        if not self._ring:
            return 0.0
        arr = np.fromiter(self._ring, dtype=np.float64, count=len(self._ring))
        return float(np.quantile(arr, 0.9))

    def process(self, samples: np.ndarray) -> np.ndarray:
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        n_win = 0
        if len(x) >= self.window:
            n = (len(x) // self.window) * self.window
            wins = x[:n].reshape(-1, self.window).astype(np.float64)
            wrms = np.sqrt((wins ** 2).mean(axis=1))
            for v in wrms:
                self._ring.append(float(v))
            n_win = n // self.window
        est = self._level_est()
        if est < 1e-9:
            want = self.max_gain  # 還沒觀察到訊號：先給上限增益
        else:
            want = min(self.max_gain, max(1.0, self.target / est))
        alpha = 1.0 - 0.5 ** (n_win / self._alpha_half) if n_win else 0.0
        self.gain = float(self.gain + alpha * (want - self.gain))
        if self.gain <= 1.0 + 1e-9:
            return x
        return np.clip(x * np.float32(self.gain), -1.0, 1.0).astype(np.float32)


class SpeechSegmenter:
    """VAD 切段狀態機：IDLE → SPEAKING → 靜音達標端點（emit）。

    - pre_roll：保留端點前 N 秒，避免吃掉起音。
    - min_speech_sec：整段有聲窗累計未達此長度視為雜訊（咳嗽/敲鍵）丟棄。
    - max_turn_sec：單段過長強制切分，避免 LLM context 爆。
    - tail_pad_sec：端點保留些許尾音，ASR 不易斷尾。
    - agc：切段前線性自動增益（可選；小聲 mic 救起音/漏接）。
    - silence_speech_frac：端點用「滑動窗語音窗占比」判定：最近 silence_sec
      的窗中語音窗占比 ≤ 此值即端點。容忍零星誤判窗，不因單一漏窗重置。
    feed() 回傳本批完成段（0~N 段）；flush() 收尾殘段。
    """

    def __init__(
        self,
        vad: VadWrapper,
        sample_rate: int = 16000,
        silence_sec: float = 0.8,
        min_speech_sec: float = 0.3,
        max_turn_sec: float = 20.0,
        pre_roll_sec: float = 0.3,
        tail_pad_sec: float = 0.2,
        agc: AutoGain | None = None,
        silence_speech_frac: float = 0.2,
    ) -> None:
        self.vad = vad
        self.agc = agc
        self.sample_rate = int(sample_rate)
        self.silence_need = max(int(float(silence_sec) * sample_rate), vad.window_samples)
        self.min_speech_need = int(float(min_speech_sec) * sample_rate)
        self.max_turn_samples = int(float(max_turn_sec) * sample_rate)
        self.pre_roll_max = int(float(pre_roll_sec) * sample_rate)
        self.tail_pad = int(float(tail_pad_sec) * sample_rate)
        self._ring_max = max(int(round(float(silence_sec) * sample_rate / vad.window_samples)), 1)
        self._speech_frac_max = float(silence_speech_frac)
        # 被 min_speech_sec 丟棄的雜訊段長（秒），供上層打 log 除錯。
        self.discarded_count = 0
        self.last_discard_sec: float | None = None
        self._discarded: list[float] = []
        self.reset()

    @property
    def is_speaking(self) -> bool:
        """目前是否在語音段內（供即時迴圈顯示提示）。"""
        return self._speaking

    def reset(self) -> None:
        self._speaking = False
        self._pre: deque[np.ndarray] = deque()
        self._pre_len = 0
        self._seg: list[np.ndarray] = []
        self._seg_len = 0
        self._silence_len = 0
        self._speech_len = 0  # 有聲窗累計（非整段長，用於雜訊過濾）
        self._flag_ring: deque[bool] = deque(maxlen=self._ring_max)  # 端點判定滑窗
        self._carry = np.zeros(0, dtype=np.float32)  # 不足一窗的殘 sample，留待下批湊齊
        self.vad.reset()

    def pop_discarded(self) -> list[float]:
        """取走並清空丟棄紀錄（秒）。呼叫方打 log 用，不影響切段狀態。"""
        out = list(getattr(self, "_discarded", []))
        self._discarded = []
        return out

    def _push_pre(self, w: np.ndarray) -> None:
        if self.pre_roll_max <= 0:
            return
        self._pre.append(w.copy())
        self._pre_len += len(w)
        while self._pre_len > self.pre_roll_max and self._pre:
            old = self._pre.popleft()
            self._pre_len -= len(old)

    def _take_pre(self) -> list[np.ndarray]:
        out = list(self._pre)
        self._pre.clear()
        self._pre_len = 0
        return out

    def _emit(self, trim_silence: bool = True) -> np.ndarray | None:
        if not self._seg:
            return None
        seg = np.concatenate(self._seg) if len(self._seg) > 1 else self._seg[0].copy()
        # 端點時多收了整段靜音，只留 tail_pad，其餘裁掉
        if trim_silence and self._silence_len > self.tail_pad and len(seg) > self.tail_pad:
            keep_sil = min(self._silence_len, self.tail_pad)
            seg = seg[: len(seg) - (self._silence_len - keep_sil)]
        ok = self._speech_len >= self.min_speech_need
        if not ok:
            # 雜訊丟棄要留痕，否則「喊了沒反應」無從除錯
            dur = len(seg) / self.sample_rate
            self.discarded_count += 1
            self.last_discard_sec = dur
            self._discarded.append(dur)
        self._seg, self._seg_len = [], 0
        self._silence_len = 0
        self._speech_len = 0
        self._flag_ring.clear()
        self._speaking = False
        self.vad.reset()
        return seg if ok else None

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        if self.agc is not None and len(x):
            x = self.agc.process(x)
        if len(self._carry):
            x = np.concatenate([self._carry, x])
            self._carry = np.zeros(0, dtype=np.float32)
        done: list[np.ndarray] = []
        w = self.vad.window_samples
        n_full = (len(x) // w) * w
        self._carry = x[n_full:].copy()  # 不足一窗留待下批，保證窗對齊（mic/檔一致）
        for i in range(0, n_full, w):
            win = x[i : i + w]
            speech = self.vad.is_speech(win)
            if not self._speaking:
                self._push_pre(win)
                if speech:
                    self._speaking = True
                    self._seg = self._take_pre()
                    self._seg_len = sum(len(p) for p in self._seg)
                    # _speech_len 只計真正判為語音的窗，不含 pre_roll 靜音。
                    self._speech_len = len(win)
                    self._silence_len = 0
                    self._flag_ring.clear()
            else:
                self._seg.append(win.copy())
                self._seg_len += len(win)
                if speech:
                    self._speech_len += len(win)
                    self._silence_len = 0
                else:
                    self._silence_len += len(win)
                # 端點：滑動窗（最近 silence_sec 的窗）語音窗占比 ≤ 上限即斷句。
                self._flag_ring.append(bool(speech))
                if len(self._flag_ring) >= self._ring_max and \
                        sum(self._flag_ring) <= max(1, math.ceil(self._speech_frac_max * self._ring_max)):
                    seg = self._emit(trim_silence=True)
                    if seg is not None:
                        done.append(seg)
                elif self._seg_len >= self.max_turn_samples:
                    # 超長強制切：不裁靜音（還在說話），保留連續
                    seg = self._emit(trim_silence=False)
                    if seg is not None:
                        done.append(seg)
                    self._speaking = True  # 繼續收下一段（VAD 已 reset）
        return done

    def flush(self) -> list[np.ndarray]:
        # 把殘 sample 補零湊一窗走完狀態機，避免尾音遺失
        if len(self._carry):
            w = self.vad.window_samples
            win = np.concatenate(
                [self._carry, np.zeros(w - len(self._carry), dtype=np.float32)])
            self._carry = np.zeros(0, dtype=np.float32)
            speech = self.vad.is_speech(win)
            if not self._speaking:
                self._push_pre(win)
                if speech:
                    self._speaking = True
                    self._seg = self._take_pre()
                    self._seg_len = sum(len(p) for p in self._seg)
                    self._speech_len = len(win)
                    self._silence_len = 0
            else:
                self._seg.append(win.copy())
                self._seg_len += len(win)
                if speech:
                    self._speech_len += len(win)
                else:
                    self._silence_len += len(win)
        if self._speaking and self._seg:
            seg = self._emit(trim_silence=False)
            self.reset()
            return [seg] if seg is not None else []
        self.reset()
        return []


def segment_samples(
    samples: np.ndarray,
    vad: VadWrapper,
    sample_rate: int = 16000,
    **seg_kwargs,
) -> list[np.ndarray]:
    """整段音訊走與 mic 相同的 feed() 路徑切段（供整檔處理 / 測試，保證連貫一致）。"""
    seg = SpeechSegmenter(vad, sample_rate=sample_rate, **seg_kwargs)
    out: list[np.ndarray] = []
    chunk = vad.window_samples * 16  # 每次餵 16 窗，減少迴圈開銷
    x = np.asarray(samples, dtype=np.float32).reshape(-1)
    for i in range(0, len(x), chunk):
        out.extend(seg.feed(x[i : i + chunk]))
    out.extend(seg.flush())
    return out
