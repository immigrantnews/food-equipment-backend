import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse, quote

import httpx
import jwt as pyjwt
from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import anthropic_client as ai
from config import get_settings
from db import save_subscriber, get_matching_subscribers, unsubscribe, get_conn as get_db_conn
from payments import create_payment, verify_webhook
from schemas import (
    AvitoEvalIn,
    AvitoEvalOut,
    ChatIn,
    ChatMessage,
    ChatOut,
    FetchUrlIn,
    FetchUrlOut,
    ListingCreate,
    ResellerAnalyzeIn,
    ResellerAnalyzeOut,
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
ADMIN_TELEGRAM_IDS = [
    int(x.strip()) for x in os.environ.get('ADMIN_TELEGRAM_IDS', '').split(',') if x.strip()
]
DAILY_AI_SEARCH_LIMIT = 30
AI_DIALOG_MAX_QUESTIONS = 2  # ask at most N clarifying questions before returning results

AI_VALID_CATEGORIES = ('Тестомесы', 'Печи', 'Расстойки', 'Пароконвектоматы',
                       'Холодильное', 'Упаковочное', 'Коптильное', 'Прочее')


def get_current_user(authorization: Optional[str] = None) -> Optional[dict]:
    """Extract user from Bearer token. Returns None if missing/invalid."""
    if not authorization or not authorization.startswith('Bearer '):
        return None
    token = authorization.replace('Bearer ', '')
    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=['HS256'])
    except Exception:
        return None


def check_admin(authorization: str) -> dict:
    """Require an authenticated admin (telegram_id in ADMIN_TELEGRAM_IDS)."""
    user = get_current_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if user.get('telegram_id') not in ADMIN_TELEGRAM_IDS:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def notify_admin_new_listing(listing_id: int, title: str, price: int, city: str, seller: str):
    """Notify all admins in Telegram about a newly created listing (best-effort)."""
    bot_token = os.environ.get('TELEGRAM_NOTIFY_TOKEN', '')
    admin_ids = ADMIN_TELEGRAM_IDS
    if not bot_token or not admin_ids:
        return
    msg = (
        f"📦 Новое объявление #{listing_id}\n\n"
        f"*{title}*\n"
        f"💰 {price:,} ₽\n"
        f"📍 {city or 'не указан'}\n"
        f"👤 @{seller or 'неизвестен'}\n\n"
        f"🔗 [Открыть](https://indmart.ru/listings#listing/{listing_id})"
    )
    import threading

    def send():
        try:
            import httpx
            for admin_id in admin_ids:
                httpx.post(
                    f"https://api.telegram.org/bot{bot_token}/sendMessage",
                    json={"chat_id": admin_id, "text": msg, "parse_mode": "Markdown"},
                    timeout=5
                )
        except Exception as e:
            logger.warning(f"Admin notification failed: {e}")
    threading.Thread(target=send, daemon=True).start()


