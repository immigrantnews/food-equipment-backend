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


AVITO_EVAL_SYSTEM = """Ты эксперт по рынку б/у пищевого оборудования России.
Анализируешь объявления с Авито для перекупщиков пищевого оборудования.

ВАЖНО при анализе:
1. Внимательно читай заголовок и описание — модель и бренд часто указаны явно (MAC.PAN SV 130, Apach ATR 20, Rational SCC WE 101)
2. Определи новое это или б/у — если продаёт дилер/компания или написано "новое" — condition=new, verdict=new_item, reseller_margin=0
3. Ищи признаки срочности: "срочно", "быстро", "в связи с закрытием", "банкротство", "ликвидация", "уезжаю", "закрываемся" → urgency=urgent или liquidation
4. Ищи оптовые лоты: "несколько", "партия", "комплект", "линия", "цех", "весь ресторан", числа штук → bulk_opportunity=true
5. Учитывай регион — Москва/СПб дороже регионов на 20-30%
6. Учитывай год выпуска если указан — старше 10 лет дешевле на 30-50%

Верни ТОЛЬКО валидный JSON без markdown:
{
  "category": "тип оборудования на русском",
  "brand": "бренд из текста или null",
  "model": "модель из текста или null",
  "year": "год выпуска или null",
  "condition": "new/used/unknown",
  "market_min": минимальная рыночная цена руб,
  "market_max": максимальная рыночная цена руб,
  "verdict": "green/yellow/red/flash/new_item",
  "reseller_margin": прибыль перекупщика руб,
  "turnover_days": "X-Y дней",
  "demand": "high/medium/low",
  "urgency": "normal/urgent/liquidation",
  "lot_type": "single/bulk/full_workshop",
  "bulk_opportunity": false,
  "notification_reason": "причина уведомления или null",
  "comment": "один совет перекупщику"
}

Вердикт:
- new_item = новое от дилера
- flash = цена ниже рынка более чем на 35%
- green = цена ниже рынка на 15-35%
- yellow = цена в рынке ±15%
- red = цена выше рынка более чем на 15%"""


def evaluate_avito_listing(title: str, price: int, region: str, description: str, photos: list = []) -> dict:
    user_msg = f"Заголовок: {title}\nЦена: {price} ₽\nРегион: {region}\nОписание: {description[:300]}"
    resp = _client().messages.create(
        model=get_settings().anthropic_model,
        max_tokens=600,
        system=AVITO_EVAL_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    result = json.loads(text)

    import datetime
    record = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "title": title,
        "price": price,
        "region": region,
        "category": result.get("category"),
        "brand": result.get("brand"),
        "model": result.get("model"),
        "verdict": result.get("verdict")
    }
    data_file = "/root/food-equipment-backend/price_data.jsonl"
    try:
        with open(data_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return result
