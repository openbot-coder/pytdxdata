"""轻量技术指标（纯Python，零依赖）：MACD/KDJ/RSI/BOLL/MA/EMA"""
from __future__ import annotations

from typing import Callable


def _ema(values: list[float], period: int) -> list[float]:
    """EMA。"""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _sma(values: list[float], period: int) -> list[float]:
    """简单移动平均（尾部对齐，前period-1个为NaN用None）。"""
    out: list[float | None] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / period if i >= period - 1 else None)
    return out


def ma(values: list[float], period: int) -> list[float | None]:
    return _sma(values, period)


def ema(values: list[float], period: int) -> list[float]:
    return _ema(values, period)


def macd(values: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD: DIF/DEA/MACD柱。返回与values等长。"""
    ema_fast = _ema(values, fast)
    ema_slow = _ema(values, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = _ema(dif, signal)
    hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return {"DIF": dif, "DEA": dea, "MACD": hist}


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    """RSI。"""
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        chg = values[i] - values[i - 1]
        if chg > 0:
            gains += chg
        else:
            losses -= chg
    for i in range(period, len(values)):
        if i > period:
            chg = values[i] - values[i - 1]
            gains = (gains * (period - 1) + max(chg, 0)) / period
            losses = (losses * (period - 1) + max(-chg, 0)) / period
        out[i] = 100.0 if losses == 0 else 100.0 - 100.0 / (1 + gains / losses) if losses else 100.0
        if losses == 0 and gains == 0:
            out[i] = 50.0
    return out


def kdj(values_high: list[float], values_low: list[float],
        values_close: list[float], period: int = 9) -> dict:
    """KDJ（简化：9日随机指标）。"""
    n = len(values_close)
    k = [50.0] * n
    d = [50.0] * n
    j = [50.0] * n
    for i in range(n):
        lo = min(values_low[max(0, i - period + 1): i + 1])
        hi = max(values_high[max(0, i - period + 1): i + 1])
        rsv = 50.0 if hi == lo else (values_close[i] - lo) / (hi - lo) * 100
        k[i] = k[i - 1] * 2 / 3 + rsv / 3
        d[i] = d[i - 1] * 2 / 3 + k[i] / 3
        j[i] = 3 * k[i] - 2 * d[i]
    return {"K": k, "D": d, "J": j}


def boll(values: list[float], period: int = 20, mult: float = 2.0) -> dict:
    """BOLL布林带。"""
    import statistics
    n = len(values)
    mid = [None] * n
    upper = [None] * n
    lower = [None] * n
    for i in range(period - 1, n):
        window = values[i - period + 1: i + 1]
        m = sum(window) / period
        sd = statistics.pstdev(window)
        mid[i] = m
        upper[i] = m + mult * sd
        lower[i] = m - mult * sd
    return {"MID": mid, "UPPER": upper, "LOWER": lower}


_INDICATORS: dict[str, Callable] = {
    "MA": lambda c, **kw: {"MA": ma(c, kw.get("period", 5))},
    "EMA": lambda c, **kw: {"EMA": ema(c, kw.get("period", 12))},
    "MACD": macd,
    "RSI": lambda c, **kw: {"RSI": rsi(c, kw.get("period", 14))},
    "KDJ": lambda h, l, c, **kw: kdj(h, l, c, kw.get("period", 9)),
    "BOLL": lambda c, **kw: boll(c, kw.get("period", 20), kw.get("mult", 2.0)),
}


def compute_indicators(high: list[float], low: list[float], close: list[float],
                       names: list[str], params: dict | None = None,
                       tail: int | None = None) -> dict:
    """计算指标，返回 {指标名: 序列}。"""
    params = params or {}
    result: dict = {}
    for name in names:
        name_u = name.upper()
        fn = _INDICATORS.get(name_u)
        if fn is None:
            continue
        kw = params.get(name, {}) or {}
        try:
            if name_u == "KDJ":
                r = fn(high, low, close, **kw)
            else:
                r = fn(close, **kw)
        except Exception:
            continue
        for k, v in r.items():
            result[k] = v[-tail:] if tail else v
    return result