@app.get("/")
def root():
    return {"service": "food-equipment-api", "status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------- AI chat ----------

@app.post("/chat")
def chat(data: dict = Body(...)):
    """Dual-contract chat.

    Legacy (index.html / extension): {messages: [{role,content},...], system, max_tokens}
    New (listings.html AI chat):     {message, system, history: [{role,content},...]}
    Both return {reply, stop_reason}.
    """
    system = data.get("system")
    max_tokens = max(1, min(int(data.get("max_tokens") or 1024), 8192))

    raw = data.get("messages")
    if not raw:
        # New single-message contract — history already includes the current
        # user turn (frontend pushes it before sending), so avoid duplicating it.
        history = data.get("history") or []
        msg = (data.get("message") or "").strip()
        raw = list(history)
        if msg and not (raw and raw[-1].get("role") == "user" and raw[-1].get("content") == msg):
            raw.append({"role": "user", "content": msg})

    # Anthropic requires the conversation to start with a user turn.
    while raw and raw[0].get("role") != "user":
        raw.pop(0)
    if not raw:
        raise HTTPException(status_code=400, detail="message required")

    try:
        messages = [ChatMessage(role=m["role"], content=m["content"]) for m in raw]
    except Exception:
        raise HTTPException(status_code=400, detail="invalid messages")

    try:
        text, stop = ai.chat(messages, system=system, max_tokens=max_tokens)
    except Exception as e:
        logger.exception("anthropic chat failed")
        raise HTTPException(status_code=502, detail=f"AI error: {e}")
    return {"reply": text, "stop_reason": stop}


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
            listing_url=req.listing_url,
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
                listing_url=f"https://indmart.ru/listings#listing/{listing_id}",
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

    # Notify admins about the new listing (best-effort, non-blocking)
    notify_admin_new_listing(listing_id, req.title, req.price, req.city or '', user.get('username', ''))

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


# ---------- Admin panel API ----------

@app.get("/admin/stats")
def admin_stats(authorization: str = Header(None)):
    check_admin(authorization)
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) FROM listings GROUP BY status")
            listings_by_status = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '7 days'")
            new_users_week = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM subscribers WHERE is_active = true AND is_group = false")
            total_subscribers = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM subscribers WHERE is_paid = true AND is_active = true AND is_group = false")
            paid_subscribers = cur.fetchone()[0]

            cur.execute("SELECT COALESCE(SUM(views), 0) FROM listings")
            total_views = cur.fetchone()[0]

            cur.execute("""
                SELECT category, COUNT(*)
                FROM listings
                WHERE status = 'active' AND category IS NOT NULL
                GROUP BY category
                ORDER BY COUNT(*) DESC
            """)
            categories_breakdown = {r[0]: r[1] for r in cur.fetchall()}

            cur.execute("""
                SELECT l.id, l.title, l.price, l.city, l.status, l.views,
                       l.created_at, u.telegram_username
                FROM listings l
                LEFT JOIN users u ON l.user_id = u.id
                ORDER BY l.created_at DESC LIMIT 20
            """)
            recent_listings = [{
                "id": r[0], "title": r[1], "price": r[2], "city": r[3],
                "status": r[4], "views": r[5],
                "created_at": r[6].isoformat() if r[6] else None,
                "seller": r[7]
            } for r in cur.fetchall()]

    return {
        "listings_by_status": listings_by_status,
        "total_users": total_users,
        "new_users_week": new_users_week,
        "total_subscribers": total_subscribers,
        "paid_subscribers": paid_subscribers,
        "total_views": int(total_views),
        "categories_breakdown": categories_breakdown,
        "recent_listings": recent_listings
    }


