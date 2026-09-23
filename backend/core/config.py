"""
全域設定：載入 .env、AI 路線驗證、所有常數。
此模組在匯入時即執行驗證，啟動失敗時會立即 raise RuntimeError。
"""
import os
from urllib.parse import urlparse

from dotenv import load_dotenv

from core.utils import env_flag

# ============================================================
# 載入環境變數
# ============================================================
# config.py 位於 backend/core/config.py，.env 在 backend/ 上一層
_BACKEND_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH: str = os.path.abspath(os.path.join(_BACKEND_DIR, "..", ".env"))
load_dotenv(dotenv_path=ENV_PATH, override=True)
print(f"[ENV] Loaded from: {ENV_PATH}")

# ============================================================
# AI 路線設定（從 .env 讀取）
# ============================================================
def provider_from_url(base_url: str) -> str:
    """辨識已知端點需要的特殊請求參數；自訂端點使用標準格式。"""
    host = (urlparse(base_url).hostname or "").lower()
    if host == "integrate.api.nvidia.com":
        return "nvidia"
    if host in {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    } or host.endswith(".dashscope.aliyuncs.com"):
        return "qwen"
    if host == "openrouter.ai":
        return "openrouter"
    if host == "generativelanguage.googleapis.com":
        return "google"
    if host == "api.openai.com":
        return "openai"
    return "custom"


def role_model_config(role: str) -> tuple[str, str, str, str]:
    """各路線直接使用自己的 key、URL、model，缺少任一項即報錯。"""
    api_key = (os.getenv(f"{role}_AI_API_KEY") or "").strip()
    base_url = (os.getenv(f"{role}_AI_BASE_URL") or "").strip()
    model = (os.getenv(f"{role}_AI_MODEL") or "").strip()
    for name, value in (("API_KEY", api_key), ("BASE_URL", base_url), ("MODEL", model)):
        if not value:
            raise RuntimeError(f"{role}_AI_{name} 未設定，請檢查 .env 檔案")
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        raise RuntimeError(f"{role}_AI_BASE_URL 必須是有效的 HTTP URL")
    return provider_from_url(base_url), api_key, base_url, model


CHAT_PROVIDER, CHAT_API_KEY, CHAT_BASE_URL, CHAT_MODEL_NAME = role_model_config("CHAT")
MEMORY_PROVIDER, MEMORY_API_KEY, MEMORY_BASE_URL, MEMORY_MODEL_NAME = role_model_config("MEMORY")
CHAT_CONTEXT_TOKEN_BUDGET: int = max(512, int(os.getenv("CHAT_CONTEXT_TOKEN_BUDGET", "8192")))

FALLBACK_MODEL: str | None = os.getenv("QWEN_FALLBACK_MODEL_NAME") or None

# ============================================================
# 記憶系統路徑常數
# ============================================================
MEMORY_DIR: str = os.path.abspath(os.getenv("AI_VT_MEMORY_DIR") or os.path.join(_BACKEND_DIR, "memory"))
USER_PROFILE_PATH: str = os.path.join(MEMORY_DIR, "user_profile.json")
MEMORY_MD_PATH: str = os.path.join(MEMORY_DIR, "memory.md")
CHAT_SESSION_DIR: str = os.path.join(MEMORY_DIR, "sessions")
EMOTION_STATE_DIR: str = os.path.join(MEMORY_DIR, "emotion_states")

# ============================================================
# Model Registry 路徑常數
# ============================================================
RESOURCES_DIR: str = os.path.abspath(
    os.path.join(_BACKEND_DIR, "..", "vtuber-web-app", "public", "Resources")
)
MODEL_REGISTRY_PATH: str = os.path.join(_BACKEND_DIR, "model_registry.json")

# ============================================================
# 語音管線設定（ASR 輸入 / TTS 輸出；引擎 port 自 voice_txt）
# ============================================================
ASR_ENABLED: bool = env_flag("ASR_ENABLED", False)
ASR_MODEL_DIR: str = os.getenv(
    "ASR_MODEL_DIR", os.path.join(_BACKEND_DIR, "models", "x-asr-zh-tw-en-streaming-ft75m")
)
VAD_MODEL_DIR: str = os.getenv("VAD_MODEL_DIR", os.path.join(_BACKEND_DIR, "models", "silero-vad"))
ASR_SAMPLE_RATE: int = int(os.getenv("ASR_SAMPLE_RATE", "16000"))
ASR_USE_AGC: bool = env_flag("ASR_USE_AGC", True)
ASR_SILENCE_SEC: float = float(os.getenv("ASR_SILENCE_SEC", "1.2"))

# TTS 輸出引擎：piper（本地串流）| google（既有整檔 Chirp3 路徑）
TTS_PROVIDER: str = os.getenv("TTS_PROVIDER", "google").lower().strip()
if TTS_PROVIDER not in ("piper", "google"):
    raise RuntimeError(f"未知的 TTS_PROVIDER='{TTS_PROVIDER}'。支援值: piper | google")
PIPER_MODEL_PATH: str = os.getenv(
    "PIPER_MODEL_PATH", os.path.join(_BACKEND_DIR, "models", "zh_TW-multi-voice.onnx")
)
PIPER_SPEAKER_ID: int = int(os.getenv("PIPER_SPEAKER_ID", "1"))
PIPER_LENGTH_SCALE: float = float(os.getenv("PIPER_LENGTH_SCALE", "1.0"))

# ============================================================
# 對話持久化設定
# ============================================================
CHAT_PERSISTENCE_ENABLED: bool = env_flag("AI_VT_TEST_MODE", False) or env_flag("CHAT_PERSISTENCE_ENABLED", False)
CHAT_PERSISTENCE_MAX_MESSAGES: int = int(os.getenv("CHAT_PERSISTENCE_MAX_MESSAGES", "80"))

# ============================================================
# Jev（System One）情緒與表情決策設定
# JEV_AI_API_KEY 留空時沿用 OPENROUTER_API_KEY
# ============================================================
OPENROUTER_SYSTEMONE_URL: str = "https://openrouter.ai/api/v1/systemone"

JEV_AI_API_KEY: str = (os.getenv("JEV_AI_API_KEY") or os.getenv("OPENROUTER_API_KEY") or "").strip()
JEV_AI_BASE_URL: str = (os.getenv("JEV_AI_BASE_URL") or OPENROUTER_SYSTEMONE_URL).strip()
JEV_AI_MODEL: str = (os.getenv("JEV_AI_MODEL") or "jev-latest").strip() or "jev-latest"
JEV_TIMEOUT_SEC: float = float(os.getenv("JEV_TIMEOUT_SEC", "2.0"))
if not JEV_AI_API_KEY.strip():
    raise RuntimeError(
        "JEV_AI_API_KEY（或 OPENROUTER_API_KEY）未設定，JEV Emotion / Action 需要 OpenRouter System One"
    )

print(f"[AI Route] JEV Model: {JEV_AI_MODEL} | URL: {JEV_AI_BASE_URL}")
print(f"[AI Route] CHAT({CHAT_PROVIDER.upper()}) Model: {CHAT_MODEL_NAME} | URL: {CHAT_BASE_URL}")
print(f"[AI Route] MEMORY({MEMORY_PROVIDER.upper()}) Model: {MEMORY_MODEL_NAME} | URL: {MEMORY_BASE_URL}")

# ============================================================
# Context 壓縮閾值
# ============================================================
COMPRESS_TOKEN_THRESHOLD: int = 230_000
COMPRESS_KEEP_RECENT: int = 20
