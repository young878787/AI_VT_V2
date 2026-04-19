"""
Display WebSocket 連線管理（供 /ws/display 訂閱者使用）。
以 module-level set 作為單例，確保 chat_ws 與 display_ws 共享同一份連線集合。
"""
_display_connections: set = set()


async def broadcast_to_displays(data: dict) -> None:
    """向所有已連線的 /ws/display 客戶端廣播行為數據（fire-and-forget）。"""
    dead: set = set()
    for ws in list(_display_connections):
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    _display_connections.difference_update(dead)
