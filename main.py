import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import airtable_client as at
import anthropic_client as ai
from config import get_settings
from db import save_subscriber, get_matching_subscribers, unsubscribe, get_conn as get_db_conn
from payments import create_payment, verify_webhook
from schemas import (
    AvitoEvalIn,
    AvitoEvalOut,
    ChatIn,
    ChatOut,
    FetchUrlIn,
    FetchUrlOut,
    LeadIn,
    ListingIn,
    ListingOut,
    ResellerAnalyzeIn,
    ResellerAnalyzeOut,
    WantToBuyIn,
)

logger = logging.getLogger("food-equipment")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Food Equipment API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins_list,
    # chrome-extension://* is a wildcard, so it must be matched via regex
    # (allow_origins only does exact string matching).
    allow_origin_regex=r"^(chrome-extension://.*|https://(.*\.)?indmart\.ru)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {"service": "food-equipment-api", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- Leads ----------

def _notify_telegram(lead: LeadIn) -> None:
    s = get_settings()
    if not s.telegram_bot_token or not s.telegram_chat_id:
        return
    text = (
        "🔔 Новый лид!\n"
        f"Имя: {lead.name}\n"
        f"Email: {lead.email or '—'}\n"
        f"Телефон: {lead.phone or '—'}\n"
        f"Город: {lead.city or '—'}\n"
        f"Сообщение: {lead.message or '—'}\n"
        f"Источник: {lead.source or 'website'}"
    )
    try:
        httpx.post(
            f"https://api.telegram.org/bot{s.telegram_bot_token}/sendMessage",
            json={"chat_id": s.telegram_chat_id, "text": text},
            timeout=5.0,
        ).raise_for_status()
    except Exception:
        logger.exception("telegram notification failed")


@app.post("/leads", status_code=201)
def create_lead(lead: LeadIn):
    fields = {
        "Name": lead.name,
        "Email": lead.email,
        "Source": lead.source or "website",
    }
    if lead.phone is not None:
        fields["Phone"] = lead.phone
    if lead.city is not None:
        fields["City"] = lead.city
    if lead.message is not None:
        fields["Message"] = lead.message
    if lead.chat:
        fields["Chat"] = lead.chat
    try:
        rec = at.create_record(at.leads_table(), fields)
    except Exception as e:
        logger.exception("airtable lead create failed")
        raise HTTPException(status_code=502, detail=f"Airtable error: {e}")
    _notify_telegram(lead)
    return {"id": rec["id"], "fields": rec.get("fields", {})}


# ---------- Listings ----------

def _listing_fields(l: ListingIn) -> dict:
    return {
        "Title": l.title,
        "Description": l.description,
        "Category": l.category,
        "Condition": l.condition,
        "Price": l.price,
        "Currency": l.currency,
        "City": l.city,
        "SellerName": l.seller_name,
        "SellerEmail": l.seller_email,
        "SellerPhone": l.seller_phone,
        "Photos": ",".join(l.photos) if l.photos else None,
        "Year": l.year,
        "Brand": l.brand,
    }


def _record_to_listing(rec: dict) -> ListingOut:
    f = rec.get("fields", {})
    photos_raw = f.get("Photos")
    if isinstance(photos_raw, str):
        photos = [p.strip() for p in photos_raw.split(",") if p.strip()]
    elif isinstance(photos_raw, list):
        photos = photos_raw
    else:
        photos = []
    return ListingOut(
        id=rec["id"],
        title=f.get("Title", ""),
        description=f.get("Description", ""),
        category=f.get("Category", ""),
        condition=f.get("Condition", "used"),
        price=float(f.get("Price") or 0),
        currency=f.get("Currency", "USD"),
        city=f.get("City"),
        seller_name=f.get("SellerName", ""),
        seller_email=f.get("SellerEmail", ""),
        seller_phone=f.get("SellerPhone"),
        photos=photos,
        year=f.get("Year"),
        brand=f.get("Brand"),
        created_at=rec.get("createdTime"),
    )


