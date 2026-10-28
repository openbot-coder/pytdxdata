"""K线批量流水线路径（多连接 × 单连接流水线）。

背景：单只K线一请求一标的，全市场 5,000 只就是 5,000 次往返。串行时每只固定付
一个 RTT（实测 33ms），实测 24 台 × 8 流水能把全市场 1 分钟K线取当天从 43.6s
压到 5s 级。这里用替身验证「路径选择」与「结果口径」，不依赖网络。
"""

from __future__ import annotations

import datetime

import pytest

from pytdxdata import TdxData
from pytdxdata.api import _auto_pipeline, _bars_for_range, _elapsed_trading_minutes
from pytdxdata.models import KlinePeriod, SecurityBar

# 替身的时间锚点必须跟着**真实 now** 走：API 内部用 ``datetime.now()`` 判断「是不是今天」，
# 用固定日期会在 CI（UTC 时区，与本地差 8 小时）上漂到别的时段分支，断言时绿时红。
NEWEST = datetime.datetime.now().replace(microsecond=0)  # noqa: DTZ001


def _bar(when: datetime.datetime, close: float = 10.0) -> SecurityBar:
    return SecurityBar(
        market=1, code="600000", open=10.0, high=10.1, low=9.9, close=close,
        vol=1000, amount=10000, year=when.year, month=when.month, day=when.day,
        hour=when.hour, minute=when.minute)


class _MacFakeConn:
    """MAC 通道语义替身：**offset 0 = 最新一根**，偏移越大越老。"""

    seen: list[tuple[str, int, int, bool]] = []   # (命令名, start, count, 是否流水线)
    fail_pipeline = False

    def __init__(self, host="m1", port=7709, timeout=10.0):
        self.host = host
        self._alive = True

    @property
    def alive(self) -> bool:
        return self._alive

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self._alive = False

    def _rows(self, cmd, *, record: bool = True):
        if record:
            type(self).seen.append(
                (type(cmd).__name__, getattr(cmd, "start", 0),
                 getattr(cmd, "count", 0), False))
        if "MacKlineCmd" not in type(cmd).__name__:
            return []
        out = []
        for off in range(cmd.start, cmd.start + cmd.count):
            if off > 20_000:
                continue
            when = NEWEST - datetime.timedelta(minutes=off)
            out.append(_bar(when, 10.0 + (20_000 - off) * 0.001))
        return out

    async def execute(self, cmd):
        return self._rows(cmd)

    async def execute_pipeline(self, cmds, **kwargs):
        if type(self).fail_pipeline:
            raise ConnectionError("流水线炸了")
        for c in cmds:
            type(self).seen.append(
                (type(c).__name__, getattr(c, "start", 0), getattr(c, "count", 0), True))
        return [self._rows(c, record=False) for c in cmds]


def _syms(n: int) -> list[str]:
    """n 只沪市股票代码（互不相同，避免按 symbol 归并时互相覆盖）。"""
    return [f"sh60{i:04d}" for i in range(n)]


async def _run(n: int, period=KlinePeriod.MIN_1, **kw):
    _MacFakeConn.seen = []
    _MacFakeConn.fail_pipeline = False
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pytdxdata.api.get_mac_hosts", lambda: ["m1", "m2"])
        mp.setattr("pytdxdata.pool.pool.TdxConnection",
                   lambda h, p=7709, t=10.0: _MacFakeConn(h))
        td = TdxData(cache_dir=None, pool_min=1, pool_max=4)
        await td.start()
        try:
            bars = await td.get_bars(_syms(n), period, **kw)
        finally:
            await td.close()
    return bars


def _data_calls() -> list[tuple[str, int, int, bool]]:
    """只看真正的数据请求（排除建连探针：count=100 那条）。"""
    return [c for c in _MacFakeConn.seen if c[2] != 100]


@pytest.mark.asyncio
async def test_bulk_path_uses_pipeline_for_many_symbols():
    bars = await _run(20, count=240)

    data = _data_calls()
    assert data, "应该有数据请求"
    assert all(via_pipe for *_, via_pipe in data), "20 只必须走流水线"
    assert len(data) == 20
    assert len(bars) == 20 * 240


@pytest.mark.asyncio
async def test_small_batch_falls_back_to_single():
    """低于阈值走逐只：不经过流水线，结果不变。"""
    bars = await _run(5, count=240)

    data = _data_calls()
    assert data and not any(via_pipe for *_, via_pipe in data)
    assert len(bars) == 5 * 240


@pytest.mark.asyncio
async def test_pipeline_1_disables_bulk():
    bars = await _run(20, count=240, pipeline=1)

    data = _data_calls()
    assert data and not any(via_pipe for *_, via_pipe in data)
    assert len(bars) == 20 * 240


