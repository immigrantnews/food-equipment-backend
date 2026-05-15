# Food Equipment API

FastAPI-бэкенд для маркетплейса пищевого оборудования. Хранит лиды,
объявления и заявки «куплю» в Airtable, отвечает на вопросы пользователей
через Anthropic Claude и оценивает оборудование для перекупщиков.

## Стек

- **FastAPI** + **uvicorn** — HTTP-сервер
- **pyairtable** — клиент Airtable
- **anthropic** — официальный SDK Claude
- **pydantic-settings** — конфиг из `.env`
- **Хостинг**: Railway (Nixpacks)

## Эндпоинты

| Метод | Путь                     | Назначение                                     |
|-------|--------------------------|------------------------------------------------|
| GET   | `/health`                | Health-check для Railway                       |
| POST  | `/leads`                 | Сохранить лид покупателя в Airtable            |
| POST  | `/listings`              | Сохранить объявление продавца                  |
| GET   | `/listings`              | Список объявлений с фильтрами                  |
| GET   | `/listings/{id}`         | Одно объявление по Airtable record id          |
| POST  | `/want-to-buy`           | Сохранить заявку «куплю»                       |
| POST  | `/chat`                  | AI-чат через Anthropic                         |
| POST  | `/reseller/analyze`      | Оценка для перекупщика (закуп + перепродажа)   |

Интерактивная документация после запуска доступна на `/docs` и `/redoc`.

### Фильтры `GET /listings`

Query-параметры (все опциональны):

- `category` — например `oven`, `fridge`
- `condition` — `new` / `used` / `refurbished`
- `city`
- `brand`
- `price_min`, `price_max`
- `limit` — 1..100, по умолчанию 50

### Пример `POST /leads`

```json
{
  "name": "Иван Петров",
  "email": "ivan@example.com",
  "phone": "+7 999 123-45-67",
  "equipment_type": "Печь конвекционная",
  "budget": 3500,
  "city": "Москва",
  "message": "Нужна на 60 пицц/час",
  "source": "landing"
}
```

### Пример `POST /reseller/analyze`

```json
{
  "title": "Печь конвекционная UNOX XB693",
  "description": "5 уровней GN 1/1, 2018 г., рабочая, есть мелкие сколы",
  "category": "oven",
  "condition": "used",
  "year": 2018,
  "brand": "UNOX",
  "asking_price": 2200,
  "currency": "USD",
  "city": "Алматы"
}
```

Ответ:

```json
{
  "recommended_buy_price": 1700,
  "estimated_resale_price": 2800,
  "estimated_margin": 1100,
  "margin_percent": 64.7,
  "confidence": "medium",
  "rationale": "...",
  "risks": ["..."],
  "suggested_actions": ["..."]
}
```

### Пример `POST /chat`

```json
{
  "messages": [
    { "role": "user", "content": "Какую печь взять для маленькой пиццерии?" }
  ],
  "max_tokens": 800
}
```

## Структура Airtable

В базе должны быть три таблицы. Имена настраиваются через `.env`.

**Leads** — `Name`, `Email`, `Phone`, `EquipmentType`, `Budget`, `City`,
`Message`, `Source`.

**Listings** — `Title`, `Description`, `Category`, `Condition`, `Price`,
`Currency`, `City`, `SellerName`, `SellerEmail`, `SellerPhone`, `Photos`
(строка — URL через запятую), `Year`, `Brand`.

**WantToBuy** — `Name`, `Email`, `Phone`, `EquipmentType`, `BudgetMin`,
`BudgetMax`, `City`, `Details`, `Urgency`.

## Локальный запуск

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env       # заполнить ключи
uvicorn main:app --reload
```

Сервер по умолчанию поднимется на `http://localhost:8000`.

## Деплой на Railway

1. Создайте новый проект из GitHub-репозитория.
2. В разделе **Variables** задайте переменные:
   - `ANTHROPIC_API_KEY`
   - `AIRTABLE_TOKEN`
   - `AIRTABLE_BASE_ID`
   - (опц.) `AIRTABLE_LEADS_TABLE`, `AIRTABLE_LISTINGS_TABLE`,
     `AIRTABLE_WANT_TO_BUY_TABLE`, `ANTHROPIC_MODEL`, `CORS_ORIGINS`
3. Railway сам подхватит `Procfile` / `railway.json` и поднимет uvicorn
   на `$PORT`. Health-check: `/health`.

## Переменные окружения

| Переменная                   | Обязательная | Описание                              |
|------------------------------|--------------|---------------------------------------|
| `ANTHROPIC_API_KEY`          | да           | Ключ Anthropic                        |
| `AIRTABLE_TOKEN`             | да           | Personal Access Token Airtable        |
| `AIRTABLE_BASE_ID`           | да           | ID базы (`appXXXXXXXXXXXXXX`)         |
| `AIRTABLE_LEADS_TABLE`       | нет          | По умолчанию `Leads`                  |
| `AIRTABLE_LISTINGS_TABLE`    | нет          | По умолчанию `Listings`               |
| `AIRTABLE_WANT_TO_BUY_TABLE` | нет          | По умолчанию `WantToBuy`              |
| `ANTHROPIC_MODEL`            | нет          | По умолчанию `claude-sonnet-4-6`      |
| `CORS_ORIGINS`               | нет          | Через запятую; `*` по умолчанию       |
