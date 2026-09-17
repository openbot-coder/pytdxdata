"""缓存键构造：{category}|{market}|{code}|{period}|{start}|{count}|{extra}"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True, slots=True)
class CacheKey:
    category: str                   # kline|quote|transaction|list|minute|xdxr|finance
    market: int | None = None
    code: str | None = None
    period: str | None = None
    start: int | None = None
    count: int | None = None
    extra: str = ""                 # adjust/date/股票集哈希 等规范化参数

    def to_str(self) -> str:
        return "|".join([
            self.category,
            "" if self.market is None else str(self.market),
            self.code or "",
            self.period or "",
            "" if self.start is None else str(self.start),
            "" if self.count is None else str(self.count),
            self.extra,
        ])

    @classmethod
    def from_str(cls, s: str) -> "CacheKey":
        parts = s.split("|")
        while len(parts) < 7:
            parts.append("")
        def _i(v: str) -> int | None:
            return int(v) if v else None
        return cls(
            category=parts[0], market=_i(parts[1]), code=parts[2] or None,
            period=parts[3] or None, start=_i(parts[4]), count=_i(parts[5]),
            extra=parts[6],
        )

    @classmethod
    def quote_key(cls, stocks: Sequence[tuple[int, str]],
                  tag: str = "") -> "CacheKey":
        """报价键：对股票集规范化排序，顺序无关可命中。

        ``tag`` 区分取数口径（标准五档 / MAC·EX 字段位），不同口径不互相命中。
        """
        norm = sorted(f"{m}:{c}" for m, c in stocks)
        extra = ",".join(norm)
        return cls(category="quote", extra=f"{tag}#{extra}" if tag else extra)
