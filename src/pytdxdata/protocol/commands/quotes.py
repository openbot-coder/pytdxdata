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
    """推断报价的小数位：**协议原始整数除以 ``10**该值``** 才是真实价格。

    四档：股票/指数/板块 ``2``、基金/ETF/B股 ``3``、债券/回购 ``4``。

    该函数只是**离线推断**（``get_quotes`` 解析时拿不到证券清单）。真正的权威来源
    是证券列表里协议自带的 ``SecurityInfo.decimal_point`` 字段——2026-10-22 拉取
    全量 51,982 只（沪 27,520 + 深 24,462）逐条比对，本函数与之一一吻合、**零例外**。
    能对上是因为「三位前缀 → 小数位」在全量清单里没有任何一个前缀出现混合小数位。

    历史修正（两次都是同一类错误：把某段前缀想当然地归错档）：

    - **2026-10-21**：沪市 ``000``/``8`` 被判成 3 位 → 上证指数/上证50/科创50/
      板块指数**价格少 10 倍**（3713 读成 371）。它们实际是 2 位。
    - **2026-10-22**：债券与回购全线偏差 —— 沪市 ``010/018/019/020/1xx/2xx``
      与深市 ``10x/11x/12x/13x/14x/19x/50x/52x/56x`` 实际是 **4 位**，原先
      沪市落到 2 位（**少 100 倍**）、深市落到 3 位（**少 10 倍**）。实测例：
      ``sh019742`` 110.228 读成 11022.8、``sh204001`` 1.385 读成 138.5、
      ``sz131810`` 1.38 读成 13.8。同时沪市 B 股 ``900xxx`` 是 3 位（原先 2 位）。

    实证方法：拿 ``pre_close``（快照口径）除以上一交易日**日线收盘**（K线口径，
    走 ``/1000`` 独立路径），比值即标度误差 —— 1.0 正确、10 少 10 倍、100 少 100 倍。
    """
    code = (code or "").strip().rstrip("\x00")
    if market == 1:  # 沪市
        if code.startswith("5"):
            return 3                     # 基金/ETF/LOF（50/51/52/56/58 系）
        if code.startswith("900"):
            return 3                     # B股
        if code.startswith(("000", "36", "88", "99", "7", "6")):
            return 2                     # 指数/板块/优先股/配售/股票(60/601/603/605/688)
        return 4                         # 债券(01x/1xx/2xx)、回购(204xxx)、ABS
    if market == 0:  # 深市
        if code.startswith(("080", "158", "159", "160", "161", "162", "163",
                            "164", "165", "166", "167", "168", "169",
                            "180", "181")):
            return 3                     # 基金/ETF/LOF/配债
        if code.startswith(("000", "001", "002", "003", "070", "140",
                            "200", "201", "300", "301", "302", "395", "399")):
            return 2                     # 股票/指数/B股/优先股
        return 4                         # 债券(10x-14x/19x)、回购(131xxx)、ABS(50x/52x/56x)
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
