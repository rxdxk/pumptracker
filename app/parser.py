"""Декодирование событий pump.fun из логов транзакций.

Anchor пишет события в логи строкой `Program data: <base64>`, первые 8 байт —
дискриминатор sha256("event:<Name>")[:8]. Раскладка полей проверена на mainnet.
"""
from __future__ import annotations

import base64
import hashlib
import struct
from dataclasses import dataclass
from typing import Optional, Union

import base58

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
TOTAL_SUPPLY = 1_000_000_000  # стандартный supply токена pump.fun (6 знаков)
LAMPORTS = 1_000_000_000
TOKEN_UNIT = 1_000_000


def _disc(name: str) -> bytes:
    return hashlib.sha256(f"event:{name}".encode()).digest()[:8]


DISC_CREATE = _disc("CreateEvent")
DISC_TRADE = _disc("TradeEvent")
DISC_COMPLETE = _disc("CompleteEvent")


@dataclass
class CreateEvent:
    name: str
    symbol: str
    uri: str
    mint: str
    bonding_curve: str
    user: str
    creator: str
    timestamp: int


@dataclass
class TradeEvent:
    mint: str
    sol_amount: int  # лампорты
    token_amount: int  # сырые единицы (6 знаков)
    is_buy: bool
    user: str
    timestamp: int
    virtual_sol_reserves: int
    virtual_token_reserves: int
    real_sol_reserves: int
    real_token_reserves: int
    creator: Optional[str] = None

    @property
    def sol(self) -> float:
        return self.sol_amount / LAMPORTS

    @property
    def tokens(self) -> float:
        return self.token_amount / TOKEN_UNIT

    @property
    def mcap_sol(self) -> float:
        return mcap_from_reserves(self.virtual_sol_reserves, self.virtual_token_reserves)

    @property
    def standard_curve(self) -> bool:
        """Стандартная SOL-кривая: x*y = 30 SOL * 1.073B токенов (±2%)."""
        k = self.virtual_sol_reserves * self.virtual_token_reserves
        return abs(k / STANDARD_K - 1) < 0.02 if k else False


# Инвариант стандартной кривой: 30 SOL (лампорты) * 1 073 000 000 токенов (6 знаков)
STANDARD_K = 30 * LAMPORTS * 1_073_000_000 * TOKEN_UNIT


@dataclass
class CompleteEvent:
    user: str
    mint: str
    bonding_curve: str
    timestamp: int


Event = Union[CreateEvent, TradeEvent, CompleteEvent]


def mcap_from_reserves(vsol: int, vtok: int) -> float:
    if vtok <= 0:
        return 0.0
    price = (vsol / LAMPORTS) / (vtok / TOKEN_UNIT)
    return price * TOTAL_SUPPLY


class _Reader:
    def __init__(self, data: bytes, offset: int = 8):
        self.b = data
        self.o = offset

    def left(self) -> int:
        return len(self.b) - self.o

    def pubkey(self) -> str:
        r = base58.b58encode(self.b[self.o:self.o + 32]).decode()
        self.o += 32
        return r

    def string(self) -> str:
        (n,) = struct.unpack_from("<I", self.b, self.o)
        self.o += 4
        r = self.b[self.o:self.o + n].decode("utf-8", errors="replace")
        self.o += n
        return r

    def u64(self) -> int:
        (v,) = struct.unpack_from("<Q", self.b, self.o)
        self.o += 8
        return v

    def i64(self) -> int:
        (v,) = struct.unpack_from("<q", self.b, self.o)
        self.o += 8
        return v

    def boolean(self) -> bool:
        v = self.b[self.o] != 0
        self.o += 1
        return v

    def skip(self, n: int) -> None:
        self.o += n


def decode_event(data: bytes) -> Optional[Event]:
    if len(data) < 8:
        return None
    d = data[:8]
    try:
        if d == DISC_TRADE:
            r = _Reader(data)
            ev = TradeEvent(
                mint=r.pubkey(),
                sol_amount=r.u64(),
                token_amount=r.u64(),
                is_buy=r.boolean(),
                user=r.pubkey(),
                timestamp=r.i64(),
                virtual_sol_reserves=r.u64(),
                virtual_token_reserves=r.u64(),
                real_sol_reserves=r.u64(),
                real_token_reserves=r.u64(),
            )
            # Новые версии: fee_recipient, fee_basis_points, fee, creator, ...
            if r.left() >= 32 + 8 + 8 + 32:
                r.skip(32 + 8 + 8)
                ev.creator = r.pubkey()
            return ev
        if d == DISC_CREATE:
            r = _Reader(data)
            name, symbol, uri = r.string(), r.string(), r.string()
            mint, curve, user = r.pubkey(), r.pubkey(), r.pubkey()
            creator = user
            ts = 0
            if r.left() >= 32 + 8:
                creator = r.pubkey()
                ts = r.i64()
            return CreateEvent(name, symbol, uri, mint, curve, user, creator, ts)
        if d == DISC_COMPLETE:
            r = _Reader(data)
            return CompleteEvent(r.pubkey(), r.pubkey(), r.pubkey(), r.i64())
    except (struct.error, IndexError, ValueError):
        return None
    return None


def events_from_logs(logs: list[str]) -> list[Event]:
    """Достаёт события pump.fun, сохраняя порядок внутри транзакции.

    Учитываются только `Program data`, выпущенные самой программой pump.fun:
    в одной транзакции бывают другие лаунчпады с одноимёнными событиями
    (тот же дискриминатор, другая раскладка полей).
    """
    out: list[Event] = []
    stack: list[str] = []
    for line in logs:
        if line.startswith("Program ") and " invoke [" in line:
            stack.append(line.split(" ", 2)[1])
            continue
        if line.startswith("Program ") and (line.endswith(" success") or " failed" in line):
            if stack:
                stack.pop()
            continue
        if not line.startswith("Program data: "):
            continue
        if not stack or stack[-1] != PUMP_PROGRAM:
            continue
        try:
            raw = base64.b64decode(line[14:])
        except (ValueError, base64.binascii.Error):
            continue
        ev = decode_event(raw)
        if ev is not None:
            out.append(ev)
    return out


@dataclass
class BondingCurveState:
    virtual_token_reserves: int
    virtual_sol_reserves: int
    real_token_reserves: int
    real_sol_reserves: int
    token_total_supply: int
    complete: bool

    @property
    def mcap_sol(self) -> float:
        k = self.virtual_sol_reserves * self.virtual_token_reserves
        if not k or abs(k / STANDARD_K - 1) >= 0.02:
            return 0.0  # нестандартная кривая — MC в SOL не считаем
        return mcap_from_reserves(self.virtual_sol_reserves, self.virtual_token_reserves)


def decode_bonding_curve(data: bytes) -> Optional[BondingCurveState]:
    if len(data) < 8 + 40 + 1:
        return None
    r = _Reader(data)
    return BondingCurveState(r.u64(), r.u64(), r.u64(), r.u64(), r.u64(), r.boolean())
