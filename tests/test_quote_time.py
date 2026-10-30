"""报价快照时间字段（0x053E 第 6 个变长字段）解码口径。

2026-10-30 修正前按「小时 + 百万分之一小时」解码，股票 11:29:59 会解成
11:17:57、ETF 会解出 `113:00:21` 这种非法小时。这里把实测口径锁死：
十进制位拼接 `HH MM SS` + 价格小数位，SS 的 50~59 段服务端写成 90~99。
"""
import struct

from pytdxdata.protocol.commands.quotes import (
    GetSecurityQuotesCmd,
    format_server_time,
    price_decimal_digits,
)
from pytdxdata.protocol.price import put_price

# ---------- 纯解码 ----------

def test_stock_two_digits():
    # 13:22:40.02（股票价格 2 位小数 → 时间带 2 位小数）
    assert format_server_time(13224002, 2) == "13:22:40.02"


def test_etf_three_digits():
    # 13:22:45.903（ETF 3 位小数）
    assert format_server_time(132245903, 3) == "13:22:45.903"


def test_bond_four_digits():
    # 13:10:22.8050（国债 / 回购 4 位小数）
    assert format_server_time(1310228050, 4) == "13:10:22.8050"


def test_seconds_50_59_written_as_90_99():
    # 服务端把 52 秒写成 92 → 必须还原成 13:17:52.01
    assert format_server_time(13179201, 2) == "13:17:52.01"
    assert format_server_time(13179501, 2) == "13:17:55.01"
    assert format_server_time(13179801, 2) == "13:17:58.01"
    # 90 以下原样（不要误伤 51、59 这类合法秒）
    assert format_server_time(13175101, 2) == "13:17:51.01"
    assert format_server_time(13175901, 2) == "13:17:59.01"


def test_index_and_garbage_return_empty():
    # 指数该字段不是时钟（同一时刻 000001 解出 12:89 → MM 越界）
    assert format_server_time(12867393, 2) == ""
    assert format_server_time(0, 2) == ""
    # 小时越界
    assert format_server_time(113005895, 2) == ""


def test_digits_follow_decimal_point():
    assert price_decimal_digits(1, "600000") == 2
    assert price_decimal_digits(1, "510300") == 3
    assert price_decimal_digits(1, "019547") == 4


# ---------- 整条报文 ----------

def _quote_record(market: int, code: str, price_raw: int, time_raw: int,
                  vol: int = 1000, rise: int = 0) -> bytes:
    """按 0x053E 记录布局拼一条报价（金额字段填 0，不参与本节断言）。"""
    out = bytearray(struct.pack("<B6sH", market, code.encode(), 0))
    for v in (price_raw, 0, 0, 0, 0):        # price / lastclose / open / high / low
        out += put_price(v)
    out += put_price(time_raw)               # ← 快照时间
    out += put_price(-price_raw)             # unknown1（多数标的 = -price）
    out += put_price(vol)                    # vol
    out += put_price(1)                      # cur_vol
    out += struct.pack("<I", 0)              # amount（4 字节自定义浮点）
    for v in (vol // 2, vol // 2, 0, 0):      # s_vol / b_vol / unknown2 / unknown3
        out += put_price(v)
    for _ in range(5):                       # 五档：价差 0，量 1
        for v in (0, 0, 1, 1):
            out += put_price(v)
    out += struct.pack("<H", 0)              # trading_status
    for _ in range(4):
        out += put_price(0)
    out += struct.pack("<hH", rise, 0)       # rise_speed / active2
    return bytes(out)


def _body(records: list[bytes]) -> bytes:
    # 前 2 字节是服务端魔数（解析时跳过），随后是 2 字节条数
    return b"\x00\x00" + struct.pack("<H", len(records)) + b"".join(records)


def test_parse_response_sets_server_time():
    body = _body([
        _quote_record(1, "600000", 996, 13224002),
        _quote_record(1, "510300", 4105, 132245903),
    ])
    quotes = GetSecurityQuotesCmd([(1, "600000"), (1, "510300")]).parse_response(body)
    assert [q.server_time for q in quotes] == ["13:22:40.02", "13:22:45.903"]
    # 价格口径未受影响
    assert [round(q.price, 3) for q in quotes] == [9.96, 4.105]
    assert [q.decimal_point for q in quotes] == [2, 3]


def test_parse_response_blank_time_for_index():
    body = _body([
        _quote_record(1, "000001", 363808, 12867393),
        _quote_record(1, "600000", 996, 13179201),
    ])
    quotes = GetSecurityQuotesCmd([(1, "000001"), (1, "600000")]).parse_response(body)
    assert quotes[0].server_time == ""
    assert quotes[1].server_time == "13:17:52.01"
