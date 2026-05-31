"""ОборудованиеПро — Telegram bot mirror of the web AI consultant.

Each user picks a chat mode (Купить → Новое / Б/у / Цех под ключ,
Обслуживание → Диагностика / Запчасти, Перекупщик → Одна позиция).
The bot relays text and photos to Claude with the mode's system prompt
and per-user conversation history. Lead-aware chats detect the
%%SHOW_LEAD_FORM%% marker, walk the user through name/phone/city,
and POST to the existing /leads endpoint with source=telegram_bot.
"""

import asyncio
import base64
import logging
import os
from io import BytesIO

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    PhotoSize,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("food-equipment-bot")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
API_BASE = os.environ.get("API_BASE", "https://web-production-d399a.up.railway.app")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN env var is required")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY env var is required")

LEAD_MARKER = "%%SHOW_LEAD_FORM%%"

# Mirrors VISION_PROMPT in ../index.html. Keep these aligned when editing either.
VISION_PROMPT = (
    "\n\nКогда пользователь присылает фото оборудования, действуй по шагам:\n"
    "ШАГ 1: опиши что видишь — тип оборудования, возможный бренд.\n"
    "ШАГ 2: если шильдик (табличка с моделью) не виден на фото — ОБЯЗАТЕЛЬНО попроси сфотографировать шильдик.\n"
    "ШАГ 3: после получения шильдика точно определи бренд, модель, серийный номер, год выпуска.\n"
    "ШАГ 3.5: попроси сфотографировать ключевые узлы для оценки состояния. "
    "Печи/тепловое — горелки/тены, панель управления, внутренняя камера. "
    "Мясорубки/волчки — режущий механизм (ножи и решётки), горловина загрузки, редуктор. "
    "Тестомесы/куттеры — чаша изнутри, ножи/крюки, панель управления. "
    "Упаковочное — сварочные элементы, механизм подачи плёнки, панель управления. "
    "Общие — место подключения (электро/газ), повреждения и следы ремонта. "
    "По узловым фото комментируй износ, повреждения, общее состояние — это влияет на итоговую оценку.\n"
    "ШАГ 4: задавай уточняющие вопросы по ОДНОМУ за сообщение: 1) состояние (отлично/хорошее/рабочее/на запчасти); 2) работает ли сейчас; 3) что входит в комплект; 4) почему интересуетесь.\n"
    "ШАГ 5: итоговый ответ в формате:\n"
    "✅ Бренд: [название]\n"
    "✅ Модель: [модель]\n"
    "✅ Год выпуска: [год]\n"
    "✅ Рыночная стоимость б/у: [диапазон] руб"
)

