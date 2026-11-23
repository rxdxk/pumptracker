"""Оценка девов и вспомогательные расчёты."""
from __future__ import annotations

from typing import Optional

from .pda import bonding_curve_address

# Капитализация в SOL: старт кривой ≈ 28, миграция ≈ 410
START_MCAP_SOL = 28.0
GRAD_MCAP_SOL = 410.0
# Токенов на продажу по кривой (real_token_reserves на старте)
CURVE_TOKENS = 793_100_000.0


def curve_progress(mcap_sol: float, completed: bool) -> float:
    if completed:
        return 1.0
    p = (mcap_sol - START_MCAP_SOL) / (GRAD_MCAP_SOL - START_MCAP_SOL)
    return max(0.0, min(1.0, p))


def dev_profile(raw: dict) -> dict:
    """raw — результат Database.dev_stats_raw. Возвращает метрики, скор 0–100 и метки."""
    launches = int(raw.get("launches") or 0)
    migrated = int(raw.get("migrated") or 0)
    dumped = int(raw.get("dumped") or 0)
    observed = int(raw.get("observed") or 0)
    avg_ath = float(raw.get("avg_ath") or 0)

    mig_rate = migrated / launches if launches else 0.0
    dump_rate: Optional[float] = dumped / observed if observed else None

    flags: list[str] = []
    score: Optional[int] = None
    tone = "neutral"
    if launches:
        progress = curve_progress(avg_ath, False)
        dump_part = (1 - dump_rate) if dump_rate is not None else 0.5
        serial = 1.0 if launches <= 10 else max(0.0, 1 - (launches - 10) / 40)
        s = 40 * min(1.0, mig_rate * 4) + 25 * progress + 25 * dump_part + 10 * serial
        score = int(round(s))
        tone = "good" if score >= 65 else "mid" if score >= 40 else "bad"
        if migrated:
            flags.append(f"{migrated} миграц.")
        if launches >= 20:
            flags.append("серийный лончер")
        if dump_rate is not None and dump_rate >= 0.5 and dumped >= 2:
            flags.append("часто сливает")
    else:
        flags.append("новый дев")

    return {
        "launches": launches,
        "migrated": migrated,
        "dumped": dumped,
        "observed": observed,
        "migration_rate": mig_rate,
        "dump_rate": dump_rate,
        "avg_ath_sol": avg_ath,
        "best_ath_sol": float(raw.get("best_ath") or 0),
        "first_launch": raw.get("first_launch"),
        "last_launch": raw.get("last_launch"),
        "backfilled_at": raw.get("backfilled_at"),
        "score": score,
        "tone": tone,
        "flags": flags,
    }


def token_view(t: dict) -> dict:
    keys = ["mint", "name", "symbol", "creator", "created_at", "dev_buy_sol", "mcap_sol",
            "ath_mcap_sol", "volume_sol", "buys", "sells", "completed", "completed_at",
            "dev_dumped", "dev_dumped_at", "last_trade_at", "twitter", "telegram", "website",
            "image", "source", "dev_tok_in", "dev_tok_out"]
    v = {k: t.get(k) for k in keys}
    # адрес пары для Axiom: bonding curve (известен или вычисляется из mint)
    v["bonding_curve"] = t.get("bonding_curve")
    if not v["bonding_curve"] and t.get("mint"):
        try:
            v["bonding_curve"] = bonding_curve_address(t["mint"])
        except ValueError:
            v["bonding_curve"] = None
    left = t.get("curve_left")
    if t.get("completed"):
        v["progress"] = 1.0
    elif left is not None:
        v["progress"] = max(0.0, min(1.0, 1 - left / CURVE_TOKENS))
    elif t.get("mcap_sol"):
        v["progress"] = curve_progress(t["mcap_sol"], False)
    else:
        v["progress"] = None  # кривая не в SOL — прогресс неизвестен
    tin = t.get("dev_tok_in") or 0
    v["dev_holding_pct"] = max(0.0, (tin - (t.get("dev_tok_out") or 0))) / 1e9 * 100 if tin else 0.0
    return v
