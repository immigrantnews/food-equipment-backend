import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx
import jwt as pyjwt
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
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
    ListingCreate,
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


# ---------- Bulletin board: JWT auth ----------
JWT_SECRET = os.environ.get('JWT_SECRET', 'indmart-secret-key')


def get_current_user(authorization: Optional[str] = None) -> Optional[dict]:
    """Extract user from Bearer token. Returns None if missing/invalid."""
    if not authorization or not authorization.startswith('Bearer '):
        return None
    token = authorization.replace('Bearer ', '')
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except Exception:
        return None


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
                            if listing_url:
                                if sub.get('is_paid'):
                                    link_text = f"🔗 [Открыть объявление]({listing_url})"
                                else:
                                    link_text = (
                                        f"🔒 Ссылка скрыта\n\n"
                                        f"Подписчики IndMart получают такие уведомления первыми\n"
                                        f"👉 [Получить доступ — 2 990 ₽/мес](https://indmart.ru/upgrade)"
                                    )
                            else:
                                link_text = f"👉 [IndMart Перекупщик](https://indmart.ru/upgrade)"

                            msg += f"\n\n{link_text}"
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
                                f"🔒 Ссылка только подписчикам\n"
                                f"📊 Подписчики получают все срочные продажи первыми\n"
                                f"👉 [Получить доступ — 2 990 ₽/мес](https://indmart.ru/upgrade)"
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
        logger.info(f"Webhook received: status={data.get('Status')} order={data.get('OrderId')} token_present={bool(data.get('Token'))}")
    except Exception:
        logger.warning("payment webhook: bad JSON body")
        return PlainTextResponse("OK")
    try:
        if data.get("Status") == "CONFIRMED":
            logger.info("Processing CONFIRMED payment. Verifying signature...")
        sig_valid = verify_webhook(data)
        if data.get("Status") == "CONFIRMED":
            logger.info(f"Signature valid: {sig_valid}")
        if not sig_valid:
            logger.warning("payment webhook: invalid signature")
            return PlainTextResponse("OK")
        if data.get("Status") == "CONFIRMED":
            order_id = data.get("OrderId", "")
            telegram_username = (data.get("DATA") or {}).get("telegram_username", "")
            if not telegram_username and "-" in order_id:
                # DATA приходит пустым от Tinkoff — берём username из OrderId "username-hexcode"
                telegram_username = order_id.rsplit("-", 1)[0]
            logger.info(f"Resolved telegram_username: '{telegram_username}' (OrderId: '{order_id}')")
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
                        updated_rows = cur.rowcount
                    conn.commit()
                logger.info(f"Payment confirmed for @{telegram_username} (rows updated: {updated_rows})")
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


# ---------- Bulletin board API (Postgres) ----------
# NOTE: /listings (GET/POST/{id}) is the existing Airtable catalog. The bulletin
# board lives under /board/* to avoid colliding with it.