@app.get("/admin/listings")
def admin_get_listings(
    status: str = None,
    category: str = None,
    city: str = None,
    user_id: int = None,
    date_from: str = None,
    date_to: str = None,
    search: str = None,
    limit: int = 50,
    offset: int = 0,
    authorization: str = Header(None)
):
    check_admin(authorization)
    limit = min(max(1, limit), 100)
    offset = max(0, offset)

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            where = ["l.status != 'deleted'"]
            params = []
            if status and status in ('active', 'sold', 'archived', 'moderation'):
                where.append("l.status = %s")
                params.append(status)
            if category:
                where.append("l.category = %s")
                params.append(category)
            if city:
                where.append("l.city ILIKE %s")
                params.append(f"%{city}%")
            if user_id is not None:
                where.append("l.user_id = %s")
                params.append(user_id)
            if date_from:
                where.append("l.created_at >= %s")
                params.append(date_from)
            if date_to:
                where.append("l.created_at <= %s")
                params.append(date_to)
            if search:
                where.append("(l.title ILIKE %s OR l.city ILIKE %s OR u.telegram_username ILIKE %s)")
                like = f"%{search.strip()[:100]}%"
                params.extend([like, like, like])

            where_str = ' AND '.join(where)

            cur.execute(f"""
                SELECT l.id, l.title, l.price, l.city, l.status, l.views,
                       l.created_at, l.category, l.condition,
                       u.telegram_username, u.first_name
                FROM listings l
                LEFT JOIN users u ON l.user_id = u.id
                WHERE {where_str}
                ORDER BY l.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            rows = cur.fetchall()

            cur.execute(
                f"SELECT COUNT(*) FROM listings l LEFT JOIN users u ON l.user_id = u.id WHERE {where_str}",
                params
            )
            total = cur.fetchone()[0]

    return {
        "listings": [{
            "id": r[0], "title": r[1], "price": r[2], "city": r[3],
            "status": r[4], "views": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "category": r[7], "condition": r[8],
            "seller_username": r[9], "seller_name": r[10]
        } for r in rows],
        "total": total
    }


@app.put("/admin/listings/{listing_id}")
def admin_update_listing(
    listing_id: int,
    data: dict = Body(...),
    authorization: str = Header(None)
):
    check_admin(authorization)

    # Whitelist fields with type validation
    allowed = {
        'title': str,
        'price': int,
        'city': str,
        'status': str,
        'description': str,
        'category': str
    }
    allowed_statuses = ('active', 'sold', 'archived', 'moderation')

    updates = {}
    for field, field_type in allowed.items():
        if field in data:
            val = data[field]
            if field == 'status' and val not in allowed_statuses:
                raise HTTPException(status_code=400, detail=f"Invalid status: {val}")
            if field == 'price':
                try:
                    val = int(val)
                    if val <= 0:
                        raise ValueError()
                except Exception:
                    raise HTTPException(status_code=400, detail="Price must be positive integer")
            updates[field] = val

    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    set_clause = ', '.join([f"{k} = %s" for k in updates])
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE listings SET {set_clause}, updated_at = NOW() WHERE id = %s RETURNING id",
                list(updates.values()) + [listing_id]
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Listing not found")
            conn.commit()
    return {"ok": True}


@app.delete("/admin/listings/{listing_id}")
def admin_delete_listing(
    listing_id: int,
    authorization: str = Header(None)
):
    check_admin(authorization)
    # Soft delete - keep data but hide from public
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE listings SET status = 'deleted', updated_at = NOW() WHERE id = %s RETURNING id",
                (listing_id,)
            )
            if not cur.fetchone():
                raise HTTPException(status_code=404, detail="Listing not found")
            conn.commit()
    return {"ok": True}


@app.get("/admin/subscribers")
def admin_get_subscribers(
    limit: int = 100,
    offset: int = 0,
    authorization: str = Header(None)
):
    check_admin(authorization)
    limit = min(limit, 200)
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT telegram_username, name, is_paid, is_active,
                       user_type, region, paid_at, created_at
                FROM subscribers
                WHERE is_group = false
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) FROM subscribers WHERE is_group = false")
            total = cur.fetchone()[0]
    return {
        "subscribers": [{
            "username": r[0], "name": r[1], "is_paid": r[2],
            "is_active": r[3], "user_type": r[4], "region": r[5],
            "paid_at": r[6].isoformat() if r[6] else None,
            "created_at": r[7].isoformat() if r[7] else None
        } for r in rows],
        "total": total
    }


@app.get("/admin/users")
def admin_get_users(
    search: str = None,
    limit: int = 50,
    offset: int = 0,
    authorization: str = Header(None)
):
    check_admin(authorization)
    limit = min(limit, 100)
    offset = max(0, offset)

    search_where = ""
    params = []
    if search:
        search_where = "WHERE u.telegram_username ILIKE %s OR u.first_name ILIKE %s"
        params = [f"%{search}%", f"%{search}%"]

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT u.id, u.telegram_id, u.telegram_username, u.first_name,
                       u.user_type, u.city, u.created_at,
                       COUNT(DISTINCT l.id) FILTER (WHERE l.status != 'deleted') as listing_count,
                       COALESCE(SUM(l.views) FILTER (WHERE l.status != 'deleted'), 0) as total_views,
                       s.is_paid, s.is_active as sub_active
                FROM users u
                LEFT JOIN listings l ON l.user_id = u.id
                LEFT JOIN subscribers s ON LOWER(s.telegram_username) = LOWER(u.telegram_username)
                {search_where}
                GROUP BY u.id, s.is_paid, s.is_active
                ORDER BY u.created_at DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            rows = cur.fetchall()
            cur.execute(f"SELECT COUNT(*) FROM users u {search_where}", params)
            total = cur.fetchone()[0]

    return {
        "users": [{
            "id": r[0], "telegram_id": r[1], "username": r[2],
            "first_name": r[3], "user_type": r[4], "city": r[5],
            "created_at": r[6].isoformat() if r[6] else None,
            "listing_count": r[7], "total_views": int(r[8]),
            "is_paid_subscriber": r[9], "has_subscription": r[10]
        } for r in rows],
        "total": total
    }


@app.put("/admin/users/{user_id}/block")
def admin_block_user(user_id: int, authorization: str = Header(None)):
    check_admin(authorization)
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET is_active = false WHERE id = %s RETURNING id",
                (user_id,)
            )
            if not cur.fetchone():
                raise HTTPException(404, "User not found")
            # Also archive all their listings
            cur.execute(
                "UPDATE listings SET status = 'archived' WHERE user_id = %s AND status = 'active'",
                (user_id,)
            )
            conn.commit()
    return {"ok": True}


@app.get("/admin/subscribers/export")
def admin_export_subscribers(authorization: str = Header(None)):
    check_admin(authorization)
    from fastapi.responses import StreamingResponse
    import csv
    import io

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT telegram_username, name, is_paid, is_active,
                       user_type, region, created_at
                FROM subscribers WHERE is_group = false
                ORDER BY created_at DESC
            """)
            rows = cur.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Username', 'Имя', 'Платный', 'Активный', 'Тип', 'Регион', 'Дата'])
    for r in rows:
        writer.writerow([r[0], r[1], 'Да' if r[2] else 'Нет',
                        'Да' if r[3] else 'Нет', r[4], r[5],
                        r[6].strftime('%d.%m.%Y') if r[6] else ''])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8-sig')),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=subscribers.csv"}
    )


