"""
全域設定：載入 .env、AI Provider 驗證、所有常數。
此模組在匯入時即執行驗證，啟動失敗時會立即 raise RuntimeError。
"""
import os
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
# AI Provider 設定（從 .env 讀取）
# ============================================================
AI_PROVIDER: str = os.getenv("AI_PROVIDER", "").lower().strip()

_PROVIDER_CONFIG: dict = {
    "openrouter": {
        "api_key_env": "OPENROUTER_API_KEY",
        "base_url_env": "OPENROUTER_BASE_URL",
        "model_env": "OPENROUTER_MODEL_NAME",
        "base_url_default": "https://openrouter.ai/api/v1",
        "model_default": "nvidia/nemotron-3-super-120b-a12b:free",
    },
    "nvidia": {
        "api_key_env": "NVIDIA_API_KEY",
        "base_url_env": "NVIDIA_BASE_URL",
        "model_env": "NVIDIA_MODEL_NAME",
        "base_url_default": "https://integrate.api.nvidia.com/v1",
        "model_default": "meta/llama-3.3-70b-instruct",
    },
    "google": {
        "api_key_env": "GOOGLE_API_KEY",
        "base_url_env": "GOOGLE_BASE_URL",
        "model_env": "GOOGLE_MODEL_NAME",
        "base_url_default": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model_default": "gemini-2.0-flash",
    },
    "qwen": {
        "api_key_env": "QWEN_API_KEY",
        "base_url_env": "QWEN_BASE_URL",
        "model_env": "QWEN_MODEL_NAME",
        "base_url_default": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "model_default": "qwen-plus",
    },
}

if not AI_PROVIDER:
    raise RuntimeError(
        "AI_PROVIDER 未設定，請在 .env 設定 AI_PROVIDER=nvidia | openrouter | google | qwen"
    )

if AI_PROVIDER not in _PROVIDER_CONFIG:
    raise RuntimeError(
        f"未知的 AI_PROVIDER='{AI_PROVIDER}'。支援值: {', '.join(_PROVIDER_CONFIG.keys())}"
    )

_cfg = _PROVIDER_CONFIG[AI_PROVIDER]

API_KEY: str = os.getenv(_cfg["api_key_env"], "")
BASE_URL: str = os.getenv(_cfg["base_url_env"], _cfg["base_url_default"])
MODEL_NAME: str = os.getenv(_cfg["model_env"], _cfg["model_default"])

# 後備模型（僅 qwen provider 使用；其他 provider 設為 None）
FALLBACK_MODEL: str | None = None
if AI_PROVIDER == "qwen":
    FALLBACK_MODEL = os.getenv("QWEN_FALLBACK_MODEL_NAME") or None

if not API_KEY:
    raise RuntimeError(f"{_cfg['api_key_env']} 未設定，請檢查 .env 檔案")

_base_url_lc = BASE_URL.lower()
if AI_PROVIDER == "nvidia" and "openrouter.ai" in _base_url_lc:
    raise RuntimeError(
        "AI_PROVIDER=nvidia 但 BASE_URL 指向 OpenRouter，請檢查 NVIDIA_BASE_URL 設定"
    )
if AI_PROVIDER == "openrouter" and "nvidia.com" in _base_url_lc:
    raise RuntimeError(
        "AI_PROVIDER=openrouter 但 BASE_URL 指向 NVIDIA，請檢查 OPENROUTER_BASE_URL 設定"
    )
if AI_PROVIDER == "google" and (
    "openrouter.ai" in _base_url_lc or "nvidia.com" in _base_url_lc
):
    raise RuntimeError(
        "AI_PROVIDER=google 但 BASE_URL 指向 OpenRouter/NVIDIA，請檢查 GOOGLE_BASE_URL 設定"
    )
if AI_PROVIDER != "google" and "googleapis.com" in _base_url_lc:
    raise RuntimeError(
        f"AI_PROVIDER={AI_PROVIDER} 但 BASE_URL 指向 Google，請檢查 {_cfg['base_url_env']} 設定"
    )
if AI_PROVIDER == "qwen" and (
    "openrouter.ai" in _base_url_lc
    or "nvidia.com" in _base_url_lc
    or "googleapis.com" in _base_url_lc
):
    raise RuntimeError(
        "AI_PROVIDER=qwen 但 BASE_URL 指向非 DashScope 端點，請檢查 QWEN_BASE_URL 設定"
    )
if AI_PROVIDER != "qwen" and "dashscope" in _base_url_lc:
    raise RuntimeError(
        f"AI_PROVIDER={AI_PROVIDER} 但 BASE_URL 指向 DashScope，請檢查 {_cfg['base_url_env']} 設定"
    )

print(f"[AI Provider] {AI_PROVIDER.upper()} | Model: {MODEL_NAME} | URL: {BASE_URL}")
if FALLBACK_MODEL:
    print(f"[AI Provider] Fallback model: {FALLBACK_MODEL}")

# ============================================================
# 記憶系統路徑常數
# ============================================================
MEMORY_DIR: str = os.path.join(_BACKEND_DIR, "memory")
USER_PROFILE_PATH: str = os.path.join(MEMORY_DIR, "user_profile.json")
MEMORY_MD_PATH: str = os.path.join(MEMORY_DIR, "memory.md")
CHAT_SESSION_DIR: str = os.path.join(MEMORY_DIR, "sessions")

# ============================================================
# Model Registry 路徑常數
# ============================================================
RESOURCES_DIR: str = os.path.abspath(
    os.path.join(_BACKEND_DIR, "..", "vtuber-web-app", "public", "Resources")
)
MODEL_REGISTRY_PATH: str = os.path.join(_BACKEND_DIR, "model_registry.json")

# ============================================================
# 對話持久化設定
# ============================================================
CHAT_PERSISTENCE_ENABLED: bool = env_flag("CHAT_PERSISTENCE_ENABLED", False)
CHAT_PERSISTENCE_MAX_MESSAGES: int = int(os.getenv("CHAT_PERSISTENCE_MAX_MESSAGES", "80"))

# ============================================================
# Context 壓縮閾值
# ============================================================
COMPRESS_TOKEN_THRESHOLD: int = 230_000
COMPRESS_KEEP_RECENT: int = 20
