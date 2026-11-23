"""Ядро: приём событий pump.fun, состояние токенов/девов/кошельков, алерты."""
from __future__ import annotations

import asyncio
import copy
import html
import json
import logging
import time
from collections import deque
from typing import Optional

import aiohttp
import websockets

from . import config
from .analytics import dev_profile, token_view
from .db import TOKEN_COLS, Database
from .parser import (PUMP_PROGRAM, CompleteEvent, CreateEvent, TradeEvent,
                     events_from_logs)
from .rpc import RpcClient, RpcError, backfill_dev, fetch_metadata, fetch_sol_usd

log = logging.getLogger("core")

DEFAULT_SETTINGS: dict = {
    "new_token": {
        "enabled": True,
        "dev_min_launches": 1,        # прошлых запусков не меньше
        "dev_max_launches": 0,        # 0 = без ограничения (отсечь серийных лончеров)
        "dev_min_migrated": 1,        # прошлых миграций не меньше
        "dev_max_dump_rate": 0.5,     # доля токенов, где дев слил (1 = не фильтровать)
        "dev_min_score": 0,
        "dev_buy_min_sol": 0.0,
        "dev_buy_max_sol": 0.0,       # 0 = без ограничения
        "require_socials": False,
        "keywords_include": "",
        "keywords_exclude": "",
        "backfill_unknown_dev": True,  # подтягивать историю незнакомого дева из RPC
        "backfill_per_hour": 60,       # бюджет запросов истории (экономит кредиты RPC)
        "backfill_depth": 15,          # без Helius: сколько последних транзакций дева смотреть
        "backfill_pages": 2,           # с Helius: страниц истории созданий (до 100 токенов каждая)
    },
    "migration": {"enabled": True},
    "scout": {"enabled": True, "per_hour": 30, "pages": 10},
    "dev_dump": {"enabled": True},
    "tracked": {"buys": True, "sells": True, "min_sol": 0.3, "dev_launch": True},
    "confluence": {"enabled": True, "min_wallets": 3, "window_min": 30, "use_top": True, "top_n": 50,
                   "smart_min_pnl": 3.0, "smart_min_winrate": 0.4, "smart_min_tokens": 5,
                   "smart_max_tokens": 300},
    "telegram": {"enabled": False},
}


def merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def is_smart(r: dict, cf: dict) -> bool:
    """Умный кошелёк: стабильно в плюсе и не похож на бота, торгующего всем подряд."""
    n = r.get("tokens") or 0
    return ((r.get("pnl") or 0) >= float(cf["smart_min_pnl"])
            and n >= int(cf["smart_min_tokens"]) and n <= int(cf["smart_max_tokens"])
            and (r.get("wins") or 0) / max(n, 1) >= float(cf["smart_min_winrate"]))


def new_token_row(mint: str) -> dict:
    row = {c: None for c in TOKEN_COLS}
    row.update(mint=mint, source="stub", dev_buy_sol=0.0, dev_tok_in=0.0, dev_tok_out=0.0,
               dev_sol_in=0.0, dev_sol_out=0.0, mcap_sol=0.0, ath_mcap_sol=0.0, volume_sol=0.0,
               buys=0, sells=0, completed=0, dev_dumped=0)
    return row