# ---------- AI search (paid subscribers, 30/day) ----------

def check_paid_subscription(user_id: int, telegram_username: str) -> bool:
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            # Try by telegram_username first (strip @ if present)
            username_clean = (telegram_username or '').lstrip('@').lower()
            if username_clean:
                cur.execute("""
                    SELECT is_paid FROM subscribers
                    WHERE LOWER(REPLACE(telegram_username, '@', '')) = %s
                    AND is_active = true AND is_group = false
                """, (username_clean,))
                row = cur.fetchone()
                if row:
                    return bool(row[0])
            return False


@app.post("/search/ai")
async def ai_search(data: dict = Body(...), authorization: str = Header(None)):
    user = get_current_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    query = (data.get('query') or '').strip()
    if len(query) < 3:
        raise HTTPException(status_code=400, detail="Query too short (min 3 chars)")
    query = query[:500]

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            # Get user telegram_username
            cur.execute(
                "SELECT telegram_username FROM users WHERE id = %s",
                (user['user_id'],)
            )
            user_row = cur.fetchone()
            if not user_row:
                raise HTTPException(status_code=404, detail="User not found")
            telegram_username = user_row[0] or ''

    # Check paid subscription
    is_paid = check_paid_subscription(user['user_id'], telegram_username)
    if not is_paid:
        raise HTTPException(
            status_code=403,
            detail="AI search requires paid subscription"
        )

    # Check daily limit
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM ai_search_usage
                WHERE user_id = %s
                AND created_at > NOW() - INTERVAL '24 hours'
            """, (user['user_id'],))
            used_today = cur.fetchone()[0]

    if used_today >= DAILY_AI_SEARCH_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit reached ({DAILY_AI_SEARCH_LIMIT}/day)"
        )

    # Extract search params via Claude with fallback
    params = {"keywords": [], "category": None, "city": None,
              "max_price": None, "min_price": None, "condition": None}

    try:
        import anthropic as _anthropic
        import json as _json
        client = _anthropic.Anthropic(
            api_key=os.environ.get('ANTHROPIC_API_KEY', ''),
            timeout=10.0
        )
        response = client.messages.create(
            model=os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6'),
            max_tokens=300,
            messages=[{"role": "user", "content": f"""Извлеки параметры поиска из запроса для базы пищевого оборудования.
