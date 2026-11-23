"""Program Derived Address без внешних зависимостей (адрес bonding curve из mint)."""
from __future__ import annotations

import hashlib
from functools import lru_cache

import base58

from .parser import PUMP_PROGRAM

_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _on_curve(b: bytes) -> bool:
    """Лежит ли 32-байтовая точка на кривой ed25519 (PDA обязаны быть вне её)."""
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    x2 = (y2 - 1) * pow(_D * y2 + 1, _P - 2, _P) % _P
    if x2 == 0:
        return True
    return pow(x2, (_P - 1) // 2, _P) == 1  # критерий Эйлера: есть ли квадратный корень


def find_program_address(seeds: list[bytes], program_id: str) -> str:
    pid = base58.b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return base58.b58encode(h).decode()
    raise ValueError("PDA не найден")


@lru_cache(maxsize=50_000)
def bonding_curve_address(mint: str) -> str:
    return find_program_address([b"bonding-curve", base58.b58decode(mint)], PUMP_PROGRAM)
