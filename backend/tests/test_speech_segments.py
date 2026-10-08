"""Shared segmentation and Piper sample timing without loading ONNX models."""

import io
import pathlib
import sys
import unittest
import wave
from types import SimpleNamespace

import numpy as np


BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from domain.speech_segments import estimate_speech_segments, split_speech_segments
from infrastructure.piper_tts import PiperTTS


class SpeechSegmentsTests(unittest.TestCase):
    def test_segmentation_keeps_short_clauses_and_decimal_together(self):
        text = "你好呀，今天溫度 23.5 度。\n我們一起出去吧！最後一句"
        self.assertEqual(split_speech_segments(text), ["你好呀，今天溫度 23.5 度。", "我們一起出去吧！", "最後一句"])
        self.assertEqual(split_speech_segments(" \n  "), [])

    def test_long_segments_are_bounded_without_losing_text(self):
        text = "這是沒有句號的長回覆" * 30
        segments = split_speech_segments(text)
        self.assertEqual("".join(segments), text)
        self.assertTrue(all(0 < len(segment) <= 80 for segment in segments))

    def test_estimates_share_segment_ids_are_contiguous_and_bounded(self):
        for text in ("好。", "先說第一段。接著是較長一些的第二段。" * 15):
            with self.subTest(text=text[:10]):
                segments = split_speech_segments(text)
                timing = estimate_speech_segments(text)
                self.assertEqual([item["id"] for item in timing], list(range(len(segments))))
                self.assertEqual(timing[0]["startMs"], 0)
                self.assertGreaterEqual(timing[-1]["endMs"], 4000)
                self.assertLessEqual(timing[-1]["endMs"], 8000)
                for left, right in zip(timing, timing[1:]):
                    self.assertEqual(left["endMs"], right["startMs"])
                self.assertTrue(all(item["endMs"] > item["startMs"] for item in timing))
        self.assertEqual(estimate_speech_segments(""), [])

    def test_estimate_rate_changes_duration_inside_bounds(self):
        text = "可以讓我慢慢說明這個想法，我會分幾段告訴你，先聊聊事情的起因，再聊後來發生的改變。"
        self.assertGreater(estimate_speech_segments(text, 0.7)[-1]["endMs"],
                           estimate_speech_segments(text, 1.4)[-1]["endMs"])
        self.assertEqual(estimate_speech_segments(text, float("nan")), estimate_speech_segments(text))


class PiperSegmentTimingTests(unittest.TestCase):
    def voice(self, chunks):
        voice = object.__new__(PiperTTS)
        voice.sample_rate = 22050
        voice._syn_config = lambda scale: scale
        calls = []

        def synthesize(text, syn_config):
            calls.append((text, syn_config))
            for array in chunks[text]:
                yield SimpleNamespace(audio_float_array=np.asarray(array, dtype=np.float32))

        voice.voice = SimpleNamespace(synthesize=synthesize)
        return voice, calls

    def test_multiple_inner_chunks_belong_to_one_shared_segment(self):
        voice, calls = self.voice({
            "第一句。": [np.full(11025, 0.1), np.full(22050, 0.2)],
            "第二句。": [np.full(22050, 0.3)],
        })
        audio, sample_rate, segments = voice.synthesize_to_wav_with_segments("第一句。第二句。", 0.8)
        self.assertEqual(calls, [("第一句。", 0.8), ("第二句。", 0.8)])
        self.assertEqual(sample_rate, 22050)
        self.assertEqual(segments, [{"id": 0, "startMs": 0, "endMs": 1500},
                                    {"id": 1, "startMs": 1500, "endMs": 2500}])
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (22050, 1, 2))
            self.assertEqual(wav.getnframes(), 55125)
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16)
        self.assertAlmostEqual(pcm[0] / 32767, 0.1, places=4)
        self.assertAlmostEqual(pcm[11025] / 32767, 0.2, places=4)
        self.assertAlmostEqual(pcm[33075] / 32767, 0.3, places=4)

    def test_timing_accumulates_samples_instead_of_rounded_chunk_durations(self):
        voice, calls = self.voice({"短句。": [np.zeros(10001)], "再一句。": [np.zeros(10001)]})
        audio, _, segments = voice.synthesize_to_wav_with_segments("短句。再一句。")
        self.assertEqual(len(calls), 2)
        self.assertEqual(segments[0]["endMs"], round(10001 / 22050 * 1000, 3))
        self.assertEqual(segments[-1]["endMs"], round(20002 / 22050 * 1000, 3))
        with wave.open(io.BytesIO(audio), "rb") as wav:
            self.assertEqual(wav.getnframes(), 20002)

    def test_empty_audio_keeps_source_ids_and_never_fabricates_timing(self):
        voice, _ = self.voice({"…": [], "有聲音。": [np.zeros(2205)]})
        audio, _, segments = voice.synthesize_to_wav_with_segments("…\n有聲音。")
        self.assertTrue(audio)
        self.assertEqual(segments, [{"id": 1, "startMs": 0, "endMs": 100}])
        self.assertEqual(voice.synthesize_to_wav_with_segments("…"), (b"", 22050, []))


if __name__ == "__main__":
    unittest.main()
