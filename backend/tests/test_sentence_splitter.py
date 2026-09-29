"""SentenceSplitter 切句測試（port 自 voice_txt/tests/test_tts_smoke_manual.py）。"""
import pathlib
import sys
import unittest

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from infrastructure.piper_tts import SentenceSplitter


class SentenceSplitterTests(unittest.TestCase):
    def test_streaming_split_and_flush(self):
        sp = SentenceSplitter()
        out: list[str] = []
        out += sp.feed("你好呀，我是小霓。今天天")
        out += sp.feed("氣很好！我們一起去")
        out += sp.flush()
        # min=6：「你好呀，」(4字) 併入下個切點，遇「。」整句切出
        self.assertEqual(out, ["你好呀，我是小霓。", "今天天氣很好！", "我們一起去"])

    def test_decimal_not_split(self):
        sp = SentenceSplitter()
        got = sp.feed("溫度 3.5 度") + sp.flush()
        self.assertEqual(got, ["溫度 3.5 度"])

    def test_max_chars_hard_cut(self):
        sp = SentenceSplitter(max_chars=10)
        got = sp.feed("一二三四五六七八九十十一十二十三") + sp.flush()
        self.assertTrue(got)
        for s in got:
            self.assertLessEqual(len(s), 10)

    def test_flush_returns_remaining_and_empties_buffer(self):
        sp = SentenceSplitter()
        self.assertEqual(sp.feed("還沒說完的話"), [])
        self.assertEqual(sp.flush(), ["還沒說完的話"])
        self.assertEqual(sp.flush(), [])


if __name__ == "__main__":
    unittest.main()
