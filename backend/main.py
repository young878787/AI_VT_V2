"""
應用程式入口：建立 FastAPI App、掛載 Middleware 與 Router、啟動 uvicorn。
所有業務邏輯已移至各層模組，此檔案只做組裝與啟動。
"""
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes.models_router import router as models_router
from api.routes.chat_ws import router as chat_router
from api.routes.display_ws import router as display_router
from api.routes.expression_debug_router import router as expression_debug_router
from api.routes.memory_router import router as memory_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(models_router)
app.include_router(chat_router)
app.include_router(display_router)
app.include_router(expression_debug_router)
app.include_router(memory_router)


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("BACKEND_PORT", 9999))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