Верни ТОЛЬКО JSON без markdown:
{{"keywords":["слово1","слово2"],"category":"Тестомесы|Печи|Расстойки|Пароконвектоматы|Холодильное|Упаковочное|Коптильное|Прочее|null","city":"город или null","max_price":число или null,"min_price":число или null,"condition":"used|new|null"}}
Запрос: {query}"""}]
        )
        text = response.content[0].text.strip().strip('`').strip()
        if text.startswith('json'):
            text = text[4:].strip()
        raw = _json.loads(text)

        # Validate each field before using
        if isinstance(raw.get('keywords'), list):
            params['keywords'] = [str(k)[:50] for k in raw['keywords'][:5] if k]
        valid_categories = ('Тестомесы', 'Печи', 'Расстойки', 'Пароконвектоматы',
                            'Холодильное', 'Упаковочное', 'Коптильное', 'Прочее')
        if raw.get('category') in valid_categories:
            params['category'] = raw['category']
        if raw.get('city') and isinstance(raw['city'], str):
            params['city'] = str(raw['city'])[:100]
        if raw.get('max_price') and str(raw['max_price']).isdigit():
            params['max_price'] = int(raw['max_price'])
        if raw.get('min_price') and str(raw['min_price']).isdigit():
            params['min_price'] = int(raw['min_price'])
        if raw.get('condition') in ('used', 'new', 'parts'):
            params['condition'] = raw['condition']

    except Exception as e:
        # Fallback: use query words directly
        logger.warning(f"AI param extraction failed: {e}, using keyword fallback")
        params['keywords'] = [w for w in query.split()[:5] if len(w) >= 3]

    # If no keywords extracted at all use original query words
    if not params['keywords']:
        params['keywords'] = [w for w in query.split()[:5] if len(w) >= 3]

    # Build and execute search query
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            where = ["l.status = 'active'"]
            sql_params = []

            if params['keywords']:
                kw_conditions = []
                for kw in params['keywords']:
                    kw_conditions.append(
                        "(l.title ILIKE %s OR l.description ILIKE %s)"
                    )
                    sql_params.extend([f"%{kw}%", f"%{kw}%"])
                where.append(f"({' OR '.join(kw_conditions)})")

            if params['category']:
                where.append("l.category = %s")
                sql_params.append(params['category'])
            if params['city']:
                where.append("l.city ILIKE %s")
                sql_params.append(f"%{params['city']}%")
            if params['max_price']:
                where.append("l.price <= %s")
                sql_params.append(params['max_price'])
            if params['min_price']:
                where.append("l.price >= %s")
                sql_params.append(params['min_price'])
            if params['condition']:
                where.append("l.condition = %s")
                sql_params.append(params['condition'])

            where_str = ' AND '.join(where)
            cur.execute(f"""
                SELECT l.id, l.title, l.price, l.city, l.condition,
                       l.photos, l.status, l.views, l.ai_verdict,
                       l.ai_market_min, l.ai_market_max,
                       l.created_at, u.first_name, u.telegram_username
                FROM listings l
                LEFT JOIN users u ON l.user_id = u.id
                WHERE {where_str}
                ORDER BY l.created_at DESC
                LIMIT 20
            """, sql_params)
            results = [{
                "id": r[0], "title": r[1], "price": r[2], "city": r[3],
                "condition": r[4], "photos": r[5] if r[5] else [],
                "status": r[6], "views": r[7], "ai_verdict": r[8],
                "ai_market_min": r[9], "ai_market_max": r[10],
                "created_at": r[11].isoformat() if r[11] else None,
                "seller_name": r[12], "seller_username": r[13]
            } for r in cur.fetchall()]

            # Save usage AFTER successful search
            cur.execute(
                "INSERT INTO ai_search_usage (user_id, query, results_count) VALUES (%s, %s, %s)",
                (user['user_id'], query, len(results))
            )
            conn.commit()

    remaining = DAILY_AI_SEARCH_LIMIT - used_today - 1
    return {
        "results": results,
        "total": len(results),
        "remaining_today": max(0, remaining),
        "daily_limit": DAILY_AI_SEARCH_LIMIT,
        "extracted": params
    }


@app.get("/search/ai/usage")
def get_ai_search_usage(authorization: str = Header(None)):
    user = get_current_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM ai_search_usage
                WHERE user_id = %s
                AND created_at > NOW() - INTERVAL '24 hours'
            """, (user['user_id'],))
            used_today = cur.fetchone()[0]
    return {
        "used_today": used_today,
        "daily_limit": DAILY_AI_SEARCH_LIMIT,
        "remaining": max(0, DAILY_AI_SEARCH_LIMIT - used_today)
    }


# ---------- Conversational AI dialog search ----------

