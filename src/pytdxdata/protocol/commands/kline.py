"""K线命令（标准通道 0x010C，含差分还原解析；指数K线多4字节跳过）"""
from __future__ import annotations

import struct
from typing import Any

from ..._binary import unpack_from
from ...models import IndexBar, KlinePeriod, SecurityBar
from ..datetime_ import get_datetime
from ..price import get_price
from ..volume import get_volume
from .base import BaseCommand, TdxDecodeError

PAGE_SIZE = 800


class GetSecurityBarsCmd(BaseCommand[list[SecurityBar]]):
    """获取指定股票K线（分页，最多800/次）。"""

    def __init__(self, market: int, code: str, category: KlinePeriod,
                 start: int, count: int = 800) -> None:
        self.market = market
        self.code = code.encode("utf-8")
        self.category = int(category)
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        return struct.pack(
            "<HIHHHH6sHHHHIIH",
            0x010C, 0x01016408, 0x001C, 0x001C, 0x052D,
            self.market, self.code, self.category, 1, self.start, self.count, 0, 0, 0,
        )

    def parse_response(self, body: bytes) -> list[SecurityBar]:
        (ret_count,) = unpack_from("<H", body, 0, "kline header")
        pos = 2
        bars: list[SecurityBar] = []
        pre_diff_base = 0
        cat = self.category

        for i in range(ret_count):
            try:
                year, month, day, hour, minute, pos = get_datetime(cat, body, pos)
                open_diff, pos = get_price(body, pos)
                close_diff, pos = get_price(body, pos)
                high_diff, pos = get_price(body, pos)
                low_diff, pos = get_price(body, pos)
                vol, pos = get_volume(body, pos)
                amount, pos = get_volume(body, pos)
            except (ValueError, IndexError) as e:
                # 服务器截断/ret_count撒谎：返回已解析部分
                if i == 0 and not bars:
                    raise TdxDecodeError(
                        f"K线响应为空（声称{ret_count}条但首条解析失败:{e}）"
                    ) from e
                break

            open_abs = open_diff + pre_diff_base
            close_abs = open_abs + close_diff
            high_abs = open_abs + high_diff
            low_abs = open_abs + low_diff
            pre_diff_base = open_abs + close_diff

            bars.append(SecurityBar(
                market=self.market, code=self.code.decode("utf-8"),
                open=open_abs / 1000.0, close=close_abs / 1000.0,
                high=high_abs / 1000.0, low=low_abs / 1000.0,
                vol=vol, amount=amount,
                year=year, month=month, day=day, hour=hour, minute=minute,
            ))
        return bars


class GetIndexBarsCmd(GetSecurityBarsCmd):
    """指数K线：响应每条 vol+amount 后多4字节（涨/跌家数），需跳过。"""

    def parse_response(self, body: bytes) -> list[IndexBar]:
        (ret_count,) = unpack_from("<H", body, 0, "index kline header")
        pos = 2
        bars: list[IndexBar] = []
        pre_diff_base = 0
        cat = self.category

        for i in range(ret_count):
            try:
                year, month, day, hour, minute, pos = get_datetime(cat, body, pos)
                open_diff, pos = get_price(body, pos)
                close_diff, pos = get_price(body, pos)
                high_diff, pos = get_price(body, pos)
                low_diff, pos = get_price(body, pos)
                vol, pos = get_volume(body, pos)
                amount, pos = get_volume(body, pos)
                pos += 4  # 指数额外：上涨家数+下跌家数 uint16×2
            except (ValueError, IndexError) as e:
                if i == 0 and not bars:
                    raise TdxDecodeError(
                        f"指数K线响应为空（声称{ret_count}条但首条解析失败:{e}）"
                    ) from e
                break

            open_abs = open_diff + pre_diff_base
            close_abs = open_abs + close_diff
            high_abs = open_abs + high_diff
            low_abs = open_abs + low_diff
            pre_diff_base = open_abs + close_diff

            bars.append(IndexBar(
                market=self.market, code=self.code.decode("utf-8"),
                open=open_abs / 1000.0, close=close_abs / 1000.0,
                high=high_abs / 1000.0, low=low_abs / 1000.0,
                vol=vol, amount=amount,
                year=year, month=month, day=day, hour=hour, minute=minute,
            ))
        return bars
