"""五档报价命令（标准通道 0x010C/method 0x05053E，最多80只/次）"""
from __future__ import annotations

import struct

from ..._binary import unpack_from
from ...models import SecurityQuote
from ..price import get_price
from ..volume import get_volume
from .base import BaseCommand

MAX_QUOTES = 80


def price_decimal_digits(market: int, code: str) -> int:
    """推断报价有效小数位：股票2位(/100)、指数/ETF/基金/转债/国债3位(/1000)。"""
    code = (code or "").strip().rstrip("\x00")
    if market == 1:  # SH
        if code.startswith(("5", "000", "8")):
            return 3
        return 2
    if market == 0:  # SZ
        if code.startswith("1"):
            return 3
        return 2
    return 2


def format_server_time(raw: int) -> str:
    """服务器时间编码：小时 + 百万分之一小时。14999212 -> 14:59:57.163"""
    hours, fractional_hour = divmod(raw, 1_000_000)
    total_millis = fractional_hour * 3600 // 1000
    minutes, remainder = divmod(total_millis, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


class GetSecurityQuotesCmd(BaseCommand[list[SecurityQuote]]):
    """批量获取实时五档行情（最多80只/次）。"""

    def __init__(self, stocks: list[tuple[int, str]]) -> None:
        if not stocks:
            raise ValueError("stocks 不能为空")
        if len(stocks) > MAX_QUOTES:
            raise ValueError(f"单次最多查询{MAX_QUOTES}只")
        self.stocks = stocks

    def build_request(self) -> bytes:
        n = len(self.stocks)
        payload_len = n * 7 + 12
        header = struct.pack(
            "<HIHHIIHH", 0x010C, 0x02006320, payload_len, payload_len, 0x0005053E, 0, 0, n
        )
        body = bytearray(header)
        for market, code in self.stocks:
            body.extend(struct.pack("<B6s", int(market), code.encode("utf-8")))
        return bytes(body)

    def parse_response(self, body: bytes) -> list[SecurityQuote]:
        pos = 2  # 跳过2字节魔数
        (num,) = unpack_from("<H", body, pos, "quotes header")
        pos += 2
        results: list[SecurityQuote] = []

        for _ in range(num):
            market_b, code_b, _active1 = unpack_from("<B6sH", body, pos, "quote record header")
            pos += 9

            price_raw, pos = get_price(body, pos)
            last_close_diff, pos = get_price(body, pos)
            open_diff, pos = get_price(body, pos)
            high_diff, pos = get_price(body, pos)
            low_diff, pos = get_price(body, pos)
            unknown_0, pos = get_price(body, pos)   # server_time原始
            _unknown_1, pos = get_price(body, pos)
            vol, pos = get_price(body, pos)
            cur_vol, pos = get_price(body, pos)
            amount, _pos2 = get_volume(body, pos)
            pos = _pos2  # get_volume 已推进4字节
            s_vol, pos = get_price(body, pos)
            b_vol, pos = get_price(body, pos)
            _unknown_2, pos = get_price(body, pos)
            _unknown_3, pos = get_price(body, pos)

            bid: list[tuple[float, float]] = []
            ask: list[tuple[float, float]] = []
            for _d in range(5):
                bid_d, pos = get_price(body, pos)
                ask_d, pos = get_price(body, pos)
                bv, pos = get_price(body, pos)
                av, pos = get_price(body, pos)
                bid.append((bid_d, float(bv)))
                ask.append((ask_d, float(av)))

            (trading_status,) = unpack_from("<H", body, pos, "quote tail")
            pos += 2
            for _d in range(4):
                _, pos = get_price(body, pos)
            _rise_speed, _active2 = unpack_from("<hH", body, pos, "quote tail2")
            pos += 4

            code = code_b.decode("utf-8").rstrip("\x00")
            divisor = 10 ** price_decimal_digits(market_b, code)
            p = price_raw / divisor
            results.append(SecurityQuote(
                market=market_b, code=code, price=p,
                cur_vol=float(cur_vol),
                rise_speed=_rise_speed / 100.0,
                decimal_point=price_decimal_digits(market_b, code),
                open_amount=float(_unknown_3),
                pre_close=(price_raw + last_close_diff) / divisor,
                open=(price_raw + open_diff) / divisor,
                high=(price_raw + high_diff) / divisor,
                low=(price_raw + low_diff) / divisor,
                vol=float(vol), amount=amount, s_vol=float(s_vol), b_vol=float(b_vol),
                bid=[((price_raw + d) / divisor, v) for d, v in bid],
                ask=[((price_raw + d) / divisor, v) for d, v in ask],
                server_time=format_server_time(unknown_0),
                trading_status=trading_status,
            ))
        return results
        return results