@app.post("/listings", status_code=201, response_model=ListingOut)
def create_listing(listing: ListingIn):
    try:
        rec = at.create_record(at.listings_table(), _listing_fields(listing))
    except Exception as e:
        logger.exception("airtable listing create failed")
        raise HTTPException(status_code=502, detail=f"Airtable error: {e}")
    return _record_to_listing(rec)


@app.get("/listings", response_model=list[ListingOut])
def list_listings(
    category: Optional[str] = None,
    condition: Optional[str] = None,
    city: Optional[str] = None,
    brand: Optional[str] = None,
    price_min: Optional[float] = Query(None, ge=0),
    price_max: Optional[float] = Query(None, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    parts: list[str] = []
    if category:
        parts.append(f"{{Category}}='{category}'")
    if condition:
        parts.append(f"{{Condition}}='{condition}'")
    if city:
        parts.append(f"{{City}}='{city}'")
    if brand:
        parts.append(f"{{Brand}}='{brand}'")
    if price_min is not None:
        parts.append(f"{{Price}}>={price_min}")
    if price_max is not None:
        parts.append(f"{{Price}}<={price_max}")
    formula = f"AND({', '.join(parts)})" if parts else None

    try:
        records = at.list_records(
            at.listings_table(),
            formula=formula,
            max_records=limit,
            sort=["-Price"],
        )
    except Exception as e:
        logger.exception("airtable listings query failed")
        raise HTTPException(status_code=502, detail=f"Airtable error: {e}")
    return [_record_to_listing(r) for r in records]


@app.get("/listings/{listing_id}", response_model=ListingOut)
def get_listing(listing_id: str):
    try:
        rec = at.get_record(at.listings_table(), listing_id)
    except Exception as e:
        logger.exception("airtable listing fetch failed")
        raise HTTPException(status_code=404, detail=f"Listing not found: {e}")
    return _record_to_listing(rec)


# ---------- Want to buy ----------

@app.post("/want-to-buy", status_code=201)
def create_want_to_buy(w: WantToBuyIn):
    fields = {
        "Name": w.name,
        "Email": w.email,
        "Phone": w.phone,
        "EquipmentType": w.equipment_type,
        "BudgetMin": w.budget_min,
        "BudgetMax": w.budget_max,
        "City": w.city,
        "Details": w.details,
        "Urgency": w.urgency,
    }
    try:
        rec = at.create_record(at.want_to_buy_table(), fields)
    except Exception as e:
        logger.exception("airtable want-to-buy create failed")
        raise HTTPException(status_code=502, detail=f"Airtable error: {e}")
    return {"id": rec["id"], "fields": rec.get("fields", {})}


# ---------- AI chat ----------

@app.post("/chat", response_model=ChatOut)
def chat(req: ChatIn):
    try:
        text, stop = ai.chat(req.messages, system=req.system, max_tokens=req.max_tokens)
    except Exception as e:
        logger.exception("anthropic chat failed")
        raise HTTPException(status_code=502, detail=f"AI error: {e}")
    return ChatOut(reply=text, stop_reason=stop)


# ---------- Reseller analysis ----------

@app.post("/reseller/analyze", response_model=ResellerAnalyzeOut)
def reseller_analyze(item: ResellerAnalyzeIn):
    try:
        return ai.analyze_for_reseller(item)
    except json.JSONDecodeError as e:
        logger.exception("reseller JSON parse failed")
        raise HTTPException(status_code=502, detail=f"AI returned invalid JSON: {e}")
    except Exception as e:
        logger.exception("reseller analyze failed")
        raise HTTPException(status_code=502, detail=f"AI error: {e}")


# ---------- Avito eval (Chrome extension) ----------

@app.post("/avito-eval", response_model=AvitoEvalOut)
def avito_eval(req: AvitoEvalIn):
    try:
        data = ai.evaluate_avito_listing(
            title=req.title,
            price=req.price,
            region=req.region,
            description=req.description,
            photos=req.photos,
            seller_type=req.seller_type,
        )
        if data.get('urgency') in ('urgent', 'liquidation'):
            try:
                subs = get_matching_subscribers(
                    req.region,
                    data.get('reseller_margin', 0),
                    True,
                    req.seller_type == 'private',
                    data.get('category', ''),
                )
                if subs:
                    import threading
                    import httpx as _httpx
                    bot_token = os.environ.get('TELEGRAM_NOTIFY_TOKEN', '')
                    listing_url = getattr(req, 'listing_url', '')

                    def send_notifications():
                        for sub in subs:
                            chat_id = sub.get('telegram_chat_id')
                            if not chat_id or not bot_token:
                                continue
                            # Check duplicate
                            try:
                                with get_db_conn() as conn:
                                    with conn.cursor() as cur:
                                        cur.execute("""
                                            SELECT id FROM urgent_alerts
                                            WHERE subscriber_id=%s AND listing_url=%s
                                        """, (sub['id'], listing_url))
                                        if cur.fetchone():
                                            continue
                                        cur.execute("""
                                            INSERT INTO urgent_alerts
                                            (subscriber_id, listing_url, listing_title, listing_price, region, verdict)
                                            VALUES (%s, %s, %s, %s, %s, %s)
                                        """, (sub['id'], listing_url, req.title, req.price, req.region, data.get('verdict')))
                                        conn.commit()
                            except Exception as e:
                                logger.warning(f"Alert dedup error: {e}")
                                continue
                            urgency_emoji = "🔥" if data.get('urgency') == 'liquidation' else "⚡"
                            msg = (
                                f"{urgency_emoji} *Срочная продажа!*\n\n"
                                f"*{data.get('category', req.title)}*\n"
                                f"💰 Цена: {req.price:,} ₽\n"
                                f"📍 Регион: {req.region or 'не указан'}\n"
                                f"💵 Маржа: ~{data.get('reseller_margin', 0):,} ₽\n"
                                f"⏱ Оборот: {data.get('turnover_days', '')}\n"
                                f"👤 {'Частное лицо' if req.seller_type == 'private' else 'Компания'}\n\n"
                                f"_{data.get('comment', '')}_\n\n"
                            )
                            if sub.get('is_paid'):
                                link_text = f"🔗 [Открыть объявление]({listing_url})" if listing_url else ""
                            else:
                                link_text = "🔒 Ссылка скрыта\n👉 [Открыть за 2 990 ₽/мес](https://indmart.ru/#upgrade-screen)"
                            msg += link_text
                            try:
                                _httpx.post(
                                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                    json={
                                        "chat_id": chat_id,
                                        "text": msg,
                                        "parse_mode": "Markdown",
                                        "disable_web_page_preview": False
                                    },
                                    timeout=10
                                )
                            except Exception as e:
                                logger.warning(f"Telegram send failed for {chat_id}: {e}")

                    thread = threading.Thread(target=send_notifications, daemon=True)
                    thread.start()
                    logger.info(f"Started notification thread for {len(subs)} subscribers")
            except Exception as e:
                logger.warning(f"Notification setup failed: {e}")

        # Group notification - max once per 3 days
        try:
            group_chat_id = int(os.environ.get('TELEGRAM_GROUP_CHAT_ID', '0'))
            bot_token = os.environ.get('TELEGRAM_NOTIFY_TOKEN', '')

            if bot_token and group_chat_id and data.get('urgency') in ('urgent', 'liquidation'):
                with get_db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT sent_at FROM group_notifications
                            WHERE chat_id = %s
                            ORDER BY sent_at DESC LIMIT 1
                        """, (group_chat_id,))
                        last = cur.fetchone()

                        now = datetime.now(timezone.utc)
                        can_post = not last or (now - last[0]) > timedelta(days=3)

                        if can_post:
                            urgency_emoji = "🔥" if data.get('urgency') == 'liquidation' else "⚡"
                            listing_url = getattr(req, 'listing_url', '')
                            group_msg = (
                                f"{urgency_emoji} *Срочная продажа!*\n\n"
                                f"*{data.get('category', req.title)}*\n"
                                f"💰 Цена: {req.price:,} ₽\n"
                                f"📍 Регион: {req.region or 'не указан'}\n"
                                f"💵 Маржа: ~{data.get('reseller_margin', 0):,} ₽\n"
                                f"⏱ Оборот: {data.get('turnover_days', '')}\n\n"
                                f"🔗 Ссылка только подписчикам\n"
                                f"👉 [Подписаться](https://indmart.ru/#subscribe-screen)"
                            )

                            def send_group():
                                try:
                                    import httpx as _httpx
                                    resp = _httpx.post(
                                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                        json={
                                            "chat_id": group_chat_id,
                                            "text": group_msg,
                                            "parse_mode": "Markdown",
                                            "disable_web_page_preview": True
                                        },
                                        timeout=10
                                    )
                                    if resp.status_code == 200:
                                        # Save ONLY after successful send
                                        with get_db_conn() as conn2:
                                            with conn2.cursor() as cur2:
                                                cur2.execute("""
                                                    INSERT INTO group_notifications
                                                    (chat_id, listing_url, listing_title)
                                                    VALUES (%s, %s, %s)
                                                """, (group_chat_id, listing_url, req.title))
                                                conn2.commit()
                                        logger.info(f"Group notification sent to {group_chat_id}")
                                    else:
                                        logger.warning(f"Group notification failed: {resp.status_code} {resp.text}")
                                except Exception as e:
                                    logger.warning(f"Group send failed: {e}")

                            import threading as _threading
                            _threading.Thread(target=send_group, daemon=True).start()
        except Exception as e:
            logger.warning(f"Group notification setup failed: {e}")
        return AvitoEvalOut(**data, data_source="ai")
    except json.JSONDecodeError as e:
        logger.exception("avito eval JSON parse failed")
        raise HTTPException(status_code=502, detail=f"AI returned invalid JSON: {e}")
    except Exception as e:
        logger.exception("avito eval failed")
        raise HTTPException(status_code=502, detail=f"AI error: {e}")


@app.get("/price-stats")
def price_stats():
    """Returns median prices from collected price_data.jsonl grouped by category+brand+model"""
    import json, os
    from collections import defaultdict
    import statistics

    data_file = "/root/food-equipment-backend/price_data.jsonl"
    if not os.path.exists(data_file):
        return {}

    groups = defaultdict(list)
    with open(data_file, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line.strip())
                key = f"{r.get('category','')}/{r.get('brand','')}/{r.get('model','')}".lower()
                if r.get('price') and r['price'] > 0:
                    groups[key].append(r['price'])
            except Exception:
                pass

    result = {}
    for key, prices in groups.items():
        if len(prices) >= 2:
            result[key] = {
                "median": statistics.median(prices),
                "count": len(prices),
                "min": min(prices),
                "max": max(prices)
            }
    return result


# ---------- Telegram subscriber system ----------

class SubscribeIn(BaseModel):
    name: str
    telegram_username: str
    region: str = ""
    categories: list[str] = []
    user_type: str = "reseller"
    filters: dict = {}


@app.post("/subscribe", status_code=201)
def subscribe_route(req: SubscribeIn):
    # простая валидация telegram username
    username = req.telegram_username.strip().lstrip('@')
    if not re.match(r'^[A-Za-z0-9_]{4,32}$', username):
        raise HTTPException(status_code=400, detail="Некорректный Telegram username")
    try:
        sub_id, token = save_subscriber(
            req.name, username, req.region,
            req.categories, req.user_type, req.filters
        )
        logger.info(f"New subscriber: @{username} type={req.user_type} region={req.region}")
        return {"id": sub_id, "status": "subscribed", "unsubscribe_token": token}
    except Exception as e:
        logger.exception("subscribe failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/unsubscribe/{token}")
def unsubscribe_route(token: str):
    if unsubscribe(token):
        return {"status": "unsubscribed"}
    raise HTTPException(status_code=404, detail="Подписка не найдена")


# ---------- URL fetch (for AI analysis of marketplace listings) ----------

_FETCH_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
_FETCH_TEXT_LIMIT = 10000
_HTML_ENTITIES = {
    "&nbsp;": " ",
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
    "&apos;": "'",
}


def _strip_html_to_text(html: str) -> str:
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    html = re.sub(r"</(p|div|h[1-6]|li|tr)>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    for k, v in _HTML_ENTITIES.items():
        text = text.replace(k, v)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


@app.post("/fetch-url", response_model=FetchUrlOut)
def fetch_url(req: FetchUrlIn):
    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http/https URLs are allowed")
    host = (parsed.hostname or "").lower()
    if host in _FETCH_BLOCKED_HOSTS or host.endswith(".local"):
        raise HTTPException(status_code=400, detail="Blocked host")
    try:
        res = httpx.get(
            req.url,
            timeout=10.0,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )
        res.raise_for_status()
    except httpx.HTTPError as e:
        logger.exception("fetch-url failed")
        raise HTTPException(status_code=502, detail=f"Fetch failed: {e}")
    return FetchUrlOut(text=_strip_html_to_text(res.text)[:_FETCH_TEXT_LIMIT])


# ---------- Tinkoff payments ----------

_PLANS = {
    "reseller": {"amount": 2990, "description": "IndMart Перекупщик — подписка на 1 месяц"},
    "pro": {"amount": 5990, "description": "IndMart Про — подписка на 1 месяц"},
}


class CreatePaymentIn(BaseModel):
    telegram_username: str
    plan: str = "reseller"  # reseller / pro


@app.post("/create-payment")
def create_payment_route(req: CreatePaymentIn):
    try:
        username = req.telegram_username.lstrip("@").strip()
        if not username:
            raise HTTPException(status_code=400, detail="telegram_username required")
        plan = _PLANS.get(req.plan, _PLANS["reseller"])
        order_id = f"{username}-{uuid.uuid4().hex[:8]}"
        result = create_payment(
            amount_rub=plan["amount"],
            order_id=order_id,
            telegram_username=username,
            description=plan["description"],
        )
        return {"payment_url": result["url"], "order_id": order_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("create payment failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/payment-webhook")
async def payment_webhook(request: Request):
    # Tinkoff ожидает в ответ ровно "OK" (text/plain), иначе шлёт повторные уведомления.
    try:
        data = await request.json()
    except Exception:
        logger.warning("payment webhook: bad JSON body")
        return PlainTextResponse("OK")
    try:
        if not verify_webhook(data):
            logger.warning("payment webhook: invalid signature")
            return PlainTextResponse("OK")
        if data.get("Status") == "CONFIRMED":
            telegram_username = (data.get("DATA") or {}).get("telegram_username", "")
            if telegram_username:
                with get_db_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE subscribers
                            SET is_paid = true, paid_at = NOW()
                            WHERE LOWER(telegram_username) = LOWER(%s)
                            """,
                            (telegram_username,),
                        )
                    conn.commit()
                logger.info(f"Payment confirmed for @{telegram_username}")
                bot_token = os.environ.get('TELEGRAM_NOTIFY_TOKEN', '')
                if bot_token:
                    with get_db_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT telegram_chat_id FROM subscribers WHERE LOWER(telegram_username) = LOWER(%s)",
                                (telegram_username,),
                            )
                            row = cur.fetchone()
                    if row and row[0]:
                        try:
                            httpx.post(
                                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                                json={
                                    "chat_id": row[0],
                                    "text": "✅ Оплата прошла успешно!\n\nТеперь вы получаете полные ссылки на все срочные объявления.\n\nДобро пожаловать в IndMart Перекупщик! 🎉",
                                },
                                timeout=5,
                            )
                        except Exception as e:
                            logger.warning(f"Telegram notify after payment failed: {e}")
    except Exception:
        logger.exception("payment webhook handler failed")
    return PlainTextResponse("OK")
