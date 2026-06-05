import os
import json
import logging
import httpx

logger = logging.getLogger(__name__)

# gemini-1.5-flash was retired (404). gemini-2.5-flash is the current fast model.
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

def evaluate_with_gemini(title: str, price: int, region: str, description: str) -> dict:
    api_key = os.environ.get('GEMINI_API_KEY', '')
    if not api_key:
        raise Exception("Gemini API key not set")

    prompt = f"""Оцени б/у пищевое оборудование для перекупщика. Ответь ТОЛЬКО валидным JSON без markdown.

Оборудование: {title}
Цена: {price} руб
Регион: {region}
Описание: {description[:300]}

Верни JSON:
{{
  "verdict": "green/yellow/red",
  "category": "категория",
  "market_min": минимальная рыночная цена числом,
  "market_max": максимальная рыночная цена числом,
  "reseller_margin": маржа перекупщика числом (0 если цена выше медианы),
  "turnover_days": "7-14 дней",
  "demand": "высокий/средний/низкий",
  "condition_visual": "unknown",
  "urgency": "normal/urgent/liquidation",
  "comment": "краткий совет перекупщику"
}}"""

    try:
        resp = httpx.post(
            f"{GEMINI_API_URL}?key={api_key}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1000}
            },
            timeout=20.0
        )
        data = resp.json()
        text = data['candidates'][0]['content']['parts'][0]['text']
        # Strip markdown if present
        text = text.strip()
        if text.startswith('```'):
            text = text.split('```')[1]
            if text.startswith('json'):
                text = text[4:]
        text = text.strip()
        result = json.loads(text)
        result['_source'] = 'gemini'
        return result
    except Exception as e:
        logger.warning(f"Gemini fallback failed: {e}")
        raise
