"""通道选路：命令类型 → 标准/MAC"""
from __future__ import annotations

from typing import Literal

from .models import Adjust, KlinePeriod

Route = Literal["standard", "mac"]


def route_for(cmd_type: str, *, adjust: Adjust = Adjust.NONE,
              period: KlinePeriod | None = None) -> Route:
    """返回命令应走的通道。"""
    if cmd_type == "kline":
        if period == KlinePeriod.MIN_1:
            return "mac"      # MIN_1 统一走 MAC (0x122E)，支持深度分页无缺失
        return "mac" if adjust != Adjust.NONE else "standard"
    if cmd_type == "transaction":
        return "mac"          # MAC 0x122F 含 trade_count 且支持历史日期
    return "standard"         # quote/list/count/minute/xdxr/finance 均走标准
