"""Веб-сервер: API + страница дашборда + SSE-поток событий."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .analytics import token_view
from .core import Tracker
from .db import Database
from .rpc import RpcError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

WEB = config.ROOT / "web"
PUBKEY_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

db = Database(config.DB_PATH)
tracker = Tracker(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.open()
    await tracker.start()
    log.info("Дашборд: http://%s:%s", config.HOST, config.PORT)
    yield
    await tracker.stop()
    await db.close()


app = FastAPI(title="PumpTracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=WEB), name="static")


def check_key(k: str) -> str:
    k = k.strip()
    if not PUBKEY_RE.match(k):
        raise HTTPException(400, "Некорректный адрес Solana")
    return k


@app.get("/")
async def index():
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
async def status():
    s = dict(tracker.stats)
    s["rate"] = tracker.rate()
    s["uptime"] = int(time.time() - s["started"])
    s.update(await db.counts())
    s["tracked"] = len(tracker.tracked)
    s["smart_top"] = len(tracker.smart_top)
    s["telegram_configured"] = bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)
    return s


@app.get("/api/alerts")
async def alerts(limit: int = 100):
    from .pda import bonding_curve_address
    out = await db.recent_alerts(min(limit, 500))
    for a in out:  # старые алерты сохранены без адреса кривой — нужен для ссылки на Axiom
        tok = a["data"].get("token")
        if isinstance(tok, dict) and tok.get("mint") and not tok.get("bonding_curve"):
            try:
                tok["bonding_curve"] = bonding_curve_address(tok["mint"])
            except ValueError:
                pass
    return out


@app.get("/api/tokens/recent")
async def recent_tokens(limit: int = 60):
    rows = await db.recent_tokens(min(limit, 200))
    out = []
    for r in rows:
        live = tracker.tokens.get(r["mint"]) or r
        dev = await tracker._dev(live["creator"], exclude=live["mint"]) if live.get("creator") else None
        out.append({"token": token_view(live), "dev": dev})
    return out


@app.get("/api/token/{mint}")
async def token(mint: str):
    mint = check_key(mint)
    t = tracker.tokens.get(mint) or await db.get_token(mint)
    if not t:
        raise HTTPException(404, "Токен ещё не встречался")
    dev = await tracker._dev(t["creator"], exclude=mint) if t.get("creator") else None
    holders = await db.token_top_wallets(mint)
    for h in holders:
        tr = tracker.tracked.get(h["wallet"])
        h["label"] = tr["label"] if tr else None
        h["smart"] = h["wallet"] in tracker.smart_top
    return {"token": token_view(t), "dev": dev, "wallets": holders}


@app.get("/api/dev/{wallet}")
async def dev(wallet: str):
    wallet = check_key(wallet)
    prof = await tracker._dev(wallet)
    toks = [token_view(tracker.tokens.get(t["mint"]) or t) for t in await db.dev_tokens(wallet)]
    return {"dev": prof, "tokens": toks}


@app.post("/api/dev/{wallet}/backfill")
async def dev_backfill(wallet: str, depth: int = config.BACKFILL_SIGNATURES, pages: int = 60):
    wallet = check_key(wallet)
    try:
        found = await tracker.backfill(wallet, min(max(depth, 10), 1000), pages=min(max(pages, 1), 200))
    except RpcError as e:
        raise HTTPException(502, f"RPC: {e}") from e
    return {"found": found, "dev": await tracker._dev(wallet)}


@app.get("/api/devs/top")
async def top_devs(mode: str = "best"):
    from .analytics import dev_profile
    await tracker.flush()
    if mode == "scam":
        rows = []
        for m in await db.scam_list():
            raw = await db.dev_stats_raw(m["wallet"])
            raw["wallet"] = m["wallet"]
            rows.append(raw)
    else:
        rows = [r for r in await db.top_devs(mode)
                if mode == "bad" or r["wallet"] not in tracker.scam]
    hist = await db.dev_recent_outcomes([r["wallet"] for r in rows])
    out = []
    for r in rows:
        p = dev_profile(r)
        p["wallet"] = r["wallet"]
        tr = tracker.tracked.get(r["wallet"])
        p["tracked"] = bool(tr)
        p["label"] = tr["label"] if tr else None
        tracker.apply_scam(p)
        p["history"] = [
            {"mint": h["mint"], "symbol": h["symbol"], "ath": h["ath_mcap_sol"],
             "outcome": "migrated" if h["completed"] else "dumped" if h["dev_dumped"] else "other"}
            for h in hist.get(r["wallet"], [])
        ]
        out.append(p)
    if mode == "best":
        out.sort(key=lambda p: (p["score"] or 0), reverse=True)
    return out


@app.get("/api/smart/leaderboard")
async def leaderboard():
    if not tracker.leaderboard_cache:
        await tracker.refresh_leaderboard()
    out = []
    for r in tracker.leaderboard_cache[:100]:
        r = dict(r)
        tr = tracker.tracked.get(r["wallet"])
        r["tracked"] = bool(tr)
        r["label"] = tr["label"] if tr else None
        r["smart"] = r["wallet"] in tracker.smart_top
        r["bot"] = (r["tokens"] or 0) > int(tracker.settings["confluence"]["smart_max_tokens"])
        out.append(r)
    return out


@app.post("/api/smart/refresh")
async def leaderboard_refresh():
    await tracker.flush()
    await tracker.refresh_leaderboard()
    return {"ok": True}


@app.get("/api/wallet/{wallet}")
async def wallet(wallet: str):
    wallet = check_key(wallet)
    pos = await db.wallet_positions(wallet)
    tr = tracker.tracked.get(wallet)
    return {
        "wallet": wallet, "label": tr["label"] if tr else None, "kind": tr["kind"] if tr else None,
        "tracked": bool(tr), "smart": wallet in tracker.smart_top,
        "pnl": sum(p["pnl"] or 0 for p in pos), "spent": sum(p["sol_in"] or 0 for p in pos),
        "positions": pos,
    }


@app.get("/api/tracked")
async def tracked():
    return await db.tracked()


@app.post("/api/tracked")
async def add_tracked(body: dict = Body(...)):
    w = check_key(str(body.get("wallet", "")))
    kind = body.get("kind") if body.get("kind") in ("trader", "dev") else "trader"
    label = str(body.get("label") or "").strip()[:40] or w[:4] + "…" + w[-4:]
    await db.add_tracked(w, label, kind)
    await tracker.reload_tracked()
    return {"ok": True}


@app.delete("/api/tracked/{wallet}")
async def del_tracked(wallet: str):
    await db.remove_tracked(check_key(wallet))
    await tracker.reload_tracked()
    return {"ok": True}


@app.get("/api/scam")
async def scam_list():
    return await db.scam_list()


@app.post("/api/scam")
async def mark_scam(body: dict = Body(...)):
    w = check_key(str(body.get("wallet", "")))
    note = str(body.get("note") or "").strip()[:200]
    await db.mark_scam(w, note)
    await tracker.reload_tracked()
    return {"ok": True}


@app.delete("/api/scam/{wallet}")
async def unmark_scam(wallet: str):
    await db.unmark_scam(check_key(wallet))
    await tracker.reload_tracked()
    return {"ok": True}


@app.get("/api/settings")
async def get_settings():
    return tracker.settings


@app.put("/api/settings")
async def put_settings(body: dict = Body(...)):
    return await tracker.save_settings(body)


@app.post("/api/telegram/test")
async def telegram_test():
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        raise HTTPException(400, "Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env")
    await tracker._telegram({"kind": "new_token", "data": {"token": {
        "name": "Тестовое сообщение", "symbol": "TEST", "mint": "—", "mcap_sol": 0}}})
    return {"ok": True}


@app.get("/api/stream")
async def stream():
    q = tracker.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            tracker.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def run() -> None:
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    run()
