"""EX 0x23FA 报价盘口（bid/ask）解析与合并。

背景（issue #20，2026-11-24 实测）：0x23FA body 实际 300B，offset 70 起是
买5价(5f)+买5量(5I)+卖5价(5f)+卖5量(5I)。期权=完整五档；期货=仅一档
（2~5 档为 0）；股票/指数=全 0（无盘口）。旧解析只取基础价量字段，
公开 API ``get_quotes`` 对所有扩展市场 bid/ask 恒为空。
"""
from __future__ import annotations

import struct

import pytest

from pytdxdata.api import _EX_ORDERBOOK_MARKETS, _fill_ex_orderbook
from pytdxdata.models import ExMarket, SecurityQuote
from pytdxdata.protocol.commands.ex_proto import GetExInstrumentQuoteCmd


def _quote_body(bid_px, bid_vol, ask_px, ask_vol) -> bytes:
    """拼 0x23FA 响应体：14B 头 + 5f 价 + 9I + 80B 盘口 + 剩余填充（共 300B）。"""
    body = bytearray(300)
    struct.pack_into("<5f", body, 14, 100.0, 101.0, 105.0, 99.0, 102.0)  # pre_close/open/high/low/price
    struct.pack_into("<9I", body, 34, 0, 0, 12345, 67, 0, 0, 0, 0, 0)    # zongliang@2, xianliang@3
    struct.pack_into("<5f", body, 70, *bid_px)
    struct.pack_into("<5I", body, 90, *bid_vol)
    struct.pack_into("<5f", body, 110, *ask_px)
    struct.pack_into("<5I", body, 130, *ask_vol)
    return bytes(body)


# ---------- 解析层 ----------

def test_parse_full_five_levels() -> None:
    """期权形态：完整五档，按价>0 全部保留。"""
    body = _quote_body(
        [10.1, 10.0, 9.9, 9.8, 9.7], [3, 120, 12, 6, 112],
        [10.3, 10.5, 10.9, 11.0, 11.1], [1, 8, 5, 4, 1],
    )
    row = GetExInstrumentQuoteCmd(8, "10011482").parse_response(body)
    assert row is not None
    assert row["price"] == pytest.approx(102.0)
    assert row["zongliang"] == 12345 and row["xianliang"] == 67
    assert row["bid"] == [
        (pytest.approx(10.1), 3.0), (pytest.approx(10.0), 120.0),
        (pytest.approx(9.9), 12.0), (pytest.approx(9.8), 6.0),
        (pytest.approx(9.7), 112.0),
    ]
    assert row["ask"][0] == (pytest.approx(10.3), 1.0)
    assert len(row["ask"]) == 5


def test_parse_single_level_futures() -> None:
    """期货形态：仅买一/卖一，2~5 档为 0 → 过滤后各剩 1 档。"""
    body = _quote_body(
        [10164.0, 0.0, 0.0, 0.0, 0.0], [1, 0, 0, 0, 0],
        [10168.0, 0.0, 0.0, 0.0, 0.0], [2, 0, 0, 0, 0],
    )
    row = GetExInstrumentQuoteCmd(30, "AG2612").parse_response(body)
    assert row is not None
    assert row["bid"] == [(pytest.approx(10164.0), 1.0)]
    assert row["ask"] == [(pytest.approx(10168.0), 2.0)]


def test_parse_no_orderbook() -> None:
    """股票/指数形态：盘口全 0 → bid/ask 均为空列表。"""
    body = _quote_body([0.0] * 5, [0] * 5, [0.0] * 5, [0] * 5)
    row = GetExInstrumentQuoteCmd(31, "00700").parse_response(body)
    assert row is not None
    assert row["bid"] == [] and row["ask"] == []


def test_parse_short_body_returns_none() -> None:
    assert GetExInstrumentQuoteCmd(8, "10011482").parse_response(b"\x00" * 100) is None


# ---------- 合并层 ----------

def _quote(market: int, code: str) -> SecurityQuote:
    return SecurityQuote(
        market=market, code=code, symbol=f"m{market}:{code}",
        price=102.0, pre_close=100.0, open=101.0, high=105.0, low=99.0,
        vol=1.0, amount=1.0, s_vol=0.0, b_vol=0.0,
    )


class _FakeApi:
    """替代 TdxData._execute_ex 的最小桩。"""

    def __init__(self, rows: dict[tuple[int, str], dict | None]) -> None:
        self.rows = rows
        self.asked: list[tuple[int, str]] = []

    async def _execute_ex(self, cmd) -> object:
        self.asked.append((cmd.market, cmd.code))
        return self.rows.get((cmd.market, cmd.code))


@pytest.mark.asyncio
async def test_fill_orderbook_merges_bid_ask() -> None:
    q = _quote(int(ExMarket.SH_STOCK_OPTION), "10011482")
    api = _FakeApi({(8, "10011482"): {
        "bid": [(10.1, 3.0)], "ask": [(10.3, 1.0)],
    }})
    await _fill_ex_orderbook(api, [q])
    assert api.asked == [(8, "10011482")]
    assert q.bid == [(10.1, 3.0)] and q.ask == [(10.3, 1.0)]


@pytest.mark.asyncio
async def test_fill_orderbook_skips_stock_markets() -> None:
    """股票/指数市场不在盘口集合内，不发 0x23FA。"""
    q = _quote(int(ExMarket.HK_MAIN_BOARD), "00700")
    api = _FakeApi({})
    await _fill_ex_orderbook(api, [q])
    assert api.asked == []


@pytest.mark.asyncio
async def test_fill_orderbook_tolerates_failure() -> None:
    """命令失败/空盘口 → 保持空列表，不抛异常。"""
    qs = [_quote(int(ExMarket.SH_FUTURES), "AG2612"),
          _quote(int(ExMarket.CFFEX_FUTURES), "IF2612")]
    api = _FakeApi({(30, "AG2612"): None})
    async def _boom(cmd):
        raise ConnectionError("down")
    api._execute_ex = _boom  # type: ignore[method-assign]
    await _fill_ex_orderbook(api, qs)  # 不抛
    assert qs[0].bid == [] and qs[1].bid == []


def test_orderbook_markets_exclude_stocks() -> None:
    assert int(ExMarket.SH_STOCK_OPTION) in _EX_ORDERBOOK_MARKETS
    assert int(ExMarket.CFFEX_FUTURES) in _EX_ORDERBOOK_MARKETS
    assert int(ExMarket.HK_MAIN_BOARD) not in _EX_ORDERBOOK_MARKETS
    assert int(ExMarket.US_STOCK) not in _EX_ORDERBOOK_MARKETS
    assert int(ExMarket.HK_INDEX) not in _EX_ORDERBOOK_MARKETS
