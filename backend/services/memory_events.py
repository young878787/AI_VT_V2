"""單程序 Memory 更新事件，供快取失效與診斷使用。"""
from collections import deque
from collections.abc import Callable

_revision = 0
_recent_events: deque[dict] = deque(maxlen=100)
_subscribers: set[Callable[[dict], None]] = set()


def subscribe_memory_events(callback: Callable[[dict], None]) -> Callable[[], None]:
    """訂閱本程序記憶事件；回傳解除訂閱函式。"""
    _subscribers.add(callback)

    def unsubscribe() -> None:
        _subscribers.discard(callback)

    return unsubscribe


def publish_memory_event(kind: str, event_id: str, turn_id: str, **changes) -> dict:
    global _revision
    _revision += 1
    event = {"type": kind, "event_id": event_id, "turn_id": turn_id,
             "revision": _revision, **changes}
    _recent_events.append(event)
    for callback in tuple(_subscribers):
        try:
            callback(event)
        except Exception:
            # UI 訊息訂閱失敗不能影響記憶交易。
            pass
    return event


def recent_memory_events() -> list[dict]:
    return list(_recent_events)