@app.post("/auth/telegram")
def auth_telegram(data: dict = Body(...)):
    """Register/login user via Telegram Login Widget data."""
    telegram_id = data.get('id')
    if not telegram_id:
        raise HTTPException(status_code=400, detail="No telegram_id")

    # NOTE: In production, verify Telegram hash here (HMAC-SHA256 with bot token).
    # For MVP we trust the data since it comes from the Telegram widget.

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (telegram_id, telegram_username, first_name, last_name, photo_url)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    telegram_username = EXCLUDED.telegram_username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    photo_url = EXCLUDED.photo_url,
                    last_seen_at = NOW()
                RETURNING id, telegram_id, telegram_username, first_name, user_type, city
            """, (
                int(telegram_id),
                data.get('username'),
                data.get('first_name'),
                data.get('last_name'),
                data.get('photo_url')
            ))
            user = cur.fetchone()
            conn.commit()

    exp = int(datetime.now(timezone.utc).timestamp()) + 30 * 24 * 3600
    token = pyjwt.encode(
        {'user_id': user[0], 'telegram_id': user[1], 'username': user[2], 'exp': exp},
        JWT_SECRET, algorithm='HS256'
    )
    return {
        "token": token,
        "user": {"id": user[0], "telegram_id": user[1], "username": user[2],
                 "first_name": user[3], "user_type": user[4], "city": user[5]}
    }


@app.get("/board/listings")
def board_get_listings(
    category: str = None, city: str = None,
    min_price: int = None, max_price: int = None,
    condition: str = None, search: str = None,
    limit: int = 20, offset: int = 0
):
    limit = min(max(1, limit), 50)
    offset = max(0, offset)
    if search:
        search = search[:100].strip()

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            where = ["l.status = 'active'"]
            params = []
            if category:
                where.append("l.category = %s")
                params.append(category)
            if city:
                where.append("l.city ILIKE %s")
                params.append(f"%{city}%")
            if min_price is not None:
                where.append("l.price >= %s")
                params.append(min_price)
            if max_price is not None:
                where.append("l.price <= %s")
                params.append(max_price)
            if condition:
                where.append("l.condition = %s")
                params.append(condition)
            if search:
                where.append("l.title ILIKE %s")
                params.append(f"%{search}%")

            where_str = ' AND '.join(where)

            cur.execute(
                f"SELECT COUNT(*) FROM listings l WHERE {where_str}",
                params
            )
            total = cur.fetchone()[0]

            cur.execute(
                f"""SELECT l.id, l.user_id, l.title, l.category, l.condition, l.price,
                       l.city, l.description, l.photos, l.video_url,
                       l.status, l.views, l.ai_verdict, l.ai_market_min, l.ai_market_max,
                       l.created_at, u.first_name, u.telegram_username
                FROM listings l
                LEFT JOIN users u ON l.user_id = u.id
                WHERE {where_str}
                ORDER BY l.created_at DESC
                LIMIT %s OFFSET %s""",
                params + [limit, offset]
            )
            rows = cur.fetchall()

    listings = [{
        "id": r[0], "user_id": r[1], "title": r[2],
        "category": r[3], "condition": r[4], "price": r[5],
        "city": r[6], "description": (r[7] or '')[:200],
        "photos": r[8] if r[8] else [],
        "video_url": r[9], "status": r[10], "views": r[11],
        "ai_verdict": r[12], "ai_market_min": r[13], "ai_market_max": r[14],
        "created_at": r[15].isoformat() if r[15] else None,
        "seller_name": r[16], "seller_username": r[17]
    } for r in rows]

    return {"listings": listings, "total": total, "limit": limit, "offset": offset}


@app.get("/board/listings/{listing_id}")
def board_get_listing(listing_id: int, authorization: str = Header(None)):
    user = get_current_user(authorization)
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET views = views + 1 WHERE id = %s AND status = 'active'",
                (listing_id,)
            )
            cur.execute("""
                SELECT l.id, l.user_id, l.title, l.category, l.condition, l.price,
                       l.city, l.region, l.description, l.photos, l.video_url,
                       l.phone, l.telegram_username, l.status, l.views,
                       l.ai_verdict, l.ai_market_min, l.ai_market_max, l.ai_comment,
                       l.created_at, u.first_name, u.telegram_username, u.user_type
                FROM listings l
                LEFT JOIN users u ON l.user_id = u.id
                WHERE l.id = %s AND l.status IN ('active', 'sold')
            """, (listing_id,))
            r = cur.fetchone()
            conn.commit()

    if not r:
        raise HTTPException(status_code=404, detail="Listing not found")

    return {
        "id": r[0], "user_id": r[1], "title": r[2],
        "category": r[3], "condition": r[4], "price": r[5],
        "city": r[6], "region": r[7], "description": r[8],
        "photos": r[9] if r[9] else [],
        "video_url": r[10],
        # Contacts only for authenticated users
        "phone": r[11] if user else None,
        "telegram_username": r[12] if user else None,
        "contacts_hidden": not bool(user),
        "status": r[13], "views": r[14],
        "ai_verdict": r[15], "ai_market_min": r[16], "ai_market_max": r[17],
        "ai_comment": r[18],
        "created_at": r[19].isoformat() if r[19] else None,
        "seller_name": r[20], "seller_username": r[21], "seller_type": r[22]
    }


@app.post("/board/listings")
def board_create_listing(req: ListingCreate, authorization: str = Header(None)):
    user = get_current_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            # Rate limit: max 5 listings per day per user
            cur.execute("""
                SELECT COUNT(*) FROM listings
                WHERE user_id = %s AND created_at > NOW() - INTERVAL '24 hours'
            """, (user['user_id'],))
            count_today = cur.fetchone()[0]
            if count_today >= 5:
                raise HTTPException(status_code=429, detail="Max 5 listings per day")

            cur.execute("""
                INSERT INTO listings
                (user_id, title, category, condition, price, city, region,
                 description, photos, video_url, phone, telegram_username)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)
                RETURNING id
            """, (
                user['user_id'], req.title, req.category, req.condition,
                req.price, req.city, req.region, req.description,
                json.dumps(req.photos, ensure_ascii=False),
                req.video_url, req.phone, req.telegram_username
            ))
            listing_id = cur.fetchone()[0]
            conn.commit()

    # AI evaluation in background thread
    def run_ai_eval():
        try:
            result = ai.evaluate_avito_listing(
                title=req.title,
                price=req.price,
                region=req.city or '',
                description=req.description or '',
                photos=[],
                seller_type='unknown',
            )
            with get_db_conn() as conn2:
                with conn2.cursor() as cur2:
                    cur2.execute("""
                        UPDATE listings SET
                            ai_verdict = %s, ai_market_min = %s,
                            ai_market_max = %s, ai_comment = %s
                        WHERE id = %s
                    """, (
                        result.get('verdict'), result.get('market_min'),
                        result.get('market_max'), result.get('comment'),
                        listing_id
                    ))
                    conn2.commit()
        except Exception as e:
            logger.warning(f"AI eval for listing {listing_id} failed: {e}")

    import threading
    threading.Thread(target=run_ai_eval, daemon=True).start()

    return {"id": listing_id, "status": "active"}


@app.put("/board/listings/{listing_id}/status")
def board_update_listing_status(
    listing_id: int,
    data: dict = Body(...),
    authorization: str = Header(None)
):
    user = get_current_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    new_status = data.get('status')
    if new_status not in ('active', 'sold', 'archived'):
        raise HTTPException(status_code=400, detail="Invalid status")
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE listings SET status = %s, updated_at = NOW()
                WHERE id = %s AND user_id = %s
                RETURNING id
            """, (new_status, listing_id, user['user_id']))
            updated = cur.fetchone()
            conn.commit()
    if not updated:
        raise HTTPException(status_code=404, detail="Listing not found or not yours")
    return {"ok": True}
