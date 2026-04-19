"""
AI 客戶端：OpenAI 相容客戶端初始化、extra_body 組裝、含後備模型的呼叫包裝。
"""
import os
from openai import AsyncOpenAI

from core.config import AI_PROVIDER, API_KEY, BASE_URL, MODEL_NAME, FALLBACK_MODEL
from core.utils import env_flag

# 初始化 OpenAI 相容客戶端（OpenRouter / Nvidia / Google AI Studio / DashScope）
client: AsyncOpenAI = AsyncOpenAI(
    base_url=BASE_URL,
    api_key=API_KEY,
)


def _build_extra_body() -> dict:
    """
    建構傳給 API 的額外參數（extra_body）。
    - Nvidia Qwen3 系列需要 chat_template_kwargs: {enable_thinking: True}
      才能啟用 Chain-of-Thought 推理模式。
    - DashScope Qwen3 系列需要頂層 enable_thinking: True（欄位路徑不同於 Nvidia）。
    - OpenRouter 與 Google AI Studio 不需要額外參數。
    """
    if AI_PROVIDER == "nvidia" and env_flag("AI_ENABLE_THINKING", False):
        return {"chat_template_kwargs": {"enable_thinking": True}}
    if AI_PROVIDER == "qwen" and env_flag("AI_ENABLE_THINKING", False):
        extra: dict = {"enable_thinking": True}
        effort = os.getenv("QWEN_REASONING_EFFORT")
        if effort:
            extra["reasoning_effort"] = effort.lower()
        return extra
    return {}


# 預先計算（啟動時固定，不需每次呼叫重建）
EXTRA_BODY: dict = _build_extra_body()


async def chat_create_with_fallback(**kwargs) -> object:
    """
    包裝 client.chat.completions.create()。
    若主模型呼叫失敗且設有後備模型（FALLBACK_MODEL），
    自動切換 model= 重試一次。僅 qwen provider 有後備模型，
    其他 provider 的 FALLBACK_MODEL 為 None，行為等同直接呼叫。
    """
    try:
        return await client.chat.completions.create(**kwargs)
    except Exception as e:
        if FALLBACK_MODEL and kwargs.get("model") != FALLBACK_MODEL:
            print(f"[Fallback] 主模型失敗 ({e})，切換至後備模型: {FALLBACK_MODEL}")
            fallback_kwargs = {**kwargs, "model": FALLBACK_MODEL}
            return await client.chat.completions.create(**fallback_kwargs)
        raise
