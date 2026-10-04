"""單程序 Memory 更新事件，供快取失效與診斷使用。"""
from collections import deque

_revision = 0
_recent_events: deque[dict] = deque(maxlen=100)


def publish_memory_event(kind: str, event_id: str, turn_id: str, **changes) -> dict:
    global _revision
    _revision += 1
    event = {"type": kind, "event_id": event_id, "turn_id": turn_id,
             "revision": _revision, **changes}
    _recent_events.append(event)
    return event


def recent_memory_events() -> list[dict]:
    return list(_recent_events)
