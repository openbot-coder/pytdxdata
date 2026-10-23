"""CLI 价格小数位 / 分时时间列的显示回归。

覆盖两个曾经出错的地方：

1. ``tdx minute`` 的时间列按序号推算（``i // 60, i % 60``），240 根分时被打印成
   ``00:00~03:59``，与真实交易时间无关。
2. 价格固定写死 ``%8.2f``，可转债（报价步长 0.001 元）会被截成 2 位
   ——``128.892`` 打成 ``128.89``。

注意 ``price_decimal_digits`` 返回的是**解码位数**（原始整数 / 10**dp），可转债
是 4 位，但第 4 位恒为 0，显示层只该给 3 位（见 ``_display_dp``）。
"""
from __future__ import annotations

from pytdxdata.cli import (
    _display_dp,
    _dp_of,
    _price_fmt,
    cmd_minute,
    cmd_transactions,
    parse_symbol,
)
from pytdxdata.models import MinuteBar, TransactionRecord


class FakeTD:
    """只实现 minute / transactions 需要的两个接口。"""

    def __init__(self, minutes=None, ticks=None):
        self._minutes = minutes or []
        self._ticks = ticks or []

    async def get_minutes(self, sym, date=None):
        return self._minutes

    async def get_ticks(self, sym, date=None, count=None):
        return self._ticks


def _bar(price, hour, minute, vol=100.0, avg=0.0):
    return MinuteBar(price=price, vol=vol, avg_price=avg or price,
                     hour=hour, minute=minute, fields={})


def _tick(price, vol, trade_count=1, available=True, time="14:59:54"):
    return TransactionRecord(market=1, code="113708", time=time, price=price, vol=vol,
                             trade_count=trade_count, bs_flag=0, fields={},
                             trade_count_available=available)


# --------------------------------------------------------------------------- #
# 小数位判定
# --------------------------------------------------------------------------- #

def test_price_fmt_width_tracks_decimals():
    assert _price_fmt(2) == "%8.2f"      # 股票：保持原宽度，不破坏对齐
    assert _price_fmt(3) == "%9.3f"
    assert _price_fmt(4) == "%10.4f"
    assert _price_fmt(None) == "%8.2f"   # 缺省当 2 位


def test_display_dp_by_instrument():
    assert _display_dp(1, "600000") == 2        # 沪股票
    assert _display_dp(0, "000001") == 2        # 深股票
    assert _display_dp(1, "000001") == 2        # 指数
    assert _display_dp(1, "113708") == 3        # 沪可转债（解码 4 位，显示 3 位）
    assert _display_dp(1, "110102") == 3
    assert _display_dp(0, "123235") == 3        # 深可转债
    assert _display_dp(0, "128136") == 3
    assert _display_dp(1, "510310") == 3        # ETF
    assert _display_dp(1, "019547") == 4        # 国债：4 位是真实报价精度


def test_dp_of_uses_cn_only():
    assert _dp_of(parse_symbol("sh113708")) == 3
    assert _dp_of(parse_symbol("sh600000")) == 2
    assert _dp_of(parse_symbol("hk00700")) == 2     # 非 A 股固定 2 位


# --------------------------------------------------------------------------- #
# tdx minute
# --------------------------------------------------------------------------- #

async def test_cmd_minute_prints_real_clock(capsys):
    bars = [_bar(129.271, 9, 30), _bar(129.270, 9, 31), _bar(128.892, 14, 59)]
    await cmd_minute(FakeTD(minutes=bars), "sh113708", None)
    out = capsys.readouterr().out
    assert "09:30" in out and "09:31" in out and "14:59" in out
    assert "00:00" not in out and "03:59" not in out


async def test_cmd_minute_cb_keeps_three_decimals(capsys):
    await cmd_minute(FakeTD(minutes=[_bar(128.892, 14, 59)]), "sh113708", None)
    out = capsys.readouterr().out
    assert "128.892" in out
    assert "128.89 " not in out


async def test_cmd_minute_stock_two_decimals(capsys):
    await cmd_minute(FakeTD(minutes=[_bar(9.62, 15, 0)]), "sh600000", None)
    out = capsys.readouterr().out
    assert "9.62" in out and "9.620" not in out


# --------------------------------------------------------------------------- #
# tdx transactions
# --------------------------------------------------------------------------- #

async def test_cmd_transactions_cb_price_and_count(capsys):
    await cmd_transactions(FakeTD(ticks=[_tick(128.892, 3, trade_count=4)]),
                           "sh113708", 50, None)
    out = capsys.readouterr().out
    assert "128.892" in out
    assert "(4笔)" in out


async def test_cmd_transactions_etf_marks_missing_count(capsys):
    tick = TransactionRecord(market=1, code="510310", time="15:27:35", price=3.989,
                             vol=15, trade_count=0, bs_flag=0, fields={},
                             trade_count_available=False)
    await cmd_transactions(FakeTD(ticks=[tick]), "sh510310", 50, None)
    out = capsys.readouterr().out
    assert "3.989" in out          # ETF 也是 3 位
    assert "(-)" in out            # 不打印假笔数
    assert "笔)" not in out