class Tracker:
    def __init__(self, db: Database):
        self.db = db
        self.rpc = RpcClient(config.RPC_HTTP_URL)
        self.http: Optional[aiohttp.ClientSession] = None
        self.settings: dict = copy.deepcopy(DEFAULT_SETTINGS)

        self.tokens: dict[str, dict] = {}
        self.dirty: set[str] = set()
        self.pos_deltas: dict[tuple[str, str], list] = {}
        self.tracked: dict[str, dict] = {}
        self.scam: dict[str, dict] = {}
        self.smart_top: set[str] = set()
        self.leaderboard_cache: list[dict] = []
        self.confluence: dict[str, dict[str, float]] = {}
        self.confluence_fired: dict[str, float] = {}
        self.alerted_mints: dict[str, float] = {}
        self.trade_alerted: dict[tuple, float] = {}

        self.subscribers: set[asyncio.Queue] = set()
        self.meta_sem = asyncio.Semaphore(6)
        self.backfill_sem = asyncio.Semaphore(2)
        self.backfill_times: deque[float] = deque()
        self.backfill_running: set[str] = set()
        self.scout_times: deque[float] = deque()

        self.stats = {"connected": False, "rpc": config.redact(config.RPC_WS_URL), "txs": 0,
                      "events": 0, "creates": 0, "trades": 0, "migrations": 0, "started": time.time(),
                      "last_event": None, "sol_usd": None, "errors": 0}
        self._rate = deque()
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.rpc.start()
        self.http = aiohttp.ClientSession(headers={"User-Agent": "pumptracker/0.1"})
        saved = await self.db.get_setting("alerts")
        self.settings = merge(DEFAULT_SETTINGS, saved or {})
        await self.reload_tracked()
        for coro in (self._ingest_loop(), self._flush_loop(), self._slow_loop()):
            self._tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self.flush()
        await self.rpc.close()
        if self.http:
            await self.http.close()

    async def reload_tracked(self) -> None:
        self.tracked = {r["wallet"]: r for r in await self.db.tracked()}
        self.scam = {r["wallet"]: r for r in await self.db.scam_list()}

    async def save_settings(self, new: dict) -> dict:
        self.settings = merge(DEFAULT_SETTINGS, new)
        await self.db.set_setting("alerts", self.settings)
        await self.refresh_leaderboard()
        return self.settings

    # ------------------------------------------------------------------ pub/sub
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, msg: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def alert(self, kind: str, mint: Optional[str], data: dict) -> None:
        aid = await self.db.add_alert(kind, mint, data)
        if mint:
            self.alerted_mints[mint] = time.time()
        msg = {"type": "alert", "id": aid, "ts": int(time.time()), "kind": kind, "mint": mint, "data": data}
        self.publish(msg)
        if self.settings["telegram"]["enabled"] and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
            asyncio.create_task(self._telegram(msg))

    # ------------------------------------------------------------------ ingest
    async def _ingest_loop(self) -> None:
        backoff = 1
        sub = {"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
               "params": [{"mentions": [PUMP_PROGRAM]}, {"commitment": "confirmed"}]}
        while True:
            try:
                async with websockets.connect(config.RPC_WS_URL, max_size=None, ping_interval=20,
                                              ping_timeout=30, open_timeout=15) as ws:
                    await ws.send(json.dumps(sub))
                    self.stats["connected"] = True
                    log.info("WS подключён: %s", config.redact(config.RPC_WS_URL))
                    backoff = 1
                    async for raw in ws:
                        msg = json.loads(raw)
                        value = msg.get("params", {}).get("result", {}).get("value")
                        if not value or value.get("err"):
                            continue
                        self.stats["txs"] += 1
                        try:
                            await self.handle_logs(value.get("logs") or [])
                        except Exception:  # noqa: BLE001
                            self.stats["errors"] += 1
                            log.exception("ошибка обработки %s", value.get("signature"))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("WS отключён: %s; переподключение через %ss", e, backoff)
            self.stats["connected"] = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def handle_logs(self, logs: list[str]) -> None:
        events = events_from_logs(logs)
        if not events:
            return
        now = time.time()
        self.stats["events"] += len(events)
        self.stats["last_event"] = now
        self._rate.append((now, len(events)))
        created: list[str] = []
        for ev in events:
            if isinstance(ev, CreateEvent):
                self._on_create(ev)
                created.append(ev.mint)
            elif isinstance(ev, TradeEvent):
                await self._on_trade(ev, in_create_tx=ev.mint in created)
            elif isinstance(ev, CompleteEvent):
                t = await self._token(ev.mint, None)
                await self._mark_completed(t, ev.timestamp)
        for mint in created:
            asyncio.create_task(self._evaluate_new_token(mint))

    async def _token(self, mint: str, creator_hint: Optional[str]) -> dict:
        t = self.tokens.get(mint)
        if t is None:
            row = await self.db.get_token(mint)
            if row is None:
                row = new_token_row(mint)
                row["creator"] = creator_hint
                row["created_at"] = int(time.time())
                self.dirty.add(mint)
            t = row
            self.tokens[mint] = t
        if not t.get("creator") and creator_hint:
            t["creator"] = creator_hint
            self.dirty.add(mint)
        return t

    def _on_create(self, ev: CreateEvent) -> None:
        self.stats["creates"] += 1
        t = self.tokens.get(ev.mint) or new_token_row(ev.mint)
        t.update(name=ev.name, symbol=ev.symbol, uri=ev.uri, creator=ev.creator,
                 bonding_curve=ev.bonding_curve, created_at=ev.timestamp or int(time.time()),
                 source="live")
        if not t["mcap_sol"]:
            t["mcap_sol"] = t["ath_mcap_sol"] = 28.0
        self.tokens[ev.mint] = t
        self.dirty.add(ev.mint)

    async def _on_trade(self, ev: TradeEvent, in_create_tx: bool) -> None:
        self.stats["trades"] += 1
        t = await self._token(ev.mint, ev.creator)
        sol, tok, ts = ev.sol, ev.tokens, ev.timestamp or int(time.time())
        sol_quoted = ev.standard_curve  # бывают кривые в другой котировке / с другими параметрами
        if sol_quoted:
            mcap = ev.mcap_sol
            t["mcap_sol"] = mcap
            t["ath_mcap_sol"] = max(t["ath_mcap_sol"] or 0, mcap)
            t["volume_sol"] = (t["volume_sol"] or 0) + sol
            t["curve_left"] = ev.real_token_reserves / 1e6
        elif t.get("source") == "live" and t.get("mcap_sol") == 28.0 and not t.get("buys"):
            t["mcap_sol"] = t["ath_mcap_sol"] = 0.0  # стартовая оценка неверна для не-SOL кривой
        t["last_trade_at"] = ts
        if ev.is_buy:
            t["buys"] = (t["buys"] or 0) + 1
        else:
            t["sells"] = (t["sells"] or 0) + 1

        is_dev = ev.user == t.get("creator")
        if is_dev:
            if ev.is_buy:
                t["dev_tok_in"] += tok
                t["dev_sol_in"] += sol
                if in_create_tx:
                    t["dev_buy_sol"] += sol
            else:
                t["dev_tok_out"] += tok
                t["dev_sol_out"] += sol
                if (not t["completed"] and not t["dev_dumped"] and t["dev_tok_in"] > 0
                        and t["dev_tok_out"] >= 0.8 * t["dev_tok_in"]):
                    t["dev_dumped"] = 1
                    t["dev_dumped_at"] = ts
                    if self.settings["dev_dump"]["enabled"] and ev.mint in self.alerted_mints:
                        await self.alert("dev_dump", ev.mint, {
                            "token": token_view(t), "sol": t["dev_sol_out"],
                            "dev_pnl": t["dev_sol_out"] - t["dev_sol_in"]})
        self.dirty.add(ev.mint)

        if not sol_quoted:
            await self._check_wallet_trade(ev, t)
            return

        # позиция кошелька (PnL считаем только в SOL)
        key = (ev.user, ev.mint)
        d = self.pos_deltas.get(key)
        if d is None:
            d = self.pos_deltas[key] = [0.0, 0.0, 0.0, 0.0, ts, ts]
        if ev.is_buy:
            d[0] += sol
            d[2] += tok
        else:
            d[1] += sol
            d[3] += tok
        d[5] = ts

        if ev.is_buy and ev.real_token_reserves == 0 and not t["completed"]:
            await self._mark_completed(t, ts)

        await self._check_wallet_trade(ev, t)

    async def _mark_completed(self, t: dict, ts: int) -> None:
        if t["completed"]:
            return
        t["completed"] = 1
        t["completed_at"] = ts or int(time.time())
        self.dirty.add(t["mint"])
        self.stats["migrations"] += 1
        creator = t.get("creator")
        if creator and self.settings["scout"]["enabled"]:
            asyncio.create_task(self._scout(creator))
        if self.settings["migration"]["enabled"]:
            dev = await self._dev(t.get("creator"), exclude=t["mint"]) if t.get("creator") else None
            await self.alert("migration", t["mint"], {"token": token_view(t), "dev": dev})

    async def _check_wallet_trade(self, ev: TradeEvent, t: dict) -> None:
        tr = self.tracked.get(ev.user)
        s = self.settings
        if tr and tr.get("kind") != "dev":
            want = s["tracked"]["buys"] if ev.is_buy else s["tracked"]["sells"]
            # У кривых с котировкой не в SOL сумма в событии = 0: порог по SOL к ним не применяем
            sol_known = ev.standard_curve
            key = (ev.user, ev.mint, ev.is_buy)
            fresh = time.time() - self.trade_alerted.get(key, 0) > 300  # антиспам: раз в 5 мин
            if want and fresh and (not sol_known or ev.sol >= float(s["tracked"]["min_sol"] or 0)):
                self.trade_alerted[key] = time.time()
                await self.alert("wallet_trade", ev.mint, {
                    "wallet": ev.user, "label": tr.get("label"), "side": "buy" if ev.is_buy else "sell",
                    "sol": ev.sol if sol_known else None, "tokens": ev.tokens,
                    "token": token_view(t)})

        cf = s["confluence"]
        if not (cf["enabled"] and ev.is_buy):
            return
        smart = (tr is not None and tr.get("kind") != "dev") or (cf["use_top"] and ev.user in self.smart_top)
        if not smart:
            return
        now = time.time()
        window = float(cf["window_min"]) * 60
        bucket = self.confluence.setdefault(ev.mint, {})
        bucket[ev.user] = now
        for w in [w for w, ts in bucket.items() if now - ts > window]:
            del bucket[w]
        if len(bucket) >= int(cf["min_wallets"]) and now - self.confluence_fired.get(ev.mint, 0) > window:
            self.confluence_fired[ev.mint] = now
            wallets = [{"wallet": w, "label": (self.tracked.get(w) or {}).get("label")} for w in bucket]
            await self.alert("confluence", ev.mint, {"token": token_view(t), "wallets": wallets,
                                                     "window_min": cf["window_min"]})

    # ------------------------------------------------------------------ new token evaluation
    async def _dev(self, wallet: str, exclude: str = "") -> dict:
        prof = dev_profile(await self.db.dev_stats_raw(wallet, exclude))
        prof["wallet"] = wallet
        tr = self.tracked.get(wallet)
        prof["tracked"] = bool(tr)
        prof["label"] = tr.get("label") if tr else None
        self.apply_scam(prof)
        hist =(await self.db.dev_recent_outcomes([wallet])).get(wallet, [])
        prof["history"] = [
            {"mint": h["mint"], "symbol": h["symbol"], "ath": h["ath_mcap_sol"],
             "outcome": "migrated" if h["completed"] else "dumped" if h["dev_dumped"] else "other"}
            for h in hist if h["mint"] != exclude
        ]
        return prof

    def apply_scam(self, prof: dict) -> dict:
        """Ручная пометка «скам» перекрывает расчётный скор."""
        sc = self.scam.get(prof.get("wallet"))
        prof["scam"] = bool(sc)
        prof["scam_note"] = sc.get("note") if sc else None
        prof["scam_at"] = sc.get("added_at") if sc else None
        if sc:
            prof["tone"] = "scam"
            prof["flags"] = ["скам"] + [f for f in prof.get("flags", []) if f != "новый дев"]
        return prof

    def _backfill_budget_ok(self) -> bool:
        per_hour = int(self.settings["new_token"]["backfill_per_hour"] or 0)
        now = time.time()
        while self.backfill_times and now - self.backfill_times[0] > 3600:
            self.backfill_times.popleft()
        return len(self.backfill_times) < per_hour

    async def backfill(self, wallet: str, depth: int, pages: int = 0) -> int:
        """depth — транзакций для обычного RPC, pages — страниц Enhanced API Helius."""
        if wallet in self.backfill_running:
            return 0
        self.backfill_running.add(wallet)
        try:
            async with self.backfill_sem:
                self.backfill_times.append(time.time())
                rows = await backfill_dev(self.rpc, wallet, depth, self.http, pages)
                for r in rows:
                    await self.db.insert_backfill_token(r)
                await self.db.mark_backfilled(wallet, len(rows))
                return len(rows)
        finally:
            self.backfill_running.discard(wallet)

    async def _scout(self, wallet: str) -> None:
        """Разведчик: дев только что вывел токен на DEX — подтягиваем его полную историю,
        чтобы рейтинг «Лучшие девы» наполнялся сам."""
        sc = self.settings["scout"]
        now = time.time()
        while self.scout_times and now - self.scout_times[0] > 3600:
            self.scout_times.popleft()
        raw = await self.db.dev_stats_raw(wallet)
        if raw.get("backfilled_at") and now - raw["backfilled_at"] < 86400:
            return
        if len(self.scout_times) >= int(sc["per_hour"]):
            return
        self.scout_times.append(now)
        try:
            found = await self.backfill(wallet, 100, pages=int(sc["pages"]))
            self.stats["scouted"] = self.stats.get("scouted", 0) + 1
            log.info("разведчик: дев %s — найдено запусков %s", wallet[:6], found)
        except RpcError as e:
            log.debug("scout %s: %s", wallet, e)

    async def _metadata(self, t: dict) -> None:
        async with self.meta_sem:
            soc = await fetch_metadata(self.http, t.get("uri") or "")
        if soc:
            t.update({k: soc.get(k) for k in ("twitter", "telegram", "website", "image")})
            self.dirty.add(t["mint"])

    async def _evaluate_new_token(self, mint: str) -> None:
        t = self.tokens.get(mint)
        if not t:
            return
        nt = self.settings["new_token"]
        creator = t.get("creator")
        started = time.time()

        meta_task = asyncio.create_task(self._metadata(t))
        needs_history = (nt["dev_min_launches"] > 0 or nt["dev_min_migrated"] > 0 or
                         nt["dev_max_launches"] > 0 or float(nt["dev_max_dump_rate"]) < 1 or
                         nt["dev_min_score"] > 0)
        dev = await self._dev(creator, exclude=mint)
        if (nt["enabled"] and needs_history and nt["backfill_unknown_dev"]
                and not dev["backfilled_at"] and self._backfill_budget_ok()):
            try:
                await self.backfill(creator, int(nt["backfill_depth"]), pages=int(nt["backfill_pages"]))
                dev = await self._dev(creator, exclude=mint)
            except RpcError as e:
                log.debug("backfill %s: %s", creator, e)

        if nt["require_socials"]:
            await meta_task
        else:
            try:
                await asyncio.wait_for(asyncio.shield(meta_task), timeout=3)
            except asyncio.TimeoutError:
                pass

        view = token_view(t)
        self.publish({"type": "token", "token": view, "dev": dev})

        tr = self.tracked.get(creator)
        if tr and tr.get("kind") == "dev" and self.settings["tracked"]["dev_launch"]:
            await self.alert("tracked_dev", mint, {"token": view, "dev": dev})
            return

        if not nt["enabled"]:
            return
        ok, reasons = self._passes(nt, t, dev)
        if ok:
            await self.alert("new_token", mint, {"token": view, "dev": dev, "reasons": reasons,
                                                 "checked_in": round(time.time() - started, 1)})

    @staticmethod
    def _passes(nt: dict, t: dict, dev: dict) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if dev.get("scam"):
            return False, []
        L, M = dev["launches"], dev["migrated"]
        if L < int(nt["dev_min_launches"]):
            return False, []
        if int(nt["dev_max_launches"]) and L > int(nt["dev_max_launches"]):
            return False, []
        if M < int(nt["dev_min_migrated"]):
            return False, []
        if M:
            reasons.append(f"у дева {M} миграц. из {L}")
        dr = dev["dump_rate"]
        if float(nt["dev_max_dump_rate"]) < 1 and dr is not None and dr > float(nt["dev_max_dump_rate"]):
            return False, []
        if int(nt["dev_min_score"]) and (dev["score"] or 0) < int(nt["dev_min_score"]):
            return False, []
        if dev["score"] is not None:
            reasons.append(f"скор дева {dev['score']}")
        buy = t.get("dev_buy_sol") or 0
        if float(nt["dev_buy_min_sol"]) and buy < float(nt["dev_buy_min_sol"]):
            return False, []
        if float(nt["dev_buy_max_sol"]) and buy > float(nt["dev_buy_max_sol"]):
            return False, []
        if nt["require_socials"]:
            if not (t.get("twitter") or t.get("telegram") or t.get("website")):
                return False, []
            reasons.append("есть соцсети")
        text = f"{t.get('name') or ''} {t.get('symbol') or ''}".lower()
        inc = [w.strip().lower() for w in str(nt["keywords_include"]).split(",") if w.strip()]
        exc = [w.strip().lower() for w in str(nt["keywords_exclude"]).split(",") if w.strip()]
        if inc and not any(w in text for w in inc):
            return False, []
        if any(w in text for w in exc):
            return False, []
        if inc:
            reasons.append("ключевое слово")
        return True, reasons

    # ------------------------------------------------------------------ background
    async def flush(self) -> None:
        if self.dirty:
            rows = [self.tokens[m] for m in self.dirty if m in self.tokens]
            self.dirty = set()
            await self.db.upsert_tokens(rows)
        if self.pos_deltas:
            deltas, self.pos_deltas = self.pos_deltas, {}
            await self.db.apply_position_deltas(deltas)
        await self.db.conn.commit()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            try:
                await self.flush()
                self._evict()
            except Exception:  # noqa: BLE001
                log.exception("flush")

    def _evict(self) -> None:
        if len(self.tokens) < 20000:
            return
        now = time.time()
        for m in [m for m, t in self.tokens.items()
                  if m not in self.dirty and now - (t.get("last_trade_at") or t.get("created_at") or 0) > 1800]:
            del self.tokens[m]
        for d in (self.confluence, self.confluence_fired, self.alerted_mints, self.trade_alerted):
            for k in [k for k, v in d.items() if (isinstance(v, float) and now - v > 7200) or (isinstance(v, dict) and not v)]:
                del d[k]

    async def refresh_leaderboard(self) -> None:
        since = int(time.time()) - config.POSITIONS_RETENTION_DAYS * 86400
        self.leaderboard_cache = await self.db.leaderboard(since, limit=200)
        cf = self.settings["confluence"]
        smart = [r for r in self.leaderboard_cache if is_smart(r, cf)]
        self.smart_top = {r["wallet"] for r in smart[:int(cf["top_n"])]}

    async def _slow_loop(self) -> None:
        last_prune = 0.0
        while True:
            try:
                px = await fetch_sol_usd(self.http)
                if px:
                    self.stats["sol_usd"] = px
                await self.refresh_leaderboard()
                if time.time() - last_prune > 3600:
                    await self.db.prune_positions(int(time.time()) - config.POSITIONS_RETENTION_DAYS * 86400)
                    last_prune = time.time()
            except Exception:  # noqa: BLE001
                log.exception("slow loop")
            await asyncio.sleep(300)

    def rate(self) -> float:
        now = time.time()
        while self._rate and now - self._rate[0][0] > 30:
            self._rate.popleft()
        return round(sum(n for _, n in self._rate) / 30, 1)

    # ------------------------------------------------------------------ telegram
    async def _telegram(self, msg: dict) -> None:
        text = format_telegram(msg, self.stats.get("sol_usd"))
        if not text:
            return
        url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            async with self.http.post(url, json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text,
                                                 "parse_mode": "HTML", "disable_web_page_preview": True},
                                      timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    log.warning("telegram: %s %s", r.status, await r.text())
        except Exception as e:  # noqa: BLE001
            log.warning("telegram: %s", e)