MODES: dict[str, dict] = {
    "buy-new": {
        "label": "🆕 Новое оборудование",
        "category": "buy",
        "greeting": (
            "Здравствуйте! Я помогу подобрать промышленное пищевое оборудование. "
            "Опишите, что вы производите или планируете производить."
        ),
        "system": (
            "Ты консультант по пищевому оборудованию в России.\n\n"
            "ЭТАП 1 — БЫСТРЫЙ РАСЧЁТ. Задавай вопросы по одному за сообщение:\n"
            "1. В каком регионе планируете запускать производство?\n"
            "2. Что планируете производить?\n"
            "3. Какой объём в смену?\n"
            "4. По какой цене планируете продавать продукт?\n\n"
            "После сбора данных: подбери конкретные модели оборудования с ценами в рублях и быстрый расчёт окупаемости "
            "на ТИПОВЫХ нормах для региона. ОБЯЗАТЕЛЬНО укажи что данные типовые.\n\n"
            "В конце быстрого расчёта ВСЕГДА предложи: \"Хотите более точный расчёт? Займёт 3-5 минут — задам вопросы про ваши реальные затраты. Результат будет точнее 📊\"\n\n"
            "ЭТАП 2 — ТОЧНЫЙ РАСЧЁТ (если согласился). По одному вопросу за сообщение: "
            "сырьё на единицу, электричество (кВт·ч + тариф), зарплаты в месяц, аренда, упаковка на единицу. "
            "После всех ответов пересчитай и покажи СРАВНЕНИЕ быстрый vs точный.\n\n"
            "После завершения быстрого ИЛИ точного расчёта обязательно добавляй маркер " + LEAD_MARKER + " в конце сообщения."
        ),
        "lead_aware": True,
    },
    "buy-used": {
        "label": "♻️ Б/у оборудование",
        "category": "buy",
        "greeting": "Расскажу про рынок б/у пищевого оборудования: цены, надёжные бренды, риски. Какое оборудование вас интересует?",
        "system": (
            "Ты эксперт по рынку б/у промышленного пищевого оборудования в России.\n\n"
            "Сначала кратко расскажи про оборудование: средние цены б/у, надёжные бренды, "
            "ключевые риски покупки б/у, на что смотреть при осмотре.\n\n"
            "Затем переходи к расчёту окупаемости.\n"
            "ЭТАП 1 — БЫСТРЫЙ РАСЧЁТ. По одному вопросу за сообщение: регион, что производить, объём в смену, цена продажи. "
            "Затем модели б/у с ценами и быстрый расчёт окупаемости на типовых нормах (укажи что типовые).\n"
            "В конце ВСЕГДА предложи точный расчёт 3-5 минут.\n"
            "ЭТАП 2 — ТОЧНЫЙ РАСЧЁТ (по согласию): сырьё, электричество (кВт·ч + тариф), зарплаты, аренда, упаковка. "
            "Финальное сравнение быстрый vs точный.\n\n"
            "В конце предлагай оставить заявку — поищем варианты в каталоге. "
            "Когда пользователь готов — добавь маркер " + LEAD_MARKER
        ),
        "lead_aware": True,
    },
    "buy-turnkey": {
        "label": "🏭 Цех под ключ",
        "category": "buy",
        "greeting": "Спланируем цех под ключ. Что собираетесь производить и в каком объёме?",
        "system": (
            "Помоги спланировать цех под ключ.\n\n"
            "ЭТАП 1 — БЫСТРЫЙ ПЛАН И РАСЧЁТ. По одному вопросу за сообщение: регион, что производить, "
            "объём в смену, бюджет (новое/б/у/смешанное), цена продажи продукта.\n"
            "После данных: полный список оборудования с ценами, раздели на обязательное и желательное. "
            "Минимальный бюджет (обязательное) и оптимальный (всё). Срок окупаемости на ТИПОВЫХ нормах "
            "(укажи что типовые).\n\n"
            "В конце ВСЕГДА предложи: \"Хотите более точный расчёт окупаемости? Займёт 3-5 минут 📊\"\n\n"
            "ЭТАП 2 — ТОЧНЫЙ РАСЧЁТ (по согласию). По одному вопросу: сырьё на единицу, электричество "
            "(кВт·ч + тариф), зарплаты в месяц, аренда, упаковка. Финальное сравнение быстрый vs точный."
        ),
        "lead_aware": False,
    },
    "service": {
        "label": "🔧 Диагностика",
        "category": "service",
        "greeting": "Опишите симптом или поломку — помогу найти причину и решение.",
        "system": (
            "Ты опытный диагност промышленного пищевого оборудования в России. "
            "Пользователь описывает симптом или поломку. Задавай уточняющие вопросы "
            "по одному за сообщение, затем предлагай конкретные причины и решения: "
            "какие узлы проверить, какие детали могут быть неисправны, "
            "ориентировочные стоимости ремонта в рублях. Будь практичным."
        ),
        "lead_aware": False,
    },
    "parts": {
        "label": "⚙️ Запчасти",
        "category": "service",
        "greeting": "Назовите модель оборудования и нужную деталь — подберу подходящую запчасть.",
        "system": (
            "Ты специалист по запчастям для промышленного пищевого оборудования в России. "
            "Пользователь называет модель и нужную деталь — помоги подобрать запчасть. "
            "Уточняй параметры (артикул, размеры, производителя), предлагай аналоги если оригинал недоступен, "
            "ориентируй по ценам в рублях и местам, где можно заказать."
        ),
        "lead_aware": False,
    },
    "reseller-single": {
        "label": "💼 Перекупщик — одна позиция",
        "category": "reseller",
        "greeting": "Считаю арбитраж по б/у оборудованию: выкуп с дисконтом, перепродажа, маржа, оборот. Назовите модель.",
        "system": (
            "Ты помогаешь перекупщику промышленного оборудования зарабатывать на арбитраже. "
            "Когда называют оборудование — показывай: цена выкупа (с дисконтом 30-40% от рынка), "
            "цена перепродажи, маржа в рублях и процентах, срок оборота (как быстро продаётся), "
            "основные риски. Формат — чёткая таблица с цифрами. "
            "В конце предложи оставить заявку — поищем варианты под перепродажу. "
            "Когда пользователь готов — добавь маркер " + LEAD_MARKER
        ),
        "lead_aware": True,
    },
}

for _mode in MODES.values():
    _mode["system"] = _mode["system"] + VISION_PROMPT


anthropic_client = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# user_id -> {"mode": str | None, "history": list[dict]}
user_state: dict[int, dict] = {}


