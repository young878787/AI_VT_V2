"""Shared text segmentation and bounded text-only speech timing."""

import math


_STRONG_END = "。！？!?\n\r"
_WEAK_PAUSE = "，、：；—…,;:"


class SentenceSplitter:
    """LLM 串流增量的切句緩衝：feed(增量) → 完整句列表；flush() 收尾。"""

    def __init__(self, min_chars: int = 6, max_chars: int = 40) -> None:
        self._buf = ""
        self._min = min_chars
        self._max = max_chars

    def _emit(self, force: bool = False) -> list[str]:
        out = []
        while self._buf:
            cut = -1
            for index, char in enumerate(self._buf):
                if char in _STRONG_END:
                    cut = index + 1
                    break
                if (not force and self._min <= index + 1 < self._max and char in _WEAK_PAUSE):
                    cut = index + 1
                    break
                if index + 1 >= self._max:
                    cut = index + 1
                    break
            if cut < 0:
                break
            sentence = self._buf[:cut].strip()
            self._buf = self._buf[cut:]
            if sentence:
                out.append(sentence)
        return out

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        return self._emit()

    def flush(self) -> list[str]:
        remaining = [self._buf.strip()] if self._buf.strip() else []
        self._buf = ""
        return remaining


def split_speech_segments(text: str) -> list[str]:
    """Keep short clauses together; bound long sentences without another tokenizer."""
    splitter = SentenceSplitter(min_chars=24, max_chars=80)
    return splitter.feed(text) + splitter.flush()


def estimate_speech_segments(text: str, speaking_rate: float = 1.0) -> list[dict]:
    """A 4–8 second expression timeline, explicitly not measured audio alignment."""
    segments = split_speech_segments(text)
    if not segments:
        return []
    rate = speaking_rate
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate):
        rate = 1.0
    rate = max(0.65, min(1.6, rate))
    weights = [max(1, len(segment)) for segment in segments]
    total = sum(weights)
    duration_ms = max(4000, min(8000, round(total * 95 / rate + 650)))
    timing = []
    consumed = 0
    for segment_id, weight in enumerate(weights):
        start_ms = round(consumed / total * duration_ms, 3)
        consumed += weight
        timing.append({"id": segment_id, "startMs": start_ms,
                       "endMs": round(consumed / total * duration_ms, 3)})
    return timing