TITLES = {
    "new_token": "🚀 Новый токен прошёл фильтры",
    "tracked_dev": "👀 Отслеживаемый дев запустил токен",
    "migration": "🎓 Миграция на DEX",
    "wallet_trade": "💼 Сделка отслеживаемого кошелька",
    "confluence": "🔥 Умные деньги заходят",
    "dev_dump": "🩸 Дев слил токен",
}


def format_telegram(msg: dict, sol_usd: Optional[float]) -> str:
    d = msg["data"]
    t = d.get("token") or {}
    e = html.escape
    mc = t.get("mcap_sol") or 0
    usd = f" (${mc * sol_usd:,.0f})" if sol_usd else ""
    lines = [f"<b>{TITLES.get(msg['kind'], msg['kind'])}</b>",
             f"<b>{e(t.get('name') or '?')}</b> ${e(t.get('symbol') or '')}",
             f"<code>{t.get('mint')}</code>",
             f"MC: {mc:.1f} SOL{usd}" if mc else "MC: — (кривая не в SOL)"]
    dev = d.get("dev")
    if dev:
        sc = dev.get("score")
        lines.append(f"Дев: запусков {dev['launches']}, миграций {dev['migrated']}"
                     + (f", скор {sc}" if sc is not None else ""))
        if dev.get("scam"):
            lines.append("🚩 Дев помечен как СКАМ" + (f": {e(dev['scam_note'])}" if dev.get("scam_note") else ""))
    if msg["kind"] == "wallet_trade":
        side = "купил" if d["side"] == "buy" else "продал"
        amount = f"на {d['sol']:.2f} SOL" if d.get("sol") is not None else f"{d.get('tokens') or 0:,.0f} токенов (не за SOL)"
        lines.append(f"{e(d.get('label') or d['wallet'][:6])} {side} {amount}")
    if msg["kind"] == "confluence":
        lines.append(f"Кошельков: {len(d['wallets'])} за {d['window_min']} мин")
    if d.get("reasons"):
        lines.append("✓ " + ", ".join(e(r) for r in d["reasons"]))
    lines.append(f"<a href=\"https://pump.fun/coin/{t.get('mint')}\">pump.fun</a>")
    return "\n".join(lines)