def get_state(user_id: int) -> dict:
    if user_id not in user_state:
        user_state[user_id] = {"mode": None, "history": []}
    return user_state[user_id]


class LeadStates(StatesGroup):
    awaiting_name = State()
    awaiting_phone = State()
    awaiting_city = State()


bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=None))
dp = Dispatcher(storage=MemoryStorage())


def strip_lead_marker(text: str) -> str:
    return text.replace(LEAD_MARKER, "").strip()


def chat_text_for_lead(history: list[dict]) -> str:
    """Flatten history into a single multi-line transcript for /leads."""
    lines: list[str] = []
    for m in history:
        label = "Пользователь" if m["role"] == "user" else "AI"
        content = m["content"]
        if isinstance(content, str):
            t = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    parts.append("[Фото]")
            t = " ".join(parts).strip()
        else:
            t = ""
        t = t.replace(LEAD_MARKER, "").strip()
        lines.append(f"{label}: {t}")
    text = "\n".join(lines)
    return text[:49000] + "\n…[усечено]" if len(text) > 49000 else text


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Купить оборудование", callback_data="cat:buy")],
        [InlineKeyboardButton(text="🔧 Обслуживание и ремонт", callback_data="cat:service")],
        [InlineKeyboardButton(text="💼 Я перекупщик", callback_data="cat:reseller")],
    ])


