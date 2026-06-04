import os
import secrets
from dotenv import load_dotenv
load_dotenv('/root/food-equipment-backend/.env')
import psycopg2
from psycopg2.extras import RealDictCursor

def get_conn():
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL not set')
    return psycopg2.connect(url)

def save_subscriber(name, telegram_username, region, categories, user_type, filters):
    token = secrets.token_urlsafe(32)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO subscribers
                (name, telegram_username, region, categories, user_type, min_margin, max_turnover, private_only, urgent_only, unsubscribe_token)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (
                name, telegram_username, region, categories, user_type,
                filters.get('minMargin', 0),
                filters.get('maxTurnover', 60),
                filters.get('privateOnly', False),
                filters.get('urgentOnly', False),
                token
            ))
            conn.commit()
            return cur.fetchone()[0], token

def get_matching_subscribers(region, margin, is_urgent, is_private, category=''):
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, name, telegram_username, telegram_chat_id FROM subscribers
                    WHERE is_active = true AND user_type = 'reseller'
                    AND (region IS NULL OR region = '' OR %s ILIKE '%%' || region || '%%')
                    AND (min_margin = 0 OR min_margin <= %s)
                    AND (private_only = false OR %s = true)
                    AND (urgent_only = false OR %s = true)
                    AND (categories IS NULL OR array_length(categories, 1) IS NULL
                         OR %s = ANY(categories) OR 'Всё оборудование' = ANY(categories))
                """, (region, margin, is_private, is_urgent, category))
                return cur.fetchall()
    except Exception:
        return []

def unsubscribe(token):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE subscribers SET is_active=false WHERE unsubscribe_token=%s RETURNING id", (token,))
            conn.commit()
            return cur.fetchone() is not None
