"""MAC通道 分时命令（0x122D 单日分时图）。

对应标准通道 0x0130 历史分钟分时，但走MAC连接池。
请求/响应格式参照 easy-tdx SymbolTickChartCmd。
"""
from __future__ import annotations

import struct
from datetime import date, time as dtime

from ..._binary import unpack_from
from ...models import MinuteBar
from ..mac_frame import build_mac_request
from .base import BaseCommand

_MSG_ID_TICK_CHART = 0x122D


class MacTickChartCmd(BaseCommand[list[MinuteBar]]):
    """MAC 单日分时图（0x122D）。

    Args:
        market:    市场代码（0=深市, 1=沪市）。
        code:      6 位股票代码。
        date:      查询日期 YYYYMMDD（0 表示今天）。
    """

    def __init__(self, market: int, code: str, date: int = 0) -> None:
        self.market = market
        self.code = code
        self.date = date

    def build_request(self) -> bytes:
        body = struct.pack(
            "<H22sI5H",
            self.market,
            self.code.encode("gbk"),
            self.date,
            1,  # flag
            0, 0, 0, 0,
        )
        return build_mac_request(_MSG_ID_TICK_CHART, body)

    def parse_response(self, body: bytes) -> list[MinuteBar]:
        # 头部: market(2) + code(22) + query_date(4) + reserved(1) + ref_price(4) + count(2) = 35
        if len(body) < 35:
            return []
        (_market, _code_raw, _query_date, _reserved, _ref_price, count) = unpack_from(
            "<H22sIBfH", body, 0, "tick_chart header"
        )
        # 限制最大条数，防止异常
        count = min(count, 240, (len(body) - 35) // 18)
        if count <= 0:
            return []

        bars: list[MinuteBar] = []
        for i in range(count):
            offset = 35 + i * 18
            if offset + 18 > len(body):
                break
            (minutes, price, avg, vol, _momentum) = unpack_from(
                "<HffIf", body, offset, f"tick_chart tick[{i}]"
            )
            bars.append(MinuteBar(
                price=price,
                vol=float(vol),
                avg_price=avg,
                hour=minutes // 60 % 24,
                minute=minutes % 60,
            ))
        return bars
