import json
from functools import lru_cache
from typing import Optional

from anthropic import Anthropic

from config import get_settings
from schemas import ChatMessage, ResellerAnalyzeIn, ResellerAnalyzeOut


@lru_cache
def _client() -> Anthropic:
    s = get_settings()
    if not s.anthropic_api_key:
        raise RuntimeError("ANTHROPIC_API_KEY должен быть задан")
    return Anthropic(api_key=s.anthropic_api_key)


DEFAULT_SYSTEM = (
    "Ты — ассистент маркетплейса пищевого оборудования. "
    "Помогаешь покупателям подобрать технику для ресторанов, пекарен и кафе, "
    "продавцам — оценить и описать оборудование, а перекупщикам — оценить "
    "рыночный потенциал. Отвечай кратко, по делу, на русском языке."
)


def chat(
    messages: list[ChatMessage],
    *,
    system: Optional[str] = None,
    max_tokens: int = 1024,
) -> tuple[str, Optional[str]]:
    resp = _client().messages.create(
        model=get_settings().anthropic_model,
        max_tokens=max_tokens,
        system=system or DEFAULT_SYSTEM,
        messages=[{"role": m.role, "content": m.content} for m in messages],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    return text, resp.stop_reason


RESELLER_SYSTEM = (
    "Ты — эксперт по перепродаже б/у пищевого оборудования (рестораны, пекарни, "
    "кафе, фастфуд) на рынках СНГ и Восточной Европы. "
    "Оцениваешь оборудование с точки зрения перекупщика: какую цену стоит "
    "предлагать продавцу и за сколько реально перепродать. "
    "Учитывай состояние, год, бренд, износ, ликвидность категории и логистику. "
    "Возвращай ТОЛЬКО валидный JSON без markdown-обёрток и комментариев."
)

RESELLER_SCHEMA_HINT = """Верни JSON строго в формате:
{
  "recommended_buy_price": число,
  "estimated_resale_price": число,
  "estimated_margin": число,
  "margin_percent": число,
  "confidence": "low" | "medium" | "high",
  "rationale": "строка с обоснованием",
  "risks": ["строка", "..."],
  "suggested_actions": ["строка", "..."]
}
Все цены в той же валюте, что и asking_price. margin = resale - buy.
margin_percent = margin / buy * 100, округлять до 1 знака."""


def analyze_for_reseller(item: ResellerAnalyzeIn) -> ResellerAnalyzeOut:
    payload = item.model_dump()
    user_msg = (
        f"Оборудование для оценки:\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n\n"
        f"{RESELLER_SCHEMA_HINT}"
    )
    resp = _client().messages.create(
        model=get_settings().anthropic_model,
        max_tokens=1500,
        system=RESELLER_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    data = json.loads(text)
    return ResellerAnalyzeOut(**data)
