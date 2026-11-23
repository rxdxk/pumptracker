# PumpTracker

Real-time **dev-wallet & smart-money tracker for pump.fun (Solana)** with a live web dashboard,
browser push notifications, sound alerts and optional Telegram alerts.

It listens to the pump.fun program directly over Solana WebSocket RPC, decodes on-chain events,
scores every token creator ("dev") by their track record and alerts you the moment something interesting happens.

🇷🇺 [Русская версия](README.ru.md)

## Features

**Live alert feed** (Server-Sent Events, no page reload)
- new tokens whose dev passes your filters
- launches by devs you track
- trades by wallets you track
- **smart money entering**: several strong wallets buying the same token
- migrations (bonding curve completed) and dev dumps

**Notifications**
- 🔔 browser notifications when the tab is in the background
- 🔊 sound alerts: each alert type has its own short WebAudio tune
- 📨 optional Telegram duplicates

**Dev ranking**
- dev score 0–100: migration rate, average ATH, dumps, serial-launcher penalty
- launch history for any dev; unknown devs are backfilled from RPC on their next launch

**Smart money**
- 7-day wallet leaderboard by PnL on pump.fun bonding curves
- your own tracked-wallet and scam-wallet lists

**Filters** are editable right in the UI and stored in SQLite.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt     # Windows
cp .env.example .env        # set HELIUS_API_KEY (or any RPC_HTTP_URL / RPC_WS_URL)
.venv\Scripts\python run.py
```

Open the dashboard at **http://127.0.0.1:8000**.

## How it works

1. `logsSubscribe` on the pump.fun program (`6EF8…F6P`) over WebSocket RPC.
2. Anchor events `CreateEvent`, `TradeEvent`, `CompleteEvent` are decoded from logs. Only events emitted by pump.fun itself are accepted: bot transactions sometimes carry look-alike events from other launchpads.
3. Tokens (creator, ATH, migration, dev actions) and wallet positions are stored in SQLite.
4. For an unknown dev, history is fetched from RPC on their next launch (last N txs → find `CreateEvent` → read bonding-curve state), with an hourly cap to protect RPC credits.
5. The alert engine pushes events to the dashboard via SSE and, if enabled, to Telegram.

### Dev score (0–100)

| Part | Max | What it measures |
|---|---|---|
| Migrations | 40 | share of the dev's tokens that migrated (max at 25%+) |
| Average ATH | 25 | how close their tokens get to migration on average |
| No dumps | 25 | 1 − share of tokens where the dev sold 80%+ before migration |
| Not serial | 10 | ≤10 launches = 10, fades to 0 at 50 launches |

65+ good · 40–64 average · below 40 poor.

## API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/stream` | SSE stream of live alerts |
| GET | `/api/alerts` | recent alerts |
| GET | `/api/tokens/recent` | recently created tokens |
| GET | `/api/token/{mint}` | token details |
| GET | `/api/dev/{wallet}` | dev profile & score |
| POST | `/api/dev/{wallet}/backfill` | fetch dev history from RPC |
| GET | `/api/devs/top` | top devs |
| GET | `/api/smart/leaderboard` | smart-money leaderboard |
| GET / POST / DELETE | `/api/tracked` | tracked wallets |
| GET / POST / DELETE | `/api/scam` | scam-wallet list |
| GET / PUT | `/api/settings` | filters & alert settings |
| POST | `/api/telegram/test` | send a test Telegram message |

## Project structure

```
app/parser.py     pump.fun event & bonding-curve decoding
app/rpc.py        HTTP RPC, dev history backfill, token metadata
app/db.py         SQLite (aiosqlite)
app/analytics.py  dev score, token views
app/core.py       stream ingestion, state, alert engine, Telegram
app/main.py       FastAPI: REST + SSE
web/              dashboard (vanilla HTML/CSS/JS, no build step)
run.py            entry point
```

## Telegram alerts (optional)

1. Create a bot with @BotFather and copy the token.
2. Message the bot once, then get your chat id (e.g. via @userinfobot).
3. Put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` into `.env`, restart, and enable Telegram in the **Filters** tab.

## Limitations

- History builds up from the moment the tracker starts: the longer it runs, the more accurate scores and rankings get.
- Wallet PnL counts only bonding-curve trades (pre-migration) that the tracker has seen.
- Some pump.fun tokens are not SOL-quoted or use a non-standard curve; for them MC and curve progress show "—", while dev, trades and dumps are still tracked.
- Free Helius plans limit concurrent WebSocket connections, so don't run two copies with the same key.

## Stack

Python · asyncio · FastAPI · Uvicorn · SSE · aiosqlite · websockets · aiohttp · Solana RPC · vanilla JS

---

*Not financial advice. This is an analytics tool; it does not trade.*
