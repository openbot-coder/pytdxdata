"""EX 0x23FF K线的字段语义：``amount`` / ``settle`` 按市场分别回填。

背景（2026-10-28 实测）：0x23FF 每条 32B = 4B 时间戳 + **28B 行情**，这 28B 只有
**一个** 4B 槽位可复用，语义随市场变化 ——

* 权益类（港股主板/创业板/基金、新三板、美股）：该槽位是**成交额**，
  校验 ``成交额 / (close × vol)`` ≈ 100（港股主板/基金，vol 单位是手）或 ≈ 1（其余，vol 是股）；
* 期货期权类：该槽位是**结算价**，成交额在帧内根本不存在（恒 0.0）。

改之前 ``amount`` 被硬编码成 0.0、末位 float 直接丢弃，于是港股/美股拿不到成交额、
期货期权拿不到结算价。
"""
from __future__ import annotations

import pickle
import struct

import pytest

from pytdxdata.api import TdxData
from pytdxdata.models import SecurityBar
from pytdxdata.protocol.commands.ex_proto import (
    _EX_AMOUNT_MARKETS,
    GetExInstrumentBarsCmd,
)
from pytdxdata.symbols import parse_symbols

DAY = 20261028


def _f2u(value: float) -> int:
    """float32 -> 其位模式的 uint32（用来拼「同一槽位两种解释」的报文）。"""
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _ex_body(rows: list[tuple[int, ...]], category: int = 4) -> bytes:
    """拼 0x23FF 响应体：20B 头（count@18）+ N × 32B（时间4B + 7×4B）。"""
    body = bytearray(20 + 32 * len(rows))
    struct.pack_into("<H", body, 18, len(rows))
    for i, words in enumerate(rows):
        off = 20 + i * 32
        struct.pack_into("<I", body, off, DAY)
        struct.pack_into("<7I", body, off + 4, *words)
    return bytes(body)


def _equity_words(close: float, amount: float, vol: int) -> tuple[int, ...]:
    """权益类布局：OHLC + 成交额(float) + 成交量(uint) + 保留(float)。"""
    return (_f2u(close), _f2u(close), _f2u(close), _f2u(close),
            _f2u(amount), vol, _f2u(0.0))


def _derivative_words(close: float, position: int, vol: int,
                      settle: float) -> tuple[int, ...]:
    """期货/期权布局：OHLC + 持仓量(uint) + 成交量(uint) + 结算价(float)。"""
    return (_f2u(close), _f2u(close), _f2u(close), _f2u(close),
            position, vol, _f2u(settle))


# ---------- 解析层 ----------

@pytest.mark.parametrize("market", [31, 48, 49, 44, 74])
def test_equity_markets_read_amount_from_fifth_slot(market: int) -> None:
    cmd = GetExInstrumentBarsCmd(market, "00700", 4, 0, 1)
    rows = cmd.parse_response(_ex_body([_equity_words(10.2, 2_040_000.0, 2000)]))

    assert len(rows) == 1
    assert rows[0]["amount"] == pytest.approx(2_040_000.0)
    assert rows[0]["trade"] == 2000          # 成交量不受影响
    assert rows[0]["position"] == 0           # 权益类无持仓量概念
    assert rows[0]["settle"] is None


@pytest.mark.parametrize("market", [7, 8, 9, 47])
def test_derivative_markets_read_settle_from_seventh_slot(market: int) -> None:
    cmd = GetExInstrumentBarsCmd(market, "IFL0", 4, 0, 1)
    rows = cmd.parse_response(_ex_body([_derivative_words(4021.0, 49_772, 25_392, 4047.8)]))

    assert rows[0]["amount"] == 0.0           # 帧内没有成交额，不是解析漏读
    assert rows[0]["position"] == 49_772
    assert rows[0]["trade"] == 25_392
    assert rows[0]["settle"] == pytest.approx(4047.8)


