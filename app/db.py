"""SQLite-хранилище: токены, позиции кошельков, отслеживаемые кошельки, алерты, настройки."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS tokens (
    mint TEXT PRIMARY KEY,
    name TEXT, symbol TEXT, uri TEXT,
    creator TEXT,
    bonding_curve TEXT,
    created_at INTEGER,
    source TEXT DEFAULT 'live',          -- live | stub | backfill
    dev_buy_sol REAL DEFAULT 0,          -- покупка дева в транзакции создания
    dev_tok_in REAL DEFAULT 0, dev_tok_out REAL DEFAULT 0,
    dev_sol_in REAL DEFAULT 0, dev_sol_out REAL DEFAULT 0,
    mcap_sol REAL DEFAULT 0, ath_mcap_sol REAL DEFAULT 0,
    volume_sol REAL DEFAULT 0,
    curve_left REAL,                     -- сколько токенов осталось на кривой
    buys INTEGER DEFAULT 0, sells INTEGER DEFAULT 0,
    completed INTEGER DEFAULT 0, completed_at INTEGER,
    dev_dumped INTEGER DEFAULT 0, dev_dumped_at INTEGER,
    last_trade_at INTEGER,
    twitter TEXT, telegram TEXT, website TEXT, image TEXT
);
CREATE INDEX IF NOT EXISTS ix_tokens_creator ON tokens(creator);
CREATE INDEX IF NOT EXISTS ix_tokens_created ON tokens(created_at);

CREATE TABLE IF NOT EXISTS positions (
    wallet TEXT, mint TEXT,
    sol_in REAL DEFAULT 0, sol_out REAL DEFAULT 0,
    tok_in REAL DEFAULT 0, tok_out REAL DEFAULT 0,
    first_at INTEGER, last_at INTEGER,
    PRIMARY KEY (wallet, mint)
);
CREATE INDEX IF NOT EXISTS ix_pos_last ON positions(last_at);
CREATE INDEX IF NOT EXISTS ix_pos_mint ON positions(mint);

CREATE TABLE IF NOT EXISTS tracked (
    wallet TEXT PRIMARY KEY,
    label TEXT,
    kind TEXT DEFAULT 'trader',          -- trader | dev
    added_at INTEGER
);

CREATE TABLE IF NOT EXISTS dev_meta (
    wallet TEXT PRIMARY KEY,
    backfilled_at INTEGER,
    backfill_found INTEGER
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, kind TEXT, mint TEXT, data TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS scam (
    wallet TEXT PRIMARY KEY,
    note TEXT,
    added_at INTEGER
);
"""

