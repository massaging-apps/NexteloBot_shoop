# Nextelo Shop Bot

Telegram email shop bot with bKash/Binance top-up, withdraw approval, stock manage.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env — BOT_TOKEN + ADMIN_ID
export $(grep -v '^#' .env | xargs)   # or set vars in shell
python bot.py
```

Or put token directly in `bot.py` settings block.

## Railway deploy (GitHub)

1. Push this folder to a GitHub repo.
2. Railway → New Project → Deploy from GitHub.
3. Add variables:

| Variable | Example |
|----------|---------|
| `BOT_TOKEN` | from @BotFather |
| `ADMIN_ID` | your numeric Telegram id |
| `BKASH_NUMBER` | 01... |
| `BINANCE_ID` | your id |
| `SHOP_NAME` | Nextelo Shop |
| `DEFAULT_PRICE` | 50 |
| `MIN_WITHDRAW` | 50 |

4. Railway detects `Procfile` → process type **worker** (not web).
5. Deploy. Check logs for `✅ Nextelo Shop`.

> SQLite on Railway is ephemeral unless you attach a volume. For production use a volume mounted at the DB path or switch to Postgres later.

## Features

- Member: buy, balance, recharge, withdraw, orders (email + datetime)
- Admin: pending payments ✅/❌, withdraw confirm/delete (name + Telegram ID), stock add/delete, price, broadcast, export
