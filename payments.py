import hashlib
import os

import httpx
from dotenv import load_dotenv

load_dotenv("/root/food-equipment-backend/.env")

TERMINAL_KEY = os.environ.get('TINKOFF_TERMINAL_KEY', '')
SECRET_KEY = os.environ.get('TINKOFF_SECRET_KEY', '')
TINKOFF_API = "https://securepay.tinkoff.ru/v2/"


def _token_value(v) -> str:
    # Tinkoff хеширует булевы значения как "true"/"false" в нижнем регистре,
    # а не как питоновское str(True) == "True".
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def generate_token(params: dict) -> str:
    """Подпись запроса/уведомления Tinkoff.

    Алгоритм Tinkoff: берём ТОЛЬКО корневые скалярные поля (вложенные объекты
    DATA/Receipt и сам Token исключаются), добавляем Password, сортируем по
    ключу, конкатенируем значения, SHA-256.
    """
    flat = {
        k: v for k, v in params.items()
        if k != "Token" and not isinstance(v, (dict, list))
    }
    flat["Password"] = SECRET_KEY
    values_string = ''.join(_token_value(v) for _, v in sorted(flat.items()))
    return hashlib.sha256(values_string.encode()).hexdigest()


def create_payment(amount_rub: int, order_id: str, telegram_username: str, description: str) -> dict:
    """Создаёт платёж в Tinkoff, возвращает ссылку на оплату."""
    amount_kopecks = amount_rub * 100
    params = {
        "TerminalKey": TERMINAL_KEY,
        "Amount": amount_kopecks,
        "OrderId": order_id,
        "Description": description,
        "NotificationURL": "https://indmart.ru/api/payment-webhook",
        "SuccessURL": "https://indmart.ru/#payment-success",
        "FailURL": "https://indmart.ru/#payment-fail",
        "DATA": {"telegram_username": telegram_username},
    }
    # DATA (вложенный объект) автоматически исключается из подписи в generate_token.
    params["Token"] = generate_token(params)
    resp = httpx.post(TINKOFF_API + "Init", json=params, timeout=15)
    data = resp.json()
    if data.get("Success"):
        return {"url": data["PaymentURL"], "payment_id": data["PaymentId"]}
    raise Exception(f"Tinkoff error: {data.get('Message', 'Unknown error')} / {data.get('Details', '')}")


def verify_webhook(params: dict) -> bool:
    """Проверка подписи входящего уведомления Tinkoff."""
    token = params.get("Token", "")
    if not token:
        return False
    return token == generate_token(params)