def _validate_search_params(raw: dict) -> dict:
    """Validate/whitelist Claude-extracted search params (defensive)."""
    params = {"keywords": [], "category": None, "city": None,
              "max_price": None, "min_price": None, "condition": None,
              "search_query": None}
    if isinstance(raw.get('keywords'), list):
        params['keywords'] = [str(k)[:50] for k in raw['keywords'][:5] if k]
    if raw.get('category') in AI_VALID_CATEGORIES:
        params['category'] = raw['category']
    if raw.get('city') and isinstance(raw['city'], str):
        params['city'] = str(raw['city'])[:100]
    if raw.get('max_price') and str(raw['max_price']).isdigit():
        params['max_price'] = int(raw['max_price'])
    if raw.get('min_price') and str(raw['min_price']).isdigit():
        params['min_price'] = int(raw['min_price'])
    if raw.get('condition') in ('used', 'new', 'parts'):
        params['condition'] = raw['condition']
    if raw.get('search_query') and isinstance(raw['search_query'], str):
        params['search_query'] = str(raw['search_query'])[:100]
    return params


def _search_listings(params: dict) -> dict:
    """Search active board listings AND evaluated market (Avito) listings.

    Returns {"results": [...board...], "market_results": [...avito...]}.
    """
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            where = ["l.status = 'active'"]
            sql_params = []
            if params['keywords']:
                kw = []
                for k in params['keywords']:
                    kw.append("(l.title ILIKE %s OR l.description ILIKE %s)")
                    sql_params.extend([f"%{k}%", f"%{k}%"])
                where.append("(" + " OR ".join(kw) + ")")
            if params.get('category'):
                where.append("l.category = %s")
                sql_params.append(params['category'])
            if params.get('city'):
                where.append("l.city ILIKE %s")
                sql_params.append(f"%{params['city']}%")
            if params.get('max_price'):
                where.append("l.price <= %s")
                sql_params.append(params['max_price'])
            if params.get('min_price'):
                where.append("l.price >= %s")
                sql_params.append(params['min_price'])
            if params.get('condition'):
                where.append("l.condition = %s")
                sql_params.append(params['condition'])
            cur.execute(f"""
                SELECT l.id, l.title, l.price, l.city, l.condition,
                       l.photos, l.status, l.views, l.ai_verdict,
                       l.ai_market_min, l.ai_market_max,
                       l.created_at, u.first_name, u.telegram_username
                FROM listings l
                LEFT JOIN users u ON l.user_id = u.id
                WHERE {' AND '.join(where)}
                ORDER BY l.created_at DESC
                LIMIT 20
            """, sql_params)
            results = [{
                "id": r[0], "title": r[1], "price": r[2], "city": r[3],
                "condition": r[4], "photos": r[5] if r[5] else [],
                "status": r[6], "views": r[7], "ai_verdict": r[8],
                "ai_market_min": r[9], "ai_market_max": r[10],
                "created_at": r[11].isoformat() if r[11] else None,
                "seller_name": r[12], "seller_username": r[13]
            } for r in cur.fetchall()]

            # Build separate WHERE for market_listings (different field names)
            market_where = [
                "listing_url IS NOT NULL",
                "listing_url != ''",
                "ts > NOW() - INTERVAL '60 days'"
            ]
            market_params = []
            # Use same keywords but with correct field names for this table
            if params.get('keywords'):
                kw_conditions = []
                for kw in params['keywords']:
                    kw_conditions.append("title ILIKE %s")
                    market_params.append(f"%{kw}%")
                market_where.append(f"({' OR '.join(kw_conditions)})")
            if params.get('category'):
                market_where.append("category ILIKE %s")
                market_params.append(f"%{params['category']}%")
            if params.get('city'):
                market_where.append("region ILIKE %s")
                market_params.append(f"%{params['city']}%")
            if params.get('max_price'):
                market_where.append("price <= %s")
                market_params.append(params['max_price'])
            if params.get('min_price'):
                market_where.append("price >= %s")
                market_params.append(params['min_price'])

            market_where_str = ' AND '.join(market_where)
            cur.execute(f"""
                SELECT id, title, price, region, listing_url,
                       category, verdict, ts
                FROM market_listings
                WHERE {market_where_str}
                ORDER BY ts DESC
                LIMIT 5
            """, market_params)
            market_results = [{
                "id": r[0],
                "title": r[1],
                "price": r[2],
                "region": r[3],
                "listing_url": r[4],
                "category": r[5],
                "verdict": r[6],
                "ts": r[7].isoformat() if r[7] else None,
                "is_market": True,
                "source": "avito"
            } for r in cur.fetchall()]

    return {"results": results, "market_results": market_results}


