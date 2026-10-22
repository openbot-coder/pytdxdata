"""指数成分成交汇总（get_index_members / get_index_trade_stats）单元测试。

全部离线：成分与逐笔都用实例属性打补丁，不建立任何真实连接。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pytdxdata import TdxData
from pytdxdata.models import (
    IndexTradeStats,
    MemberQuote,
    MinuteTradeStat,
    TransactionRecord,
)

# ---------- _index_pair：裸代码按指数语义解析 ----------

@pytest.mark.parametrize("text,expect", [
    ("000001", (1, "000001")),      # 上证指数（不是平安银行！）
    ("sh000001", (1, "000001")),
    ("000300", (1, "000300")),
    ("sh000300", (1, "000300")),
    ("000905", (1, "000905")),
    ("999999", (1, "999999")),
    ("880305", (1, "880305")),
    ("399001", (0, "399001")),      # 深证成指
    ("sz399006", (0, "399006")),
    ("899050", (2, "899050")),      # 北证50
    ("bj899050", (2, "899050")),
    ("sh.000300", (1, "000300")),   # 点号/冒号都容忍
    ("sz:399001", (0, "399001")),
])
def test_index_pair_parses(text, expect):
    assert TdxData._index_pair(text) == expect


@pytest.mark.parametrize("text", [
    "", "600000", "sz000001x", "sh12345", "12345", "abc", "zzz000001",
])
def test_index_pair_rejects(text):
    with pytest.raises(ValueError):
        TdxData._index_pair(text)


def test_index_pair_rejects_non_index_cn_code():
    """000001 之外的普通股票段（如 600xxx）不是指数，裸写必须报错。"""
    with pytest.raises(ValueError):
        TdxData._index_pair("600000")


# ---------- 模型：累计曲线 ----------

def test_index_trade_stats_cumulative():
    st = IndexTradeStats(
        index="sh000300", date=None, member_count=2, active_members=2,
        trade_count_members=2,
        minutes=[
            MinuteTradeStat("09:30", 100.0, 10, 1000.0),
            MinuteTradeStat("09:31", 200.0, 20, 2000.0),
            MinuteTradeStat("09:32", 50.0, 5, 500.0),
        ],
        total_volume=350.0, total_trade_count=35, total_amount=3500.0,
    )
    cum = st.cumulative()
    assert [c.time for c in cum] == ["09:30", "09:31", "09:32"]
    assert [c.volume for c in cum] == [100.0, 300.0, 350.0]
    assert [c.trade_count for c in cum] == [10, 30, 35]
    assert [c.amount for c in cum] == [1000.0, 3000.0, 3500.0]
    # 累计必须是新对象，不污染原始增量
    assert st.minutes[1].volume == 200.0
    assert st.cum_trade_count() == [("09:30", 10), ("09:31", 30), ("09:32", 35)]


def test_index_trade_stats_trade_count_available():
    ok = IndexTradeStats(index="sh000300", date=None, member_count=1,
                         active_members=1, trade_count_members=1)
    assert ok.trade_count_available is True
    bj = IndexTradeStats(index="bj899050", date=None, member_count=1,
                         active_members=1, trade_count_members=0)
    assert bj.trade_count_available is False


# ---------- get_index_members：分页终止 ----------

def _member(market: int, code: str, name: str = "") -> MemberQuote:
    return MemberQuote(market=market, code=code, name=name,
                       fields={"0x0": 9.9, "0x5": 1000.0, "0x7": 99000.0})


@pytest.mark.asyncio
async def test_get_index_members_pages_until_short_batch():
    """短页（不足 80 条）即视为最后一页，不再多发一次空请求。"""
    calls: list[int] = []

    async def fake_exec(cmd):
        calls.append(cmd.start)
        if cmd.start == 0:
            return [_member(1, "600000"), _member(1, "600004")]
        return []

    td = TdxData(cache_dir=None)
    td._execute_mac = fake_exec
    members = await td.get_index_members("000016")
    assert [m.symbol for m in members] == ["sh600000", "sh600004"]
    assert calls == [0]


@pytest.mark.asyncio
async def test_get_index_members_continues_on_full_page():
    """满页（80 条）后继续翻页，直到空页。"""
    calls: list[int] = []

    async def fake_exec(cmd):
        calls.append(cmd.start)
        if cmd.start == 0:
            return [_member(1, "600000")] * cmd.page_size
        return []

    td = TdxData(cache_dir=None)
    td._execute_mac = fake_exec
    members = await td.get_index_members("000016")
    assert len(members) == 80
    assert calls == [0, 80]


@pytest.mark.asyncio
async def test_get_index_members_respects_count_cap():
    async def fake_exec(cmd):
        # 永远返回满页，验证 count 能截断结果
        return [_member(1, "600000")] * cmd.page_size

    td = TdxData(cache_dir=None)
    td._execute_mac = fake_exec
    members = await td.get_index_members("000016", count=5)
    assert len(members) == 5


# ---------- get_index_trade_stats：按分钟聚合 ----------

def _tick(sym: str, hhmm: str, price: float, vol: float, tc: int) -> TransactionRecord:
    market, code = (1, sym[2:]) if sym.startswith("sh") else (0, sym[2:])
    return TransactionRecord(market=market, code=code, time=f"{hhmm}:00",
                             price=price, vol=vol, trade_count=tc, bs_flag=1,
                             symbol=sym)


@pytest.mark.asyncio
async def test_get_index_trade_stats_aggregates_minutes():
    async def fake_ticks(symbols, date=None, concurrency=6, **kw):
        out = []
        for s in symbols:
            if s == "sh600000":
                out += [_tick(s, "09:30", 10.0, 100.0, 10),
                        _tick(s, "09:30", 10.0, 50.0, 5),
                        _tick(s, "09:31", 11.0, 200.0, 20)]
            else:
                out += [_tick(s, "09:30", 5.0, 1000.0, 7)]
        return out

    td = TdxData(cache_dir=None)
    td.get_ticks = fake_ticks
    st = await td.get_index_trade_stats(
        "000016", members=["sh600000", "sz000001"])

    assert st.index == "sh000016"
    assert st.member_count == 2
    assert st.active_members == 2
    assert st.trade_count_members == 2
    assert st.trade_count_available is True
    assert st.failed_members == 0

    m = {x.time: x for x in st.minutes}
    assert sorted(m) == ["09:30", "09:31"]
    assert m["09:30"].volume == 1150.0            # 100 + 50 + 1000
    assert m["09:30"].trade_count == 22          # 10 + 5 + 7
    assert m["09:31"].volume == 200.0
    # amount = Σ(price × 手 × 100)
    assert m["09:30"].amount == pytest.approx(
        10.0 * 100 * 100 + 10.0 * 50 * 100 + 5.0 * 1000 * 100)
    assert st.total_volume == 1350.0
    assert st.total_trade_count == 42
    assert st.total_amount == pytest.approx(
        10.0 * 100 * 100 + 10.0 * 50 * 100 + 5.0 * 1000 * 100 + 11.0 * 200 * 100)
    assert st.cum_trade_count() == [("09:30", 22), ("09:31", 42)]


@pytest.mark.asyncio
async def test_get_index_trade_stats_bj_has_no_trade_count():
    """北交所成分返回 trade_count=0 → trade_count_available False。"""
    async def fake_ticks(symbols, date=None, concurrency=6, **kw):
        return [_tick("sh600000", "09:30", 10.0, 100.0, 0)]

    td = TdxData(cache_dir=None)
    td.get_ticks = fake_ticks
    st = await td.get_index_trade_stats("899050", members=["sh600000"])
    assert st.trade_count_members == 0
    assert st.trade_count_available is False
    assert st.total_volume == 100.0


@pytest.mark.asyncio
async def test_get_index_trade_stats_retries_per_symbol_on_batch_failure():
    """整批失败 → 退化为逐只重试，只有真正失败的计入 failed_members。"""
    async def fake_ticks(symbols, date=None, concurrency=6, **kw):
        single = isinstance(symbols, str)
        if not single:
            raise ConnectionError("批量炸了")
        if symbols == "sh600004":
            raise ConnectionError("这只也炸了")
        return [_tick(symbols, "09:30", 10.0, 100.0, 3)]

    td = TdxData(cache_dir=None)
    td.get_ticks = fake_ticks
    st = await td.get_index_trade_stats(
        "000016", members=["sh600000", "sh600004", "sh600006"])
    assert st.failed_members == 1
    assert st.active_members == 2
    assert st.total_trade_count == 6


@pytest.mark.asyncio
async def test_get_index_trade_stats_raises_when_not_skip_errors():
    async def fake_ticks(symbols, date=None, concurrency=6, **kw):
        raise ConnectionError("boom")

    td = TdxData(cache_dir=None)
    td.get_ticks = fake_ticks
    with pytest.raises(ConnectionError):
        await td.get_index_trade_stats(
            "000016", members=["sh600000"], skip_errors=False)


@pytest.mark.asyncio
async def test_get_index_trade_stats_progress_callback():
    seen: list[tuple[int, int]] = []

    async def fake_ticks(symbols, date=None, concurrency=6, **kw):
        return [_tick(s, "09:30", 10.0, 1.0, 1) for s in symbols]

    td = TdxData(cache_dir=None)
    td.get_ticks = fake_ticks
    st = await td.get_index_trade_stats(
        "000016", members=[f"sh60000{i}" for i in range(5)],
        batch_size=2, on_progress=lambda d, t: seen.append((d, t)))
    assert seen == [(2, 5), (4, 5), (5, 5)]
    assert st.member_count == 5


@pytest.mark.asyncio
async def test_get_index_trade_stats_fetches_members_when_absent():
    asked: list[str] = []

    async def fake_members(code):
        asked.append(code)
        return [SimpleNamespace(symbol="sh600000")]

    async def fake_ticks(symbols, date=None, concurrency=6, **kw):
        return [_tick(s, "09:30", 10.0, 1.0, 1) for s in symbols]

    td = TdxData(cache_dir=None)
    td.get_index_members = fake_members
    td.get_ticks = fake_ticks
    await td.get_index_trade_stats("sh000300")
    assert asked == ["000300"]


@pytest.mark.asyncio
async def test_get_index_trade_stats_rejects_empty_members():
    td = TdxData(cache_dir=None)

    async def fake_members(code):
        return []

    td.get_index_members = fake_members
    with pytest.raises(ValueError, match="没有取到成分股"):
        await td.get_index_trade_stats("000300")


@pytest.mark.asyncio
async def test_get_index_trade_stats_multi_reuses_members():
    member_calls: list[str] = []
    tick_dates: list[int | None] = []

    async def fake_members(code):
        member_calls.append(code)
        return [SimpleNamespace(symbol="sh600000")]

    async def fake_ticks(symbols, date=None, concurrency=6, **kw):
        tick_dates.append(date)
        return [_tick(s, "09:30", 10.0, 1.0, 2) for s in symbols]

    td = TdxData(cache_dir=None)
    td.get_index_members = fake_members
    td.get_ticks = fake_ticks
    out = await td.get_index_trade_stats_multi(
        "000300", [20261020, 20261021, None])
    assert len(out) == 3
    assert member_calls == ["000300"]           # 成分只取一次
    assert tick_dates == [20261020, 20261021, None]
    assert all(s.total_trade_count == 2 for s in out)
