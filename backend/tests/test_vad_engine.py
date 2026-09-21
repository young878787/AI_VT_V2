"""VAD 切段 / AutoGain 測試（純 CPU，不碰 mic/API/模型檔，強制能量 VAD）。

port 自 voice_txt/tests/test_vad.py 與 test_agc.py。
"""
import pathlib
import sys
import unittest

import numpy as np

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from infrastructure.vad_engine import AutoGain, SpeechSegmenter, VadWrapper, segment_samples

SR = 16000


def _sine(sec: float, freq: float = 440.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(SR * sec), dtype=np.float64) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _silence(sec: float) -> np.ndarray:
    return np.zeros(int(SR * sec), dtype=np.float32)


def _rms(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64).reshape(-1)
    return float(np.sqrt(np.mean(x ** 2)))


def _energy_vad(**kw) -> VadWrapper:
    kw.setdefault("force_energy", True)
    kw.setdefault("energy_threshold", 0.02)
    return VadWrapper(**kw)


class VadSegmenterTests(unittest.TestCase):
    def test_energy_vad_silence_vs_tone(self):
        vad = _energy_vad()
        self.assertFalse(vad.is_speech(_silence(0.1)[: vad.window_samples]))
        self.assertTrue(vad.is_speech(_sine(0.1)[: vad.window_samples]))

    def test_silence_only_yields_no_segment(self):
        vad = _energy_vad()
        segs = segment_samples(_silence(2.0), vad, SR)
        self.assertEqual(segs, [])

    def test_speech_silence_speech_yields_two_segments(self):
        vad = _energy_vad()
        audio = np.concatenate([_sine(1.0), _silence(1.5), _sine(1.0), _silence(1.0)])
        segs = segment_samples(audio, vad, SR, silence_sec=0.8,
                               min_speech_sec=0.3, pre_roll_sec=0.1, tail_pad_sec=0.1)
        self.assertEqual(len(segs), 2)
        for s in segs:
            # 每段約 1 秒語音 + 少量前後墊，落在合理區間
            self.assertTrue(0.8 < len(s) / SR < 1.8)

    def test_short_blip_filtered_as_noise(self):
        vad = _energy_vad()
        audio = np.concatenate([_sine(0.08), _silence(1.5)])  # 80ms 短雜訊
        segs = segment_samples(audio, vad, SR, silence_sec=0.5, min_speech_sec=0.3)
        self.assertEqual(segs, [])

    def test_max_turn_forces_split(self):
        vad = _energy_vad()
        audio = _sine(5.0)
        segs = segment_samples(audio, vad, SR, silence_sec=5.0, min_speech_sec=0.2,
                               max_turn_sec=2.0, pre_roll_sec=0.0, tail_pad_sec=0.0)
        self.assertGreaterEqual(len(segs), 2)
        total = sum(len(s) for s in segs) / SR
        self.assertLess(abs(total - 5.0), 0.5)

    def test_feed_and_flush_parity(self):
        """逐塊 feed 與整批 segment_samples 結果一致（mic/檔連貫）。"""
        vad1, vad2 = _energy_vad(), _energy_vad()
        audio = np.concatenate([_sine(0.8), _silence(1.2), _sine(0.8), _silence(1.0)])
        kw = dict(silence_sec=0.8, min_speech_sec=0.3, pre_roll_sec=0.1, tail_pad_sec=0.1)
        ref = segment_samples(audio, vad1, SR, **kw)
        seg = SpeechSegmenter(vad2, SR, **kw)
        out: list[np.ndarray] = []
        for i in range(0, len(audio), 1600):  # 100ms 塊，模擬 mic
            out.extend(seg.feed(audio[i:i + 1600]))
        out.extend(seg.flush())
        self.assertEqual(len(out), len(ref))
        self.assertEqual(len(ref), 2)
        for a, b in zip(out, ref):
            self.assertLessEqual(abs(len(a) - len(b)), vad1.window_samples)


class AutoGainTests(unittest.TestCase):
    def test_quiet_signal_gets_gain(self):
        agc = AutoGain(sample_rate=SR)
        quiet = _sine(1.0, amp=0.008)  # rms ≈ 0.0057，遠低於目標
        y = agc.process(quiet)
        self.assertGreater(agc.gain, 2.0)
        self.assertGreater(_rms(y), _rms(quiet) * 2.0)

    def test_loud_signal_untouched(self):
        agc = AutoGain(sample_rate=SR)
        loud = _sine(1.0, amp=0.4)  # rms ≈ 0.28 > 目標
        y = agc.process(loud)
        self.assertLessEqual(agc.gain, 1.0 + 1e-6)
        self.assertTrue(np.allclose(y, loud))

    def test_never_exceeds_full_scale(self):
        agc = AutoGain(sample_rate=SR)
        y = agc.process(_sine(1.0, amp=0.2))
        self.assertLessEqual(float(np.max(np.abs(y))), 1.0)

    def test_silence_at_startup_reaches_max_gain(self):
        """開頭純靜音也要把增益拉上去（讓之後第一個字就吃到增益）。"""
        agc = AutoGain(sample_rate=SR)
        for _ in range(10):  # 1s 靜音（100ms/塊，mic 節奏）
            agc.process(_silence(0.1))
        self.assertGreater(agc.gain, agc.max_gain * 0.9)

    def test_quiet_speech_segment_recovered(self):
        """能量 VAD（門檻 0.02）切不出的小聲句，經 AGC 後要切得出來。"""
        audio = np.concatenate([_silence(0.3), _sine(1.0, amp=0.008), _silence(1.5)])
        kw = dict(silence_sec=0.8, min_speech_sec=0.2, pre_roll_sec=0.3, tail_pad_sec=0.2)
        without = segment_samples(audio, _energy_vad(), SR, agc=None, **kw)
        with_agc = segment_samples(audio, _energy_vad(), SR, agc=AutoGain(sample_rate=SR), **kw)
        self.assertEqual(without, [])  # 無 AGC：小聲句整段切不出來
        self.assertGreaterEqual(len(with_agc), 1)
        self.assertTrue(0.8 <= len(with_agc[0]) / SR <= 2.5)

    def test_feed_chunked_matches_batch(self):
        """逐塊 feed（mic 模擬 100ms）與整批切段一致（AGC 在兩條路徑同樣生效）。"""
        audio = np.concatenate([_silence(0.3), _sine(0.9, amp=0.008), _silence(1.5)])
        kw = dict(silence_sec=0.8, min_speech_sec=0.2, pre_roll_sec=0.3, tail_pad_sec=0.2)
        ref = segment_samples(audio, _energy_vad(), SR, agc=AutoGain(sample_rate=SR), **kw)
        seg = SpeechSegmenter(_energy_vad(), SR, agc=AutoGain(sample_rate=SR), **kw)
        out: list[np.ndarray] = []
        for i in range(0, len(audio), 1600):
            out.extend(seg.feed(audio[i:i + 1600]))
        out.extend(seg.flush())
        self.assertGreaterEqual(len(out), 1)
        self.assertEqual(len(out), len(ref))
        for a, b in zip(out, ref):
            self.assertLessEqual(abs(len(a) - len(b)) / SR, 0.05)


if __name__ == "__main__":
    unittest.main()
