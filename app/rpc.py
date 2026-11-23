"""HTTP-клиент Solana RPC, подтягивание истории дева и метаданных токенов."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import struct
from typing import Any, Optional

import aiohttp
import base58

from . import config
from .analytics import GRAD_MCAP_SOL
from .parser import PUMP_PROGRAM, CreateEvent, decode_bonding_curve, events_from_logs

log = logging.getLogger("rpc")


class RpcError(Exception):
    pass


class RpcClient:
    def __init__(self, url: str, concurrency: int = 4):
        self.url = url
        self.sem = asyncio.Semaphore(concurrency)
        self.session: Optional[aiohttp.ClientSession] = None
        self._id = 0

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def close(self) -> None:
        if self.session:
            await self.session.close()

    async def call(self, method: str, params: list, retries: int = 4) -> Any:
        delay = 0.5
        for attempt in range(retries + 1):
            async with self.sem:
                self._id += 1
                try:
                    async with self.session.post(
                        self.url, json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
                    ) as r:
                        if r.status == 429:
                            raise RpcError("rate limited")
                        data = await r.json(content_type=None)
                except (aiohttp.ClientError, asyncio.TimeoutError, RpcError) as e:
                    if attempt == retries:
                        raise RpcError(f"{method}: {e}") from e
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
            if "error" in data:
                raise RpcError(f"{method}: {data['error']}")
            return data.get("result")
        raise RpcError(method)

    async def signatures(self, address: str, limit: int) -> list[dict]:
        return await self.call("getSignaturesForAddress", [address, {"limit": limit}]) or []

    async def transaction_logs(self, sig: str) -> Optional[list[str]]:
        tx = await self.call(
            "getTransaction",
            [sig, {"encoding": "json", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}],
        )
        if not tx or not tx.get("meta") or tx["meta"].get("err"):
            return None
        return tx["meta"].get("logMessages") or []

    async def accounts(self, keys: list[str]) -> list[Optional[bytes]]:
        out: list[Optional[bytes]] = []
        for i in range(0, len(keys), 100):
            chunk = keys[i:i + 100]
            res = await self.call("getMultipleAccounts", [chunk, {"encoding": "base64"}])
            for v in (res or {}).get("value", []):
                out.append(base64.b64decode(v["data"][0]) if v else None)
        return out


def _ix_disc(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


CREATE_IX = {_ix_disc("create"), _ix_disc("create_v2")}
BEFORE_RE = re.compile(r"before-signature` parameter set to (\w+)")


def _borsh_strings(b: bytes, o: int, n: int) -> list[str]:
    out = []
    for _ in range(n):
        (ln,) = struct.unpack_from("<I", b, o)
        o += 4
        out.append(b[o:o + ln].decode("utf-8", errors="replace"))
        o += ln
    return out


async def helius_creates(session: aiohttp.ClientSession, api_key: str, dev: str,
                         max_pages: int) -> list[dict]:
    """Все создания токенов pump.fun девом через Enhanced API Helius (фильтр type=CREATE
    на стороне сервера — не нужно тянуть тысячи сделок дева)."""
    base = (f"https://api-mainnet.helius-rpc.com/v0/addresses/{dev}/transactions"
            f"?api-key={api_key}&type=CREATE&limit=100")
    found: dict[str, dict] = {}
    before: Optional[str] = None
    for _ in range(max_pages):
        url = base + (f"&before={before}" if before else "")
        for attempt in range(4):
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status == 429:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                body = await r.json(content_type=None)
                status = r.status
            break
        else:
            raise RpcError("Helius: rate limited")
        if status != 200 or isinstance(body, dict):
            # «В окне поиска событий нет» — продолжаем с подсказанной сигнатуры
            m = BEFORE_RE.search(json.dumps(body))
            if m:
                before = m.group(1)
                continue
            if status == 404:
                break
            raise RpcError(f"Helius {status}: {str(body)[:200]}")
        if not body:
            break
        for tx in body:
            if tx.get("transactionError"):
                continue
            for ix in tx.get("instructions", []):
                if ix.get("programId") != PUMP_PROGRAM or len(ix.get("accounts", [])) < 3:
                    continue
                try:
                    raw = base58.b58decode(ix.get("data", ""))
                except ValueError:
                    continue
                if raw[:8] not in CREATE_IX:
                    continue
                try:
                    name, symbol, uri = _borsh_strings(raw, 8, 3)
                except (struct.error, IndexError):
                    name = symbol = uri = None
                mint = ix["accounts"][0]
                found[mint] = {"mint": mint, "bonding_curve": ix["accounts"][2], "name": name,
                               "symbol": symbol, "uri": uri, "created_at": tx.get("timestamp")}
        before = body[-1]["signature"]
    return list(found.values())


async def backfill_dev(rpc: RpcClient, dev: str, limit: int,
                       http: Optional[aiohttp.ClientSession] = None, pages: int = 0) -> list[dict]:
    """Токены, созданные девом, с текущим состоянием кривой.

    С ключом Helius — полная история через Enhanced API (pages страниц),
    иначе — поиск по последним `limit` транзакциям через обычный RPC."""
    if config.HELIUS_API_KEY and http is not None and pages > 0:
        items = await helius_creates(http, config.HELIUS_API_KEY, dev, pages)
        return await _with_curve_state(rpc, dev, items)
    return await _backfill_by_signatures(rpc, dev, limit)


async def _with_curve_state(rpc: RpcClient, dev: str, items: list[dict]) -> list[dict]:
    if not items:
        return []
    curves = await rpc.accounts([e["bonding_curve"] for e in items])
    out = []
    for e, raw in zip(items, curves):
        st = decode_bonding_curve(raw) if raw else None
        done = bool(st and st.complete)
        mc = st.mcap_sol if st else 0.0
        out.append({**e, "creator": dev, "mcap_sol": mc, "completed": 1 if done else 0,
                    "ath_mcap_sol": max(mc, GRAD_MCAP_SOL if done else 0.0)})
    return out


async def _backfill_by_signatures(rpc: RpcClient, dev: str, limit: int) -> list[dict]:
    sigs = [s for s in await rpc.signatures(dev, limit) if not s.get("err")]

    async def one(s: dict) -> list[CreateEvent]:
        try:
            logs = await rpc.transaction_logs(s["signature"])
        except RpcError as e:
            log.debug("tx %s: %s", s["signature"], e)
            return []
        if not logs:
            return []
        return [e for e in events_from_logs(logs)
                if isinstance(e, CreateEvent) and dev in (e.creator, e.user)]

    creates: dict[str, CreateEvent] = {}
    for evs in await asyncio.gather(*(one(s) for s in sigs)):
        for e in evs:
            creates[e.mint] = e
    if not creates:
        return []

    items = [{"mint": e.mint, "name": e.name, "symbol": e.symbol, "uri": e.uri,
              "bonding_curve": e.bonding_curve, "created_at": e.timestamp}
             for e in creates.values()]
    return await _with_curve_state(rpc, dev, items)


async def fetch_metadata(session: aiohttp.ClientSession, uri: str) -> dict:
    """Соцсети токена из JSON-метаданных (обычно IPFS)."""
    if not uri or not uri.startswith("http"):
        return {}
    try:
        async with session.get(uri, timeout=aiohttp.ClientTimeout(total=6)) as r:
            if r.status != 200:
                return {}
            data = await r.json(content_type=None)
    except Exception:  # noqa: BLE001 — метаданные бывают любыми
        return {}
    if not isinstance(data, dict):
        return {}
    ext = data.get("extensions") if isinstance(data.get("extensions"), dict) else {}

    def pick(*keys: str) -> Optional[str]:
        for k in keys:
            v = data.get(k) or ext.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:300]
        return None

    return {
        "twitter": pick("twitter", "x"),
        "telegram": pick("telegram"),
        "website": pick("website"),
        "image": pick("image"),
    }


async def fetch_sol_usd(session: aiohttp.ClientSession) -> Optional[float]:
    try:
        async with session.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            data = await r.json(content_type=None)
            return float(data["solana"]["usd"])
    except Exception:  # noqa: BLE001
        return None