def _keyword_fallback(text: str) -> list:
    return [w for w in (text or '').split()[:5] if len(w) >= 3]


def _build_external_links(params: dict) -> list:
    # fix #6: never build links from an empty query
    search_q = (params.get('search_query') or ' '.join(params['keywords'])).strip()
    if not search_q:
        return []
    return [{"name": "Avito", "url": f"https://www.avito.ru/all?q={quote(search_q)}"}]


@app.post("/search/ai/dialog")
async def ai_search_dialog(data: dict = Body(...), authorization: str = Header(None)):
    user = get_current_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    query = (data.get('query') or '').strip()[:500]
    history = data.get('history')
    if not isinstance(history, list):
        history = []
    # Keep only well-formed turns, cap to last 8 to bound prompt size
    history = [h for h in history
               if isinstance(h, dict) and h.get('role') in ('user', 'assistant')
               and isinstance(h.get('content'), str)][-8:]

    # fix #5: compute step server-side from history; ignore any client step
    step = len(history) + (1 if query else 0)
    if step < 1:
        raise HTTPException(status_code=400, detail="Query required")
    if not history and len(query) < 3:
        raise HTTPException(status_code=400, detail="Query too short (min 3 chars)")

    # paid subscription
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_username FROM users WHERE id = %s", (user['user_id'],))
            user_row = cur.fetchone()
            if not user_row:
                raise HTTPException(status_code=404, detail="User not found")
            telegram_username = user_row[0] or ''
    if not check_paid_subscription(user['user_id'], telegram_username):
        raise HTTPException(status_code=403, detail="AI search requires paid subscription")

    # daily limit (counted on results turns)
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FROM ai_search_usage
                WHERE user_id = %s AND created_at > NOW() - INTERVAL '24 hours'
            """, (user['user_id'],))
            used_today = cur.fetchone()[0]
    if used_today >= DAILY_AI_SEARCH_LIMIT:
        raise HTTPException(status_code=429, detail=f"Daily limit reached ({DAILY_AI_SEARCH_LIMIT}/day)")

    must_results = step > AI_DIALOG_MAX_QUESTIONS
    convo = "\n".join(f"{h['role']}: {h['content']}" for h in history)
    convo += f"\nuser: {query}" if query else ""
    all_user_text = " ".join([h['content'] for h in history if h['role'] == 'user'] + ([query] if query else []))

    decision = None
    try:
        import anthropic as _anthropic
        import json as _json
        client = _anthropic.Anthropic(api_key=os.environ.get('ANTHROPIC_API_KEY', ''), timeout=12.0)
        instruction = (
            "Ты — ассистент поиска б/у пищевого оборудования на доске IndMart. "
            f"Это шаг {step} из максимум {AI_DIALOG_MAX_QUESTIONS} уточняющих вопросов. "
            + ("Ты ДОЛЖЕН вернуть type=results. " if must_results else
               "Если данных мало — задай ОДИН короткий уточняющий вопрос с 2-4 вариантами. Если данных достаточно — верни results. ")
            + "Категории: " + "|".join(AI_VALID_CATEGORIES) + ". "
            + "Верни ТОЛЬКО JSON без markdown, одной из форм:\n"
            '{"type":"question","question":"...","options":["...","..."]}\n'
            '{"type":"results","params":{"keywords":["..."],"category":null,"city":null,'
            '"min_price":null,"max_price":null,"condition":"used|new|null","search_query":"короткая строка"}}\n'
            "Диалог:\n" + convo
        )
        resp = client.messages.create(
            model=os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6'),
            max_tokens=400,
            messages=[{"role": "user", "content": instruction}],
        )
        text = resp.content[0].text.strip().strip('`').strip()
        if text.startswith('json'):
            text = text[4:].strip()
        decision = _json.loads(text)
    except Exception as e:
        logger.warning(f"AI dialog decision failed: {e}, using results fallback")
        decision = None

    # Decide: question vs results (server enforces max questions — fix #5 semantics)
    if (not must_results and isinstance(decision, dict)
            and decision.get('type') == 'question' and decision.get('question')):
        opts = decision.get('options')
        options = [str(o)[:80] for o in opts[:4]] if isinstance(opts, list) else []
        return {"type": "question", "question": str(decision['question'])[:300],
                "options": options, "step": step}

    # Results path
    if isinstance(decision, dict) and decision.get('type') == 'results' and isinstance(decision.get('params'), dict):
        params = _validate_search_params(decision['params'])
    else:
        params = _validate_search_params({})
    if not params['keywords']:
        params['keywords'] = _keyword_fallback(all_user_text)

    external_links = _build_external_links(params)
    search_data = _search_listings(params)
    results = search_data["results"]
    market_results = search_data["market_results"]

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ai_search_usage (user_id, query, results_count) VALUES (%s, %s, %s)",
                (user['user_id'], (all_user_text or query)[:500], len(results))
            )
            conn.commit()

    return {
        "type": "results",
        "results": results,
        "total": len(results),
        "market_results": market_results,
        "external_links": external_links,
        "remaining_today": max(0, DAILY_AI_SEARCH_LIMIT - used_today - 1),
        "daily_limit": DAILY_AI_SEARCH_LIMIT,
        "params": params,
        "step": step,
    }


@app.get("/admin/search/analytics")
def admin_search_analytics(authorization: str = Header(None)):
    check_admin(authorization)
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*),
                       COUNT(DISTINCT user_id),
                       COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days'),
                       COALESCE(AVG(results_count), 0)
                FROM ai_search_usage
            """)
            total, users, week, avg_results = cur.fetchone()

            cur.execute("""
                SELECT query, COUNT(*) AS c, COALESCE(AVG(results_count), 0)
                FROM ai_search_usage
                WHERE query IS NOT NULL AND query <> ''
                GROUP BY query
                ORDER BY c DESC
                LIMIT 20
            """)
            top_queries = [{"query": r[0], "count": r[1], "avg_results": round(float(r[2]), 1)}
                           for r in cur.fetchall()]

            cur.execute("""
                SELECT a.query, a.results_count, a.created_at, u.telegram_username
                FROM ai_search_usage a
                LEFT JOIN users u ON a.user_id = u.id
                ORDER BY a.created_at DESC
                LIMIT 20
            """)
            recent = [{"query": r[0], "results_count": r[1],
                       "created_at": r[2].isoformat() if r[2] else None,
                       "username": r[3]} for r in cur.fetchall()]

    return {
        "total_searches": total or 0,
        "unique_users": users or 0,
        "searches_week": week or 0,
        "avg_results": round(float(avg_results or 0), 1),
        "top_queries": top_queries,
        "recent": recent,
    }