def test_intraday_settle_zero_normalized_to_none() -> None:
    """结算价是日终量：分钟线该槽位恒 0，归一化成 None 而不是 0.0。"""
    cmd = GetExInstrumentBarsCmd(47, "IFL0", 7, 0, 1)
    rows = cmd.parse_response(_ex_body([_derivative_words(4054.6, 49_490, 260, 0.0)], 7))

    assert rows[0]["settle"] is None


def test_index_markets_kept_out_of_amount_table() -> None:
    """港股指数(27)/中证指数(62) 该槽位既非成交额也非结算价 —— 不能猜，保持 0.0。"""
    assert 27 not in _EX_AMOUNT_MARKETS
    assert 62 not in _EX_AMOUNT_MARKETS
    cmd = GetExInstrumentBarsCmd(27, "HSI", 4, 0, 1)
    rows = cmd.parse_response(_ex_body([_equity_words(23041.32, 178_348.0, 17_835)]))
    assert rows[0]["amount"] == 0.0


# ---------- API 层透传 ----------

def _fake_api(rows_for: dict[int, list[dict]]) -> TdxData:
    td = TdxData(cache_dir=None, pool_min=1, pool_max=2)
    seen: list[tuple[int, str]] = []

    async def fake(make_cmd, spec):            # noqa: ANN001 — 与真实签名同形
        cmd = make_cmd(0, spec.page_size)
        seen.append((cmd.market, cmd.code))
        return rows_for[cmd.market]

    td._fetch_paginated_ex = fake             # type: ignore[method-assign]
    td._seen_ex = seen                        # type: ignore[attr-defined]
    return td


def _row(market: int, code: str, **kw) -> dict:
    base = {"year": 2026, "month": 10, "day": 28, "hour": 15, "minute": 0,
            "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.2,
            "amount": 0.0, "position": 0, "trade": 2000, "settle": None}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_bars_ex_backfills_amount_for_hk() -> None:
    td = _fake_api({31: [_row(31, "00700", amount=2_040_000.0)]})
    bars = await td.get_bars("hk00700", count=1)

    assert len(bars) == 1
    assert bars[0].amount == pytest.approx(2_040_000.0)
    assert bars[0].vol == 2000
    assert bars[0].settle is None


@pytest.mark.asyncio
async def test_bars_ex_backfills_settle_for_futures() -> None:
    td = _fake_api({47: [_row(47, "IFL0", position=49_772, trade=25_392, settle=4047.8)]})
    bars = await td.get_bars("cffex_futures:IFL0", count=1)

    assert bars[0].settle == pytest.approx(4047.8)
    assert bars[0].amount == 0.0


# ---------- 旧磁盘缓存兼容 ----------

def test_legacy_pickle_without_settle_still_loads() -> None:
    """升级前写下的缓存没有 settle 槽位；默认反序列化不会补默认值，读它必崩。"""
    bar = SecurityBar(market=1, code="600000", open=1.0, high=2.0, low=0.5,
                      close=1.5, vol=100.0, amount=150.0,
                      year=2026, month=10, day=28, settle=12.3)
    _, slots = bar.__getstate__()
    legacy = dict(slots)
    legacy.pop("settle")

    revived = SecurityBar.__new__(SecurityBar)
    revived.__setstate__((None, legacy))

    assert revived.settle is None
    assert revived.close == 1.5


def test_new_pickle_roundtrip_keeps_settle() -> None:
    bar = SecurityBar(market=47, code="IFL0", open=4021.0, high=4060.0, low=4010.0,
                      close=4054.6, vol=25_618.0, amount=0.0,
                      year=2026, month=10, day=28, settle=4052.6)

    assert pickle.loads(pickle.dumps(bar)).settle == pytest.approx(4052.6)


def test_ex_symbols_route_to_ex_path() -> None:
    """守住前提：上面对 31/47 的断言确实走的是 EX 通道。"""
    assert parse_symbols(["hk00700"])[0].is_ex
    assert parse_symbols(["cffex_futures:IFL0"])[0].is_ex