TOKEN_COLS = [
    "mint", "name", "symbol", "uri", "creator", "bonding_curve", "created_at", "source",
    "dev_buy_sol", "dev_tok_in", "dev_tok_out", "dev_sol_in", "dev_sol_out",
    "mcap_sol", "ath_mcap_sol", "volume_sol", "curve_left", "buys", "sells",
    "completed", "completed_at", "dev_dumped", "dev_dumped_at", "last_trade_at",
    "twitter", "telegram", "website", "image",
]


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---------- токены ----------
    async def get_token(self, mint: str) -> Optional[dict]:
        cur = await self.conn.execute("SELECT * FROM tokens WHERE mint=?", (mint,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_tokens(self, rows: Iterable[dict]) -> None:
        rows = list(rows)
        if not rows:
            return
        cols = ",".join(TOKEN_COLS)
        qs = ",".join("?" * len(TOKEN_COLS))
        upd = ",".join(f"{c}=excluded.{c}" for c in TOKEN_COLS if c != "mint")
        await self.conn.executemany(
            f"INSERT INTO tokens ({cols}) VALUES ({qs}) ON CONFLICT(mint) DO UPDATE SET {upd}",
            [tuple(r.get(c) for c in TOKEN_COLS) for r in rows],
        )

    async def insert_backfill_token(self, row: dict) -> None:
        """Токен из истории дева: не перезаписываем живые данные."""
        await self.conn.execute(
            """INSERT INTO tokens (mint,name,symbol,uri,creator,bonding_curve,created_at,source,
                                   mcap_sol,ath_mcap_sol,completed)
               VALUES (?,?,?,?,?,?,?,'backfill',?,?,?)
               ON CONFLICT(mint) DO UPDATE SET
                 creator=COALESCE(tokens.creator, excluded.creator),
                 name=COALESCE(tokens.name, excluded.name),
                 symbol=COALESCE(tokens.symbol, excluded.symbol),
                 created_at=MIN(COALESCE(tokens.created_at, excluded.created_at), excluded.created_at),
                 completed=MAX(tokens.completed, excluded.completed),
                 ath_mcap_sol=MAX(tokens.ath_mcap_sol, excluded.ath_mcap_sol)""",
            (row["mint"], row["name"], row["symbol"], row["uri"], row["creator"],
             row["bonding_curve"], row["created_at"], row["mcap_sol"],
             row.get("ath_mcap_sol", row["mcap_sol"]), row["completed"]),
        )

    async def update_socials(self, mint: str, socials: dict) -> None:
        await self.conn.execute(
            "UPDATE tokens SET twitter=?, telegram=?, website=?, image=? WHERE mint=?",
            (socials.get("twitter"), socials.get("telegram"), socials.get("website"),
             socials.get("image"), mint),
        )

    async def recent_tokens(self, limit: int = 50) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM tokens WHERE source='live' ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def dev_tokens(self, wallet: str, limit: int = 200) -> list[dict]:
        cur = await self.conn.execute(
            "SELECT * FROM tokens WHERE creator=? ORDER BY created_at DESC LIMIT ?", (wallet, limit)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def dev_stats_raw(self, wallet: str, exclude_mint: str = "") -> dict:
        """Статистика дева. exclude_mint — не учитывать текущий токен (для оценки истории)."""
        cur = await self.conn.execute(
            """SELECT COUNT(*) AS launches,
                      COALESCE(SUM(completed),0) AS migrated,
                      COALESCE(SUM(dev_dumped),0) AS dumped,
                      COALESCE(SUM(CASE WHEN source='live' THEN 1 ELSE 0 END),0) AS observed,
                      COALESCE(AVG(ath_mcap_sol),0) AS avg_ath,
                      COALESCE(MAX(ath_mcap_sol),0) AS best_ath,
                      MIN(created_at) AS first_launch, MAX(created_at) AS last_launch
               FROM tokens WHERE creator=? AND mint != ?""",
            (wallet, exclude_mint),
        )
        row = dict(await cur.fetchone())
        meta = await self.conn.execute("SELECT * FROM dev_meta WHERE wallet=?", (wallet,))
        m = await meta.fetchone()
        row["backfilled_at"] = m["backfilled_at"] if m else None
        return row

    async def top_devs(self, mode: str = "best", limit: int = 100) -> list[dict]:
        """mode: best — с миграциями, all — 2+ запуска, bad — со сливами."""
        having = {
            "best": "SUM(completed) > 0",
            "all": "COUNT(*) >= 2",
            "bad": "SUM(dev_dumped) > 0",
        }.get(mode, "SUM(completed) > 0")
        order = {
            "best": "SUM(completed) DESC, AVG(ath_mcap_sol) DESC",
            "all": "MAX(created_at) DESC",
            "bad": "SUM(dev_dumped) DESC, COUNT(*) DESC",
        }.get(mode, "SUM(completed) DESC")
        cur = await self.conn.execute(
            f"""SELECT creator AS wallet, COUNT(*) AS launches,
                      SUM(completed) AS migrated, SUM(dev_dumped) AS dumped,
                      SUM(CASE WHEN source='live' THEN 1 ELSE 0 END) AS observed,
                      AVG(ath_mcap_sol) AS avg_ath, MAX(ath_mcap_sol) AS best_ath,
                      MIN(created_at) AS first_launch, MAX(created_at) AS last_launch
               FROM tokens WHERE creator IS NOT NULL
               GROUP BY creator HAVING {having}
               ORDER BY {order} LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def dev_recent_outcomes(self, wallets: list[str], per_dev: int = 12) -> dict[str, list[dict]]:
        """Последние запуски для полоски истории: миграция / слив / ATH."""
        if not wallets:
            return {}
        qs = ",".join("?" * len(wallets))
        cur = await self.conn.execute(
            f"""SELECT creator, mint, symbol, completed, dev_dumped, ath_mcap_sol, created_at
                FROM tokens WHERE creator IN ({qs}) ORDER BY created_at DESC""",
            wallets,
        )
        out: dict[str, list[dict]] = {}
        for r in await cur.fetchall():
            lst = out.setdefault(r["creator"], [])
            if len(lst) < per_dev:
                lst.append(dict(r))
        return out

    async def mark_backfilled(self, wallet: str, found: int) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO dev_meta (wallet, backfilled_at, backfill_found) VALUES (?,?,?)",
            (wallet, int(time.time()), found),
        )
        await self.conn.commit()

    # ---------- позиции ----------
    async def apply_position_deltas(self, deltas: dict) -> None:
        if not deltas:
            return
        await self.conn.executemany(
            """INSERT INTO positions (wallet,mint,sol_in,sol_out,tok_in,tok_out,first_at,last_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(wallet,mint) DO UPDATE SET
                 sol_in=sol_in+excluded.sol_in, sol_out=sol_out+excluded.sol_out,
                 tok_in=tok_in+excluded.tok_in, tok_out=tok_out+excluded.tok_out,
                 last_at=excluded.last_at""",
            [(w, m, d[0], d[1], d[2], d[3], d[4], d[5]) for (w, m), d in deltas.items()],
        )

    async def prune_positions(self, older_than: int) -> None:
        await self.conn.execute("DELETE FROM positions WHERE last_at < ?", (older_than,))
        await self.conn.execute("DELETE FROM alerts WHERE id < (SELECT MAX(id) - 5000 FROM alerts)")
        await self.conn.commit()

    async def leaderboard(self, since: int, limit: int = 50, min_tokens: int = 3) -> list[dict]:
        """PnL = продано + остаток по текущей цене − потрачено. Позиции по токенам до миграции."""
        cur = await self.conn.execute(
            """SELECT p.wallet,
                      COUNT(*) AS tokens,
                      SUM(p.sol_in) AS spent,
                      SUM(p.sol_out + MAX(p.tok_in - p.tok_out, 0) * COALESCE(t.mcap_sol,0) / 1e9
                          - p.sol_in) AS pnl,
                      SUM(CASE WHEN p.sol_out + MAX(p.tok_in - p.tok_out, 0) * COALESCE(t.mcap_sol,0) / 1e9
                               > p.sol_in THEN 1 ELSE 0 END) AS wins,
                      MAX(p.last_at) AS last_at
               FROM positions p LEFT JOIN tokens t ON t.mint = p.mint
               WHERE p.last_at >= ? AND p.sol_in > 0
               GROUP BY p.wallet
               HAVING COUNT(*) >= ? AND SUM(p.sol_in) >= 1
               ORDER BY pnl DESC LIMIT ?""",
            (since, min_tokens, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def wallet_positions(self, wallet: str, limit: int = 100) -> list[dict]:
        cur = await self.conn.execute(
            """SELECT p.*, t.name, t.symbol, t.mcap_sol, t.completed, t.creator,
                      p.sol_out + MAX(p.tok_in - p.tok_out, 0) * COALESCE(t.mcap_sol,0) / 1e9 - p.sol_in AS pnl
               FROM positions p LEFT JOIN tokens t ON t.mint = p.mint
               WHERE p.wallet=? ORDER BY p.last_at DESC LIMIT ?""",
            (wallet, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def token_top_wallets(self, mint: str, limit: int = 20) -> list[dict]:
        cur = await self.conn.execute(
            """SELECT wallet, sol_in, sol_out, tok_in, tok_out, first_at
               FROM positions WHERE mint=? ORDER BY (tok_in - tok_out) DESC LIMIT ?""",
            (mint, limit),
        )
        return [dict(r) for r in await cur.fetchall()]

    # ---------- отслеживаемые ----------
    async def tracked(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM tracked ORDER BY added_at DESC")
        return [dict(r) for r in await cur.fetchall()]

    async def add_tracked(self, wallet: str, label: str, kind: str) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO tracked (wallet,label,kind,added_at) VALUES (?,?,?,?)",
            (wallet, label, kind, int(time.time())),
        )
        await self.conn.commit()

    async def remove_tracked(self, wallet: str) -> None:
        await self.conn.execute("DELETE FROM tracked WHERE wallet=?", (wallet,))
        await self.conn.commit()

    # ---------- скам-девы ----------
    async def scam_list(self) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM scam ORDER BY added_at DESC")
        return [dict(r) for r in await cur.fetchall()]

    async def mark_scam(self, wallet: str, note: str) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO scam (wallet, note, added_at) VALUES (?,?,?)",
            (wallet, note, int(time.time())),
        )
        await self.conn.commit()

    async def unmark_scam(self, wallet: str) -> None:
        await self.conn.execute("DELETE FROM scam WHERE wallet=?", (wallet,))
        await self.conn.commit()

    # ---------- алерты ----------
    async def add_alert(self, kind: str, mint: Optional[str], data: dict) -> int:
        cur = await self.conn.execute(
            "INSERT INTO alerts (ts,kind,mint,data) VALUES (?,?,?,?)",
            (int(time.time()), kind, mint, json.dumps(data, ensure_ascii=False)),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def recent_alerts(self, limit: int = 100) -> list[dict]:
        cur = await self.conn.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["data"] = json.loads(d["data"])
            out.append(d)
        return out

    # ---------- настройки ----------
    async def get_setting(self, key: str) -> Optional[Any]:
        cur = await self.conn.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return json.loads(row["value"]) if row else None

    async def set_setting(self, key: str, value: Any) -> None:
        await self.conn.execute(
            "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await self.conn.commit()

    async def counts(self) -> dict:
        out = {}
        for name, q in [
            ("tokens", "SELECT COUNT(*) FROM tokens"),
            ("devs", "SELECT COUNT(DISTINCT creator) FROM tokens"),
            ("positions", "SELECT COUNT(*) FROM positions"),
        ]:
            cur = await self.conn.execute(q)
            out[name] = (await cur.fetchone())[0]
        return out