@pytest.mark.asyncio
async def test_result_order_follows_input_order():
    """返回按入参标的顺序拼接（组内升序）。"""
    bars = await _run(20, count=30)

    seen: list[str] = []
    for b in bars:
        if not seen or seen[-1] != b.symbol:
            seen.append(b.symbol)
    assert seen == _syms(20)
    # 组内按时间正序
    head = [b.datetime for b in bars if b.symbol == _syms(20)[0]]
    assert head == sorted(head)


@pytest.mark.asyncio
async def test_long_count_is_paged_inside_pipeline():
    """count 超过单请求上限时按页切分，且页也走流水线（不是逐只串行翻页）。"""
    bars = await _run(20, count=1600, pipeline=8)

    data = _data_calls()
    assert all(via_pipe for *_, via_pipe in data)
    assert len(data) == 20 * 3          # 700 + 700 + 200
    assert {c[2] for c in data} == {700, 200}
    assert len(bars) == 20 * 1600


@pytest.mark.asyncio
async def test_pipeline_failure_falls_back_to_single_per_symbol():
    """流水线失败 → 该批交回逐只重试，结果不丢。"""
    _MacFakeConn.seen = []
    _MacFakeConn.fail_pipeline = True
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("pytdxdata.api.get_mac_hosts", lambda: ["m1", "m2"])
        mp.setattr("pytdxdata.pool.pool.TdxConnection",
                   lambda h, p=7709, t=10.0: _MacFakeConn(h))
        td = TdxData(cache_dir=None, pool_min=1, pool_max=4)
        await td.start()
        try:
            bars = await td.get_bars(_syms(20), KlinePeriod.MIN_1, count=240)
        finally:
            await td.close()
    assert len(bars) == 20 * 240


@pytest.mark.asyncio
async def test_dated_range_bulk_trims_to_start_date():
    """日期区间走批量时，区间左端仍然精确裁剪。"""
    start = NEWEST - datetime.timedelta(minutes=30)
    bars = await _run(20, start_date=start)

    assert bars
    assert all(b.datetime >= start for b in bars)
    assert all(b.symbol in set(_syms(20)) for b in bars)


@pytest.mark.asyncio
async def test_dated_future_range_returns_empty_without_requests():
    future = NEWEST + datetime.timedelta(days=5)
    bars = await _run(20, start_date=future)

    assert bars == []


# ---------------- 纯函数 ----------------
def test_elapsed_trading_minutes():
    def at(h, m):
        return datetime.datetime(2026, 10, 28, h, m)  # noqa: DTZ001

    assert _elapsed_trading_minutes(at(9, 0)) == 0
    assert _elapsed_trading_minutes(at(9, 30)) == 0
    assert _elapsed_trading_minutes(at(10, 0)) == 30
    assert _elapsed_trading_minutes(at(11, 45)) == 120     # 午休
    assert _elapsed_trading_minutes(at(13, 30)) == 150
    assert _elapsed_trading_minutes(at(15, 0)) == 240
    assert _elapsed_trading_minutes(at(20, 0)) == 240      # 收盘后


def test_bars_for_range_shrinks_intraday():
    """盘中只需已走完的根数，不是一整天 240 根。"""
    morning = datetime.datetime(2026, 10, 28, 10, 0)   # noqa: DTZ001
    midnight = datetime.datetime(2026, 10, 28, 0, 0)   # noqa: DTZ001
    # 10:00 走了 30 分钟 → 30 + 余量，远小于 240
    assert 30 <= _bars_for_range(KlinePeriod.MIN_1, midnight, morning) <= 40
    # 收盘后回到一整天的量级
    close = datetime.datetime(2026, 10, 28, 16, 0)     # noqa: DTZ001
    assert _bars_for_range(KlinePeriod.MIN_1, midnight, close) >= 245


def test_bars_for_range_past_days():
    now = datetime.datetime(2026, 10, 28, 16, 0)       # noqa: DTZ001
    start = now - datetime.timedelta(days=5)
    n = _bars_for_range(KlinePeriod.DAY, start, now)
    assert 5 <= n <= 12       # 5 个自然日 × 1.6 余量


def test_auto_pipeline():
    # 每根 32B：小 count 用深流水，大 count 用浅流水（在途字节数恒定）
    assert _auto_pipeline(2, None) == 32
    assert _auto_pipeline(240, None) == 8
    assert _auto_pipeline(240, 4) == 4        # 显式指定优先
    assert _auto_pipeline(2, 1) == 2          # 夹到最小值 2
