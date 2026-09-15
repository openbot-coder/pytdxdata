"""MAC通道 K线命令（0x122E，支持复权；每条36B: ymd+time+7float）"""
from __future__ import annotations

import struct

from ..._binary import unpack_from
from ...models import Adjust, KlinePeriod, SecurityBar
from ..mac_frame import build_mac_request
from .base import BaseCommand

MAC_KLINE_PAGE = 700

# MAC Period 与标准 KlineCategory 同值（DAILY=4）
_MAC_PERIOD = {
    KlinePeriod.MIN_5: 0, KlinePeriod.MIN_15: 1, KlinePeriod.MIN_30: 2,
    KlinePeriod.MIN_60: 3, KlinePeriod.DAY: 4, KlinePeriod.WEEK: 5,
    KlinePeriod.MONTH: 6, KlinePeriod.MIN_1: 7, KlinePeriod.MIN_3: 8,
    KlinePeriod.YEAR: 11, KlinePeriod.SEASON: 10,
}


class MacKlineCmd(BaseCommand[list[SecurityBar]]):
    """MAC通道K线（0x122E）。"""

    def __init__(self, market: int, code: str, period: KlinePeriod, adjust: Adjust,
                 start: int, count: int = 700, times: int = 1) -> None:
        self.market = market
        self.code = code
        self.period = period
        self.adjust = adjust
        self.start = start
        self.count = count
        self.times = times

    def build_request(self) -> bytes:
        # 与 easy-tdx SymbolBarCmd 一致: <H22sHHIHHbbbbH4s
        body = struct.pack(
            "<H22sHHIHHbbbbH4s",
            self.market,
            self.code.encode("gbk"),
            _MAC_PERIOD.get(self.period, 4),
            self.times,
            self.start,
            self.count,
            int(self.adjust),
            1, 1, 0, 1, 0,
            b"",
        )
        return build_mac_request(0x122E, body)

    def parse_response(self, body: bytes) -> list[SecurityBar]:
        # 头部: market(2)+code(22)+category(2)+flag(1)+count(2)+start(4)=33B; count@offset24 <HBHI
        if len(body) < 33:
            return []
        _cat, _flag, count, _start = unpack_from("<HBHI", body, 24, "mac kline header")
        count = min(count, (len(body) - 33) // 36)
        if count < 0:
            count = 0
        is_intraday = self.period.is_minute
        bars: list[SecurityBar] = []
        for i in range(count):
            offset = 33 + i * 36
            if offset + 36 > len(body):
                break
            ymd, time_num, open_, high, low, close, amount, vol, _fs = unpack_from(
                "<II7f", body, offset, "mac kline bar")
            if ymd < 19900101 or ymd > 20991231:
                continue
            year, month, day = ymd // 10000, (ymd % 10000) // 100, ymd % 100
            hour = minute = 15
            if is_intraday and time_num:
                hour = time_num // 3600
                minute = (time_num % 3600) // 60
            bars.append(SecurityBar(
                market=self.market, code=self.code,
                open=open_, close=close, high=high, low=low,
                vol=vol, amount=amount,
                year=year, month=month, day=day, hour=hour, minute=minute,
            ))
        return bars
