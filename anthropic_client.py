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
Тебе могут передать фотографии оборудования — внимательно изучи их.

АНАЛИЗ ФОТО (если есть):
1. Ищи шильдик, табличку с названием, наклейки с моделью — они обычно на боку или сзади
2. Читай любой текст на оборудовании — бренд, модель, серийный номер
3. Оцени визуальное состояние:
   - excellent: как новое, следов износа нет
   - good: рабочее, небольшой износ
   - fair: видимый износ, краска облупилась, требует ТО
   - poor: сильный износ, ржавчина, нужен ремонт
   - unknown: фото не позволяет оценить

АНАЛИЗ ТЕКСТА:
1. Внимательно читай заголовок и описание — модель и бренд часто указаны явно
2. Определи новое это или б/у — если продаёт дилер/компания или написано "новое" — condition=new
3. Ищи срочность: "срочно", "быстро", "закрытие", "банкротство", "ликвидация", "уезжаю"
4. Ищи оптовые лоты: "несколько", "партия", "комплект", "линия", "цех", "весь ресторан"
5. Учитывай регион — Москва/СПб дороже регионов на 20-30%
6. Учитывай год выпуска — старше 10 лет дешевле на 30-50%
7. Тип продавца: если company — это дилер/магазин, reseller_margin=0, verdict=new_item если новое. Если private — частное лицо, можно торговаться.

КОРРЕКТИРОВКА ЦЕНЫ ПО СОСТОЯНИЮ:
- excellent: +20% к рыночной цене
- good: рыночная цена
- fair: -20% к рыночной цене
- poor: -40% к рыночной цене

БУДЬ СКЕПТИКОМ:
- Не верь описанию продавца без проверки
- "Почти новое", "использовалась мало" — НЕ учитывай без фото шильдика, ставь condition_visual=unknown
- Рыночная цена это НЕ выгода — выгода только при скидке 15%+ от рынка
- В поле comment ВСЕГДА указывай конкретную цену, до которой стоит торговаться
- Если цена рыночная (verdict=yellow) — пиши прямо: "цена рыночная, смысл только если сторгуете до X ₽"

РАСЧЁТ МАРЖИ — СТРОГИЕ ПРАВИЛА:
- Маржа считается ТОЛЬКО если цена ниже медианы рынка минимум на 15%
- Медиана = (market_min + market_max) / 2
- Если цена >= медианы — reseller_margin = 0, это не выгодная сделка
- Если цена в верхней трети диапазона (выше 70% от market_max) — verdict = red, margin = 0
- Реальная маржа = цена перепродажи - цена покупки - 10% на расходы (доставка, хранение, время)
- Цена перепродажи = не выше медианы рынка (покупатель не заплатит больше среднего)
- Пример: рынок 600 000-950 000, медиана 775 000, цена 908 000 → выше медианы → margin=0, verdict=red
- Пример: рынок 600 000-950 000, медиана 775 000, цена 550 000 → ниже на 29% → margin=(775000-550000)*0.9=202500, verdict=green

Верни ТОЛЬКО валидный JSON без markdown:
{
  "category": "тип оборудования на русском",
  "brand": "бренд из текста или фото или null",
  "model": "модель из текста или фото или null",
  "year": "год выпуска или null",
  "condition": "new/used/unknown",
  "condition_visual": "excellent/good/fair/poor/unknown",
  "market_min": минимальная рыночная цена руб с учётом состояния,
  "market_max": максимальная рыночная цена руб с учётом состояния,
  "verdict": "green/yellow/red/flash/new_item",
  "reseller_margin": прибыль перекупщика руб,
  "turnover_days": "X-Y дней",
  "demand": "high/medium/low",
  "urgency": "normal/urgent/liquidation",
  "lot_type": "single/bulk/full_workshop",
  "bulk_opportunity": false,
  "notification_reason": "причина уведомления или null",
  "comment": "совет перекупщику с обязательным указанием цены, до которой торговаться"
}

Вердикт:
- new_item = новое от дилера
- flash = цена ниже рынка более чем на 35%
- green = цена ниже рынка на 15-35%
- yellow = цена в рынке ±15%
- red = цена выше рынка более чем на 15%"""


def evaluate_avito_listing(title: str, price: int, region: str, description: str, photos: list = [], seller_type: str = "unknown") -> dict:
    content = []

    # photos are now base64 JPEG strings (already compressed in the extension)
    for b64 in photos[:3]:
        if not b64:
            continue
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64
            }
        })

    seller_label = {"private": "частное лицо", "company": "компания/дилер"}.get(seller_type, "не определён")
    user_msg = f"Заголовок: {title}\nЦена: {price} ₽\nРегион: {region}\nТип продавца: {seller_label}\nОписание: {description[:300]}\n\nЕсли есть фото — определи модель и бренд по шильдику или внешнему виду оборудования."
    content.append({"type": "text", "text": user_msg})

    resp = _client().messages.create(
        model=get_settings().anthropic_model,
        max_tokens=600,
        system=AVITO_EVAL_SYSTEM,
        messages=[{"role": "user", "content": content}],
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
    try:
        with open("/root/food-equipment-backend/price_data.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass

    return result
