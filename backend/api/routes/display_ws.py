"""
Display WebSocket 端點（OBS Browser Source 用）。
此端點只推送，不處理客戶端訊息；客戶端可發送任意文字保持連線。
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.display_manager import _display_connections

router = APIRouter()


@router.websocket("/ws/display")
async def display_endpoint(websocket: WebSocket):
    """
    供 /display 頁面（或 OBS Browser Source）連線，接收 AI 行為廣播。
    """
    await websocket.accept()
    _display_connections.add(websocket)
    try:
        while True:
            # 接收並忽略客戶端訊息（如 ping），維持連線存活
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[display ws] 連線異常斷線: {e}")
    finally:
        _display_connections.discard(websocket)
