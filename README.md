# RP

Telegram card-checker bot (aiogram v2).

## Requirements

- Python 3.10 – 3.13
- **aiogram 2.x** (v3 is NOT supported — the codebase uses the v2 Dispatcher /
  `@dp.message_handler` / `dp.start_polling` API)

## Install

```bash
pip install -r requirements.txt
```

If you previously installed aiogram v3, downgrade:

```bash
pip install --upgrade "aiogram>=2.25,<3"
```

## Configure

Edit `config.py` and set:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ADMIN_CODE` / `TELEGRAM_ADMIN`

## Run

```bash
python main.py
```

On startup the bot will exit immediately with a clear error if aiogram v3 is
installed — re-install v2 as shown above.
