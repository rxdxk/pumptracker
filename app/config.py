"""Настройки из .env (без внешних зависимостей)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ROOT / ".env")

HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")

if os.getenv("RPC_HTTP_URL"):
    RPC_HTTP_URL = os.environ["RPC_HTTP_URL"]
elif HELIUS_API_KEY:
    RPC_HTTP_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
else:
    RPC_HTTP_URL = "https://api.mainnet-beta.solana.com"

if os.getenv("RPC_WS_URL"):
    RPC_WS_URL = os.environ["RPC_WS_URL"]
elif HELIUS_API_KEY:
    RPC_WS_URL = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
else:
    RPC_WS_URL = RPC_HTTP_URL.replace("https://", "wss://").replace("http://", "ws://")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = ROOT / os.getenv("DB_PATH", "data/pumptracker.db")

# Сколько дней храним позиции кошельков для рейтинга умных денег
POSITIONS_RETENTION_DAYS = int(os.getenv("POSITIONS_RETENTION_DAYS", "7"))
# Сколько транзакций дева смотреть при подтягивании истории
BACKFILL_SIGNATURES = int(os.getenv("BACKFILL_SIGNATURES", "100"))


def redact(url: str) -> str:
    """Для логов: прячем api-key."""
    if "api-key=" in url:
        return url.split("api-key=")[0] + "api-key=***"
    return url
