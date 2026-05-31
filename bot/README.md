# ОборудованиеПро — Telegram bot

Mirror of the web AI consultant inside Telegram. Same Claude model, same
system prompts, same lead-capture flow that ends in a POST to the existing
`/leads` endpoint with `source=telegram_bot`.

## Setup

```bash
cd bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in this directory:

```
TELEGRAM_BOT_TOKEN=...
ANTHROPIC_API_KEY=sk-ant-...
API_BASE=https://web-production-d399a.up.railway.app   # optional, defaults to production
ANTHROPIC_MODEL=claude-sonnet-4-20250514               # optional
```

`TELEGRAM_BOT_TOKEN` can be the same token the backend already uses for
outbound `/leads` notifications — incoming long-polling and outbound
`sendMessage` do not conflict, they share the same bot identity.

## Run

```bash
python main.py
```

The bot uses long polling, so it just needs to be a running process —
no webhook configuration required. Logs go to stdout.

## Conversation flow

- `/start` — welcome + main menu (Купить / Обслуживание / Перекупщик)
- Pick a category, then a chat — mirrors the web app's home → category → chat hierarchy
- Send text or photos; both are relayed to Claude with the mode's system prompt
- Photos are downloaded from Telegram, base64-encoded, and sent as vision blocks
- Lead-aware chats detect `%%SHOW_LEAD_FORM%%` in Claude's reply and offer
  an inline "📝 Оставить заявку" button that walks through name → phone → city
  and POSTs to `/leads`
- `/menu` returns to the main menu; `/reset` clears the current chat history

## Modes included

| Mode | Lead-aware |
|---|---|
| 🆕 Новое оборудование (two-stage payback) | ✅ |
| ♻️ Б/у оборудование (market info + payback) | ✅ |
| 🏭 Цех под ключ (turnkey plan) | — |
| 🔧 Диагностика | — |
| ⚙️ Запчасти | — |
| 💼 Перекупщик — одна позиция | ✅ |

Modes from the web app that depend on structured table rendering
(`Линия`, `Выбрать из склада`, `Продать несколько позиций`,
`Продать одну позицию` with form prefill) are intentionally omitted in
this MVP — those work poorly in a text-only chat. To add one, append an
entry to `MODES` in `main.py` and the button will appear automatically
under its category.

## Notes

- Per-user state (current mode + chat history) lives in process memory.
  Restarting the bot clears every session. For production scale move
  `user_state` and `MemoryStorage` to Redis.
- Telegram caps a single message at 4096 characters — Claude replies
  longer than that are split into multiple messages.
- The system prompts in `main.py` are hand-copied from
  `../index.html`. Keep them aligned when you edit either side; there is
  no shared source of truth.
- Photos in chat history are kept as base64 blocks, so each subsequent
  Anthropic request re-sends them. Watch token usage if a user shares
  many photos in one conversation.
