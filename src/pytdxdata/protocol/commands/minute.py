"""分时命令（标准通道 0x013000；0x1b08 当日分时在当前服务器格式已变，统一用历史命令传当天）"""
from __future__ import annotations

import struct

from ..._binary import unpack_from
from ...models import MinuteBar
from ..price import get_price
from .base import BaseCommand


class GetHistoryMinuteTimeDataCmd(BaseCommand[list[MinuteBar]]):
    """某日分时（date=YYYYMMDD；当日分时也用它，date 传当天）。"""

    def __init__(self, market: int, code: str, date: int) -> None:
        self.market = market
        self.code = code.encode("utf-8")
        self.date = date

    def build_request(self) -> bytes:
        header = bytes.fromhex("0c01300001010d000d00b40f".replace(" ", ""))
        return header + struct.pack("<IB6s", self.date, self.market, self.code)

    def parse_response(self, body: bytes) -> list[MinuteBar]:
        return _parse_minute_body(body, skip=6)


def _parse_minute_body(body: bytes, skip: int) -> list[MinuteBar]:
    (num,) = unpack_from("<H", body, 0, "minute header")
    pos = skip
    last_price = 0
    bars: list[MinuteBar] = []
    for idx in range(num):
        price_diff, pos = get_price(body, pos)
        _unknown, pos = get_price(body, pos)  # 协议第2字段(unknown, easy-tdx也丢弃)
        vol, pos = get_price(body, pos)
        last_price += price_diff
        # 合成时间: 前120条=09:30~11:29, 后120条=13:00~14:59 (与easy-tdx一致)
        if idx < 120:
            hour, minute = 9 + (30 + idx) // 60, (30 + idx) % 60
        else:
            hour, minute = 13 + (idx - 120) // 60, (idx - 120) % 60
        bars.append(MinuteBar(price=last_price / 100.0, vol=float(vol),
                              hour=hour, minute=minute))
    return bars
