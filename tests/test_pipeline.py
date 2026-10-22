"""单连接流水线（execute_pipeline）单元测试：不连真实网络。

关键防线：流水线把多条请求塞进同一条 TCP 流，一旦响应与请求错配就是
「价格张冠李戴」级别的灾难，所以这里逐项锁死顺序、长度与失败语义。
"""
from __future__ import annotations

import struct
import unittest.mock as mock

import pytest

from pytdxdata.pool.connection import TdxConnection
from pytdxdata.pool.pool import PooledConnection
from pytdxdata.protocol.commands.base import BaseCommand


class EchoCmd(BaseCommand[bytes]):
    """请求体即 tag，响应体原样带回，便于断言顺序。"""

    def __init__(self, tag: bytes) -> None:
        self.tag = tag

    def build_request(self) -> bytes:
        return b"REQ:" + self.tag

    def parse_response(self, body: bytes) -> bytes:
        return body


class FakeQuote:
    """最小报价替身：stamp() 会写 symbol，需要 market/code 是可 int/str 的真值。"""

    def __init__(self) -> None:
        self.market = 0
        self.code = "000001"
        self.symbol = ""


def frame(body: bytes) -> bytes:
    """构造一帧未压缩响应（zipsize == unzipsize）。"""
    return struct.pack("<IIIHH", 7654321, 0, 0x053E, len(body), len(body)) + body


class FakeReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    async def read(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += len(chunk)
        return chunk


class FakeWriter:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False
        self.transport = FakeQuote()

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


def make_conn(payload: bytes, host: str = "1.2.3.4") -> TdxConnection:
    conn = TdxConnection(host, 7709, 5.0)
    conn._reader = FakeReader(payload)      # type: ignore[assignment]
    conn._writer = FakeWriter()             # type: ignore[assignment]
    return conn


# ---------- 基本语义 ----------

@pytest.mark.asyncio
async def test_empty_cmds_returns_empty():
    conn = make_conn(b"")
    assert await conn.execute_pipeline([]) == []


@pytest.mark.asyncio
async def test_responses_paired_in_order():
    bodies = [b"AAA", b"BBB", b"CCC"]
    conn = make_conn(b"".join(frame(b) for b in bodies))
    cmds = [EchoCmd(t) for t in (b"1", b"2", b"3")]
    got = await conn.execute_pipeline(cmds)
    assert got == bodies                     # 顺序必须与 cmds 严格一致


@pytest.mark.asyncio
async def test_all_requests_sent_at_once():
    conn = make_conn(b"".join(frame(b"X") for _ in range(3)))
    cmds = [EchoCmd(t) for t in (b"a", b"b", b"c")]
    await conn.execute_pipeline(cmds)
    assert conn._writer.written == b"REQ:aREQ:bREQ:c"


@pytest.mark.asyncio
async def test_closed_connection_raises():
    conn = make_conn(b"")
    conn._writer = None
    with pytest.raises(ConnectionError):
        await conn.execute_pipeline([EchoCmd(b"1")])


# ---------- 失败语义：响应流错配后必须废连接 ----------

@pytest.mark.asyncio
async def test_truncated_stream_force_closes():
    # 只给第一帧完整的、第二帧只有半个头 → 读不满，抛错且连接作废
    conn = make_conn(frame(b"AAA") + b"\x00" * 5)
    with pytest.raises(ConnectionError):
        await conn.execute_pipeline([EchoCmd(b"1"), EchoCmd(b"2")])
    assert conn._writer is None              # _force_close 已清空 writer


@pytest.mark.asyncio
async def test_body_length_mismatch_propagates():
    bad = struct.pack("<IIIHH", 7654321, 0, 0x053E, 99, 99) + b"short"
    conn = make_conn(bad)
    # 帧头自称 99 字节但流里没有 → _recv_exact 读空，连接作废
    with pytest.raises(ConnectionError):
        await conn.execute_pipeline([EchoCmd(b"1")])
    assert conn._writer is None


# ---------- 池包装转发 ----------

@pytest.mark.asyncio
async def test_pooled_connection_forwards_pipeline():
    inner = mock.AsyncMock()
    inner.execute_pipeline = mock.AsyncMock(return_value=["r1", "r2"])
    pc = PooledConnection(inner, "host1")
    got = await pc.execute_pipeline([EchoCmd(b"1"), EchoCmd(b"2")])
    assert got == ["r1", "r2"]
    inner.execute_pipeline.assert_awaited_once()


# ---------- api 层：切批是否走流水线 ----------

def _api_with_fake_pool():
    from pytdxdata.api import TdxData
    from pytdxdata.symbols import CN, Symbol

    td = TdxData.__new__(TdxData)            # 跳过 __init__（不建网络池）
    td._std_pool = FakeQuote()
    td._std_pool.max_size = 24
    td._std_pool.servers = [f"s{i}" for i in range(11)]
    td._std_pool.acquire = mock.AsyncMock()
    td._std_pool.release = mock.AsyncMock()
    td._std_pool.warm = mock.AsyncMock()      # 预建连接是网络动作，测试里空转
    return td, Symbol, CN


@pytest.mark.asyncio
async def test_quotes_std_uses_pipeline_for_multi_page():
    td, Symbol, CN = _api_with_fake_pool()
    syms = [Symbol(0, f"{i:06d}", CN) for i in range(1, 401)]   # 5 片
    chunks = []

    async def fake_pipeline(batch_group):
        chunks.append(len(batch_group))
        return [[FakeQuote()] * 80 for _ in batch_group]

    td._execute_std_pipeline = fake_pipeline
    out = await td._quotes_std(syms, pipeline=True)
    assert len(out) == 5 * 80
    assert sum(chunks) == 5                  # 每片恰好被处理一次，不多不少


@pytest.mark.asyncio
async def test_quotes_std_single_page_skips_pipeline():
    td, Symbol, CN = _api_with_fake_pool()
    called = []
    td._execute_std_pipeline = mock.AsyncMock(return_value=[])

    async def fake_std(cmd):
        called.append(1)
        return []

    td._execute_standard = fake_std
    syms = [Symbol(0, f"{i:06d}", CN) for i in range(1, 51)]    # 1 片
    await td._quotes_std(syms, pipeline=True)
    assert called == [1]
    td._execute_std_pipeline.assert_not_awaited()


@pytest.mark.asyncio
async def test_quotes_std_no_pipeline_flag_uses_plain_path():
    td, Symbol, CN = _api_with_fake_pool()

    async def fake_std(cmd):
        return [FakeQuote()] * 80

    td._execute_standard = fake_std
    td._execute_std_pipeline = mock.AsyncMock(return_value=[])
    syms = [Symbol(0, f"{i:06d}", CN) for i in range(1, 401)]
    out = await td._quotes_std(syms, pipeline=False)
    assert len(out) == 5 * 80
    td._execute_std_pipeline.assert_not_awaited()


@pytest.mark.asyncio
async def test_pipeline_failure_falls_back_to_per_page():
    """节点不支持流水线时，降级逐片，结果条数不变。"""
    td, Symbol, CN = _api_with_fake_pool()

    pool = td._std_pool
    pool.acquire = mock.AsyncMock(return_value=mock.Mock(
        execute_pipeline=mock.AsyncMock(side_effect=ConnectionError("boom"))))
    pool.release = mock.AsyncMock()

    calls = []

    async def fake_std(cmd):
        calls.append(1)
        return [FakeQuote()] * 80

    td._execute_standard = fake_std
    syms = [Symbol(0, f"{i:06d}", CN) for i in range(1, 241)]   # 3 片
    out = await td._execute_std_pipeline(
        [syms[i:i + 80] for i in range(0, len(syms), 80)])
    assert len(out) == 3 and len(calls) == 3
    assert pool.release.await_args.kwargs.get("broken") is True