# ---------- Market listings (Avito evaluated via extension) ----------

@app.get("/market/listings")
def get_market_listings(
    search: str = None,
    category: str = None,
    region: str = None,
    limit: int = 10,
    offset: int = 0
):
    limit = min(max(1, limit), 20)
    offset = max(0, offset)

    with get_db_conn() as conn:
        with conn.cursor() as cur:
            # Only show records with URL, not older than 60 days
            where = [
                "listing_url IS NOT NULL",
                "listing_url != ''",
                "ts > NOW() - INTERVAL '60 days'"
            ]
            params = []
            if search:
                where.append("title ILIKE %s")
                params.append(f"%{search[:100]}%")
            if category:
                where.append("category ILIKE %s")
                params.append(f"%{category[:50]}%")
            if region:
                where.append("region ILIKE %s")
                params.append(f"%{region[:50]}%")

            where_str = ' AND '.join(where)
            cur.execute(f"""
                SELECT id, title, price, region, listing_url,
                       category, brand, model, verdict, ts
                FROM market_listings
                WHERE {where_str}
                ORDER BY ts DESC
                LIMIT %s OFFSET %s
            """, params + [limit, offset])
            rows = cur.fetchall()

            cur.execute(
                f"SELECT COUNT(*) FROM market_listings WHERE {where_str}",
                params
            )
            total = cur.fetchone()[0]

    return {
        "listings": [{
            "id": r[0], "title": r[1], "price": r[2],
            "region": r[3], "listing_url": r[4],
            "category": r[5], "brand": r[6], "model": r[7],
            "verdict": r[8],
            "ts": r[9].isoformat() if r[9] else None
        } for r in rows],
        "total": total
    }