def category_menu_kb(category: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=mode["label"], callback_data=f"mode:{key}")]
        for key, mode in MODES.items()
        if mode["category"] == category
    ]
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="cat:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chat_anchor_kb(mode_key: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if MODES[mode_key].get("lead_aware"):
        rows.append([InlineKeyboardButton(text="📝 Оставить заявку", callback_data="lead:start")])
    rows.append([
        InlineKeyboardButton(text="↻ Начать заново", callback_data="reset"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="cat:home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Commands ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user_state.pop(message.from_user.id, None)
    await state.clear()
    await message.answer(
        "ОборудованиеПро — бесплатный AI-консультант для пищевых производств.\n\n"
        "Выберите раздел:",
        reply_markup=main_menu_kb(),
    )


@dp.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Выберите раздел:", reply_markup=main_menu_kb())


@dp.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext) -> None:
    s = get_state(message.from_user.id)
    s["history"] = []
    await state.clear()
    mode_key = s.get("mode")
    if mode_key and mode_key in MODES:
        await message.answer(
            f"Контекст очищен. {MODES[mode_key]['greeting']}",
            reply_markup=chat_anchor_kb(mode_key),
        )
    else:
        await message.answer("Контекст очищен. /menu — выбрать тему.")


# ---------- Inline-keyboard navigation ----------

@dp.callback_query(F.data == "cat:home")
async def cb_home(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text("Выберите раздел:", reply_markup=main_menu_kb())
    await cb.answer()


@dp.callback_query(F.data.startswith("cat:"))
async def cb_category(cb: CallbackQuery) -> None:
    category = cb.data.split(":", 1)[1]
    titles = {"buy": "🛒 Купить оборудование", "service": "🔧 Обслуживание и ремонт", "reseller": "💼 Перекупщик"}
    await cb.message.edit_text(titles.get(category, "Раздел"), reply_markup=category_menu_kb(category))
    await cb.answer()


@dp.callback_query(F.data.startswith("mode:"))
async def cb_mode(cb: CallbackQuery, state: FSMContext) -> None:
    mode_key = cb.data.split(":", 1)[1]
    if mode_key not in MODES:
        await cb.answer("Неизвестный режим.", show_alert=True)
        return
    await state.clear()
    s = get_state(cb.from_user.id)
    s["mode"] = mode_key
    s["history"] = []
    mode = MODES[mode_key]
    await cb.message.edit_text(
        f"{mode['label']}\n\n{mode['greeting']}\n\n"
        "Пишите сообщение или присылайте фото. /menu — вернуться к выбору.",
        reply_markup=chat_anchor_kb(mode_key),
    )
    await cb.answer()


@dp.callback_query(F.data == "reset")
async def cb_reset(cb: CallbackQuery, state: FSMContext) -> None:
    s = get_state(cb.from_user.id)
    s["history"] = []
    await state.clear()
    mode_key = s.get("mode")
    if mode_key and mode_key in MODES:
        await cb.message.answer(
            f"Контекст очищен. {MODES[mode_key]['greeting']}",
            reply_markup=chat_anchor_kb(mode_key),
        )
    else:
        await cb.message.answer("Контекст очищен.", reply_markup=main_menu_kb())
    await cb.answer()


@dp.callback_query(F.data == "lead:start")
async def cb_lead_start(cb: CallbackQuery, state: FSMContext) -> None:
    s = get_state(cb.from_user.id)
    if not s.get("mode"):
        await cb.answer("Сначала выберите тему через /menu.", show_alert=True)
        return
    await state.set_state(LeadStates.awaiting_name)
    await cb.message.answer("Оставим заявку. Как вас зовут?", reply_markup=ReplyKeyboardRemove())
    await cb.answer()


# ---------- Lead FSM ----------

@dp.message(LeadStates.awaiting_name)
async def lead_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=(message.text or "").strip())
    await state.set_state(LeadStates.awaiting_phone)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться телефоном", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer("Ваш телефон?", reply_markup=kb)


@dp.message(LeadStates.awaiting_phone)
async def lead_phone(message: Message, state: FSMContext) -> None:
    if message.contact and message.contact.phone_number:
        phone = message.contact.phone_number
    else:
        phone = (message.text or "").strip()
    await state.update_data(phone=phone)
    await state.set_state(LeadStates.awaiting_city)
    await message.answer("В каком городе?", reply_markup=ReplyKeyboardRemove())


@dp.message(LeadStates.awaiting_city)
async def lead_city(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    name = data.get("name", "")
    phone = data.get("phone", "")
    city = (message.text or "").strip()
    await state.clear()

    s = get_state(message.from_user.id)
    history = s.get("history", [])
    first_user = next((m for m in history if m["role"] == "user"), None)
    first_text = ""
    if first_user:
        c = first_user["content"]
        if isinstance(c, str):
            first_text = c
        elif isinstance(c, list):
            for b in c:
                if b.get("type") == "text":
                    first_text = b.get("text", "")
                    break

    payload = {
        "name": name,
        "phone": phone,
        "city": city,
        "message": first_text,
        "chat": chat_text_for_lead(history),
        "source": "telegram_bot",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.post(f"{API_BASE}/leads", json=payload)
            res.raise_for_status()
    except Exception as e:
        logger.exception("lead submit failed")
        await message.answer(f"Не удалось отправить заявку: {e}. Попробуйте позже.")
        return

    mode_key = s.get("mode") or ""
    await message.answer(
        "Спасибо! Мы свяжемся с вами в ближайшее время. 🤝",
        reply_markup=chat_anchor_kb(mode_key) if mode_key in MODES else main_menu_kb(),
    )


# ---------- Chat ----------

async def photo_to_base64(photo: PhotoSize) -> tuple[str, str]:
    file = await bot.get_file(photo.file_id)
    buf = BytesIO()
    await bot.download_file(file.file_path, destination=buf)
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode("ascii")


async def reply_with_claude(message: Message, s: dict) -> None:
    mode = MODES[s["mode"]]
    try:
        resp = await anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            system=mode["system"],
            messages=s["history"],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    except Exception as e:
        logger.exception("anthropic call failed")
        await message.answer(f"Ошибка AI: {e}")
        return

    s["history"].append({"role": "assistant", "content": text})
    show_lead = LEAD_MARKER in text and mode.get("lead_aware")
    cleaned = strip_lead_marker(text) or "(пустой ответ)"

    # Telegram caps a single message at 4096 chars — chunk if needed.
    for start in range(0, len(cleaned), 4000):
        await message.answer(cleaned[start:start + 4000])

    if show_lead:
        await message.answer(
            "Хотите оставить заявку? Свяжемся с вами в ближайшее время.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📝 Оставить заявку", callback_data="lead:start")],
                [InlineKeyboardButton(text="🏠 Меню", callback_data="cat:home")],
            ]),
        )


@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    s = get_state(message.from_user.id)
    if not s.get("mode") or s["mode"] not in MODES:
        await message.answer("Сначала выберите тему через /menu.", reply_markup=main_menu_kb())
        return
    photo = message.photo[-1]
    media_type, data = await photo_to_base64(photo)
    text = (message.caption or "").strip()
    content: list[dict] = [{
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }]
    if text:
        content.append({"type": "text", "text": text})
    s["history"].append({"role": "user", "content": content})
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await reply_with_claude(message, s)


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text(message: Message) -> None:
    s = get_state(message.from_user.id)
    if not s.get("mode") or s["mode"] not in MODES:
        await message.answer("Сначала выберите тему через /menu.", reply_markup=main_menu_kb())
        return
    s["history"].append({"role": "user", "content": message.text})
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    await reply_with_claude(message, s)


async def main() -> None:
    logger.info("Bot starting (long polling)…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
