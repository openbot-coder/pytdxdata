"""证券清单（0x053E）回归测试：请求长度 + 北交所降级。全部离线。

背景（2026-10-23 实测）：`build_request` 曾经多塞一个 ``H``（18 字节），
服务端只消费前 16 字节，剩下的 ``00 00`` 留在它的接收缓冲里，把**下一个请求**
的开头吃掉 → 同一连接「第一次成功、第二次起全部通信超时」。
`get_universe` 的分页器复用连接，于是 66/28 页全靠失败重连，冷启动 12~18s。
"""
from __future__ import annotations

import struct
import unittest.mock as mock

import pytest

from pytdxdata.models import Market
from pytdxdata.protocol.commands import GetSecurityListCmd

_HEADER = bytes.fromhex("0c0118640101060006005004")


def test_request_is_exactly_16_bytes():
    """12 字节命令头 + market(H) + start(H)，一个字节都不能多。"""
    req = GetSecurityListCmd(1, 0).build_request()
    assert len(req) == 16
    assert req == _HEADER + struct.pack("<HH", 1, 0)


def test_request_encodes_market_and_start_little_endian():
    req = GetSecurityListCmd(0, 2000).build_request()
    assert req[:12] == _HEADER
    market, start = struct.unpack("<HH", req[12:])
    assert (market, start) == (0, 2000)


def test_request_has_no_trailing_padding():
    """尾部不能有 padding：多余的 0 会让服务端解析错位。"""
    for market in (0, 1, 2):
        for start in (0, 1000, 27000):
            req = GetSecurityListCmd(market, start).build_request()
            assert len(req) == 16, f"market={market} start={start} -> {len(req)} 字节"


def test_parse_response_reads_all_records():
    """29 字节/条，头部 2 字节条数。"""
    code, name = b"600000", "浦发银行".encode("gbk").ljust(8, b"\x00")
    rec = struct.pack("<6sH8s4sBI4s", code, 100, name, b"\x00" * 4, 2, 0, b"\x00" * 4)
    assert len(rec) == 29
    body = struct.pack("<H", 2) + rec + rec
    rows = GetSecurityListCmd(1, 0).parse_response(body)
    assert len(rows) == 2
    assert rows[0].code == "600000"
    assert rows[0].decimal_point == 2


@pytest.mark.asyncio
async def test_bj_universe_returns_empty_without_network():
    """北交所清单协议不支持：必须立刻返回空，且**不碰连接池**。"""
    from pytdxdata.api import TdxData

    td = TdxData()
    td._std_pool.acquire = mock.AsyncMock(side_effect=AssertionError("不该联网"))
    td._std_pool.acquire_many = mock.AsyncMock(
        side_effect=AssertionError("不该联网"))
    rows = await td.get_universe("bj")
    assert rows == []
    td._std_pool.acquire.assert_not_called()
    td._std_pool.acquire_many.assert_not_called()


@pytest.mark.asyncio
async def test_bj_market_is_in_unsupported_set():
    from pytdxdata import api as api_mod

    assert int(Market.BJ) in api_mod._LIST_UNSUPPORTED_MARKETS


@pytest.mark.asyncio
async def test_ex_all_fetched_once_in_get_universe():
    """`get_universe()` 跨多个 EX 市场时，EX 全量只能取一次。"""
    from pytdxdata.api import TdxData

    td = TdxData()
    calls = {"n": 0}

    async def fake_ex_all():
        calls["n"] += 1
        return []

    td._ex_all = fake_ex_all
    await td.get_universe("all")
    assert calls["n"] == 1, f"_ex_all 被调用 {calls['n']} 次（应为 1）"
