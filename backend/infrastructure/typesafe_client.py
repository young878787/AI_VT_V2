"""
TypeSafe System One（Jev）client：httpx 直打 OpenRouter /api/v1/systemone。

不引入 typesafe-sdk（省一個依賴）；請求/回應形狀與 TypeSafe 官方一致：
- 請求：{model, state, questions}
- 回應：{model, answers, usage}（OpenRouter 另附 id / provider / usage.cost）

呼叫失敗（網路/逾時/非 2xx/解析失敗）一律回 None，由呼叫端走 fallback。
"""
import httpx

from core.config import (
    JEV_AI_API_KEY,
    JEV_AI_BASE_URL,
    JEV_AI_MODEL,
    JEV_TIMEOUT_SEC,
)


async def call_jev(state: dict, questions: dict) -> dict | None:
    """送出 state + questions，回傳 answers dict；失敗回 None（交由呼叫端 fallback）。"""
    api_key = JEV_AI_API_KEY.strip()
    if not api_key:
        print("[Jev] JEV_AI_API_KEY 未設定，跳過 Jev 呼叫")
        return None

    payload = {"model": JEV_AI_MODEL, "state": state, "questions": questions}
    try:
        async with httpx.AsyncClient(timeout=JEV_TIMEOUT_SEC) as client:
            resp = await client.post(
                JEV_AI_BASE_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
    except httpx.TimeoutException:
        print(f"[Jev] 呼叫逾時（>{JEV_TIMEOUT_SEC}s），走 fallback")
        return None
    except httpx.HTTPStatusError as e:
        print(f"[Jev] HTTP 錯誤 {e.response.status_code}: {e.response.text[:200]}")
        return None
    except Exception as e:
        print(f"[Jev] 呼叫失敗: {e}")
        return None

    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict):
        print(f"[Jev] 回應缺少 answers 欄位: {str(body)[:200]}")
        return None
    return answers
