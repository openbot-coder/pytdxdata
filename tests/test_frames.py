"""DataFrame 转换层测试（0.7.0）。

覆盖两条独立路径：

1. **转换口径**：直接调 :func:`to_frame`，喂合成模型对象，断言列名/列序/取值/dtype。
   不碰网络，双后端各跑一遍。
2. **接线**：``TdxData(df=...)`` 的取值校验、装饰器的「内层不二次转换」语义。
"""
from __future__ import annotations

import math
import sys

import pytest

from pytdxdata import TdxData
from pytdxdata import frames as _frames
from pytdxdata.frames import FrameBackendError, framed, get_backend, to_frame
from pytdxdata.models import (
    ExMarket,
    IndexBar,
    IndexTradeStats,
    IndicatorSet,
    Market,
    MinuteBar,
    MinuteTradeStat,
    SecurityBar,
    SecurityQuote,
    TdxBlock,
    XdxrRecord,
)

BACKENDS = ("polars", "pandas")


# ---------------------------------------------------------------- 工具


def bar(**kw) -> SecurityBar:
    base = {
        "market": int(Market.SH), "code": "600000", "open": 10.0, "high": 11.0,
        "low": 9.5, "close": 10.5, "vol": 1000.0, "amount": 10500.0,
        "year": 2026, "month": 10, "day": 27, "hour": 0, "minute": 0,
        "symbol": "sh600000",
    }
    base.update(kw)
    return SecurityBar(**base)


def rows(df, backend: str) -> int:
    return len(df) if backend == "pandas" else df.height


def cols(df, backend: str) -> list[str]:
    return list(df.columns)


def cell(df, backend: str, name: str, index: int = 0):
    if backend == "pandas":
        return df[name].iloc[index]
    # polars 2.x：Series[0] 会返回 Series 而不是标量，统一走 to_list()
    return df.get_column(name).to_list()[index]


def missing(value) -> bool:
    """pandas 的数值列会把 None 变成 NaN，两者都算「缺」。"""
    return value is None or (isinstance(value, float) and math.isnan(value))


# ---------------------------------------------------------------- 后端


def test_backend_instances_are_cached():
    assert get_backend("polars") is get_backend("polars")
    assert get_backend("pandas") is get_backend("pandas")


def test_unknown_backend_raises_value_error():
    with pytest.raises(ValueError, match="未知的 DataFrame 后端"):
        get_backend("numpy")


def test_missing_backend_raises_with_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "polars", None)  # 让 import polars 抛 ImportError
    monkeypatch.delitem(_frames._BACKEND_CACHE, "polars", raising=False)
    with pytest.raises(FrameBackendError) as excinfo:
        get_backend("polars")
    assert isinstance(excinfo.value, ImportError)
    assert excinfo.value.name == "polars"
    assert "pip install pytdxdata[polars]" in str(excinfo.value)
    monkeypatch.delitem(_frames._BACKEND_CACHE, "polars", raising=False)


# ---------------------------------------------------------------- 转换口径


@pytest.mark.parametrize("backend", BACKENDS)
def test_bar_replaces_split_time_with_datetime(backend):
    df = to_frame([bar(), bar(minute=30)], backend=backend)
    assert cols(df, backend) == [
        "market", "code", "open", "high", "low", "close", "vol", "amount",
        "datetime", "trading_date", "symbol", "settle",
    ]
    assert rows(df, backend) == 2
    assert cell(df, backend, "market") == 1
    assert cell(df, backend, "trading_date") == "2026-10-27"
    stamp = cell(df, backend, "datetime", 1)
    assert stamp.year == 2026 and stamp.hour == 0 and stamp.minute == 30


@pytest.mark.parametrize("backend", BACKENDS)
def test_int_enum_column_is_plain_int(backend):
    df = to_frame([bar(market=Market.SZ)], backend=backend)
    value = cell(df, backend, "market")
    assert value == 0
    assert not isinstance(value, Market)


@pytest.mark.parametrize("backend", BACKENDS)
def test_empty_list_falls_back_to_annotation_hint(backend):
    df = to_frame([], backend=backend, model="SecurityBar")
    assert rows(df, backend) == 0
    assert cols(df, backend) == [
        "market", "code", "open", "high", "low", "close", "vol", "amount",
        "datetime", "trading_date", "symbol", "settle",
    ]


@pytest.mark.parametrize("backend", BACKENDS)
def test_quote_expands_bid_ask_levels(backend):
    quote = SecurityQuote(
        market=1, code="600000", price=10.5, pre_close=10.0, open=10.1, high=10.6,
        low=9.9, vol=100.0, amount=1000.0, s_vol=40.0, b_vol=60.0,
        bid=[(10.49, 100.0), (10.48, 200.0)], ask=[(10.51, 300.0)],
        symbol="sh600000", fields={"0x7": 1.5},
    )
    df = to_frame([quote], backend=backend)
    out = cols(df, backend)
    assert "bid1_price" in out and "ask5_vol" in out
    assert "bid" not in out and "ask" not in out
    assert cell(df, backend, "bid2_vol") == 200.0
    assert missing(cell(df, backend, "bid3_price"))       # 只有两档，缺档给 None
    assert cell(df, backend, "fields") == '{"0x7":1.5}'


@pytest.mark.parametrize("backend", BACKENDS)
def test_empty_dict_field_becomes_none(backend):
    df = to_frame([SecurityQuote(
        market=1, code="600000", price=1.0, pre_close=1.0, open=1.0, high=1.0,
        low=1.0, vol=1.0, amount=1.0, s_vol=0.0, b_vol=0.0,
    )], backend=backend)
    assert missing(cell(df, backend, "fields"))


@pytest.mark.parametrize("backend", BACKENDS)
def test_minute_uses_real_clock(backend):
    late = MinuteBar(price=1.0, vol=10.0, avg_price=1.0, hour=27, minute=59,
                     symbol="usAAPL", market=74, code="AAPL", date=20261026)
    df = to_frame([late], backend=backend)
    assert cols(df, backend) == [
        "price", "vol", "avg_price", "datetime", "trading_date",
        "symbol", "market", "code", "fields",
    ]
    stamp = cell(df, backend, "datetime")
    assert (stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute) == (
        2026, 10, 27, 3, 59,
    )
    assert cell(df, backend, "trading_date") == "2026-10-26"


@pytest.mark.parametrize("backend", BACKENDS)
def test_xdxr_date_column(backend):
    row = XdxrRecord(market=1, code="600000", year=2026, month=6, day=12,
                     category=1, name="分红", fenhong=0.5, symbol="sh600000")
    df = to_frame([row], backend=backend)
    assert cols(df, backend)[:3] == ["market", "code", "date"]
    stamp = cell(df, backend, "date")
    assert (stamp.year, stamp.month, stamp.day) == (2026, 6, 12)


@pytest.mark.parametrize("backend", BACKENDS)
def test_tdx_block_keeps_codes_list(backend):
    df = to_frame([TdxBlock(name="银行", category=0, count=2, codes=["600000", "600036"])],
                  backend=backend)
    assert cols(df, backend) == ["name", "category", "count", "codes"]
    assert cell(df, backend, "codes") == ["600000", "600036"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_indicator_set_merges_bars_and_indicators(backend):
    item = IndicatorSet(
        symbol="sh600000",
        bars=[bar(), bar(minute=1), bar(minute=2)],
        indicators={"ma5": [1.0, 2.0, 3.0], "boll": [None, None, 9.0]},
    )
    df = to_frame([item], backend=backend)
    assert rows(df, backend) == 3
    assert cell(df, backend, "symbol") == "sh600000"
    assert cell(df, backend, "ma5", 2) == 3.0
    assert missing(cell(df, backend, "boll"))


@pytest.mark.parametrize("backend", BACKENDS)
def test_indicator_series_shorter_than_bars_is_padded(backend):
    item = IndicatorSet(symbol="sh600000", bars=[bar(), bar(minute=1)],
                        indicators={"ma5": [7.0]})
    df = to_frame([item], backend=backend)
    assert rows(df, backend) == 2
    assert cell(df, backend, "ma5") == 7.0
    assert missing(cell(df, backend, "ma5", 1))


@pytest.mark.parametrize("backend", BACKENDS)
def test_index_trade_stats_single_and_multi(backend):
    stat = IndexTradeStats(
        index="sh000300", date=20261027, member_count=300, active_members=299,
        trade_count_members=299,
        minutes=[MinuteTradeStat(time="09:31", volume=10.0, trade_count=5, amount=1000.0)],
    )
    single = to_frame(stat, backend=backend)
    assert cols(single, backend) == ["index", "date", "time", "volume", "trade_count", "amount"]
    assert cell(single, backend, "index") == "sh000300"

    multi = to_frame([stat, stat], backend=backend)
    assert rows(multi, backend) == 2


@pytest.mark.parametrize("backend", BACKENDS)
def test_batch_dict_is_concatenated(backend):
    data = {
        "600000": [bar(code="600000"), bar(code="600000", minute=1)],
        "600036": [bar(code="600036", symbol="sh600036")],
    }
    df = to_frame(data, backend=backend)
    assert rows(df, backend) == 3
    assert cols(df, backend)[:2] == ["market", "code"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_batch_dict_all_empty_returns_typed_empty(backend):
    df = to_frame({"600000": [], "600036": []}, backend=backend, model="SecurityBar")
    assert rows(df, backend) == 0
    assert "datetime" in cols(df, backend)


@pytest.mark.parametrize("backend", BACKENDS)
def test_list_of_dicts_uses_key_union(backend):
    df = to_frame([{"name": "银行", "rise": 1.2}, {"name": "券商", "amount": 5.0}],
                  backend=backend)
    assert cols(df, backend) == ["name", "rise", "amount"]
    assert missing(cell(df, backend, "amount", 0))


def test_unsupported_payload_raises_type_error():
    with pytest.raises(TypeError, match="无法转换为 DataFrame 的返回值类型"):
        to_frame(42, backend="polars")
    with pytest.raises(TypeError, match="无法转换为 DataFrame 的列表元素"):
        to_frame([1.0, 2.0], backend="polars")


def test_subclass_reuses_parent_plan():
    df = to_frame([IndexBar(market=1, code="000001", open=1.0, high=1.0, low=1.0,
                            close=1.0, vol=1.0, amount=1.0, year=2026, month=1, day=5)],
                  backend="polars")
    assert "datetime" in df.columns
    assert "year" not in df.columns


# ---------------------------------------------------------------- 接线


def test_tdxdata_default_keeps_legacy_behaviour():
    td = TdxData()
    assert td._df_backend is None


def test_tdxdata_rejects_unknown_backend():
    with pytest.raises(ValueError, match="未知的 DataFrame 后端"):
        TdxData(df="numpy")


def test_tdxdata_accepts_supported_backend():
    td = TdxData(df="polars")
    assert td._df_backend == "polars"


class _Source:
    """最小替身：只验装饰器语义，不碰网络。"""

    def __init__(self, backend: str | None) -> None:
        self._df_backend = backend

    @framed
    async def single(self) -> list[SecurityBar]:
        return [bar()]

    @framed
    async def nested(self) -> dict[str, list[SecurityBar]]:
        got = await self.single()          # 内层必须是原始 list
        assert isinstance(got, list), f"内层被提前转换成了 {type(got)!r}"
        return {"600000": got}


async def test_decorator_passthrough_when_no_backend():
    src = _Source(None)
    assert isinstance(await src.single(), list)


async def test_decorator_converts_outermost_only():
    src = _Source("polars")
    out = await src.nested()
    assert cols(out, "polars")[0] == "market"
    assert rows(out, "polars") == 1


def test_model_name_resolution():
    # 注解是 dict[str, list[SecurityBar]] 时正则也能取到 SecurityBar；
    # 但 "dict" 这类内建名在 models 里找不到，必须返回 None 而不是内建类型
    assert _frames._model_by_name("dict") is None
    assert _frames._model_by_name("SecurityBar") is SecurityBar


def test_datetime_dtype_is_datetime_like():
    pl = pytest.importorskip("polars")
    df = to_frame([bar()], backend="polars")
    assert isinstance(df.schema["datetime"], pl.Datetime)


# ---------------------------------------------------------------- 交易日列缓存


@pytest.mark.parametrize("backend", BACKENDS)
def test_trading_date_multi_key_batch_matches_model(backend):
    """一批里混着 A股与美股、跨 0~23 点，逐行都要等于模型自己的 ``trading_date``。

    ``trading_date`` 是按「时间字段」缓存算出来的（240k 行常只有 1~2 个键），
    这里专门造出「多键同批」的极端情形，防止缓存键漏掉参与计算的因素。

    注意：美股的跨日处理在 ``SecurityBar.__post_init__`` 里就做完了（凌晨段
    的年月日会被推进一天），所以 ``trading_date`` 对每个 bar 只是自然日，
    真正会分叉的是 ``datetime`` —— 两者都要逐行核对。
    """
    batch = []
    for market in (int(Market.SH), int(ExMarket.US_STOCK)):
        for hour in range(24):
            batch.append(bar(market=market, hour=hour, minute=15))
    df = to_frame(batch, backend=backend)
    got_td = [cell(df, backend, "trading_date", i) for i in range(rows(df, backend))]
    got_dt = [cell(df, backend, "datetime", i) for i in range(rows(df, backend))]
    assert got_td == [b.trading_date for b in batch]
    assert got_dt == [b.datetime for b in batch]
    # 美股凌晨段的 datetime 确实落在下一天（否则这条测试等于没测）
    us = [b for b in batch if b.market == int(ExMarket.US_STOCK)]
    assert {b.datetime.date().isoformat() for b in us} == {"2026-10-27", "2026-10-28"}


@pytest.mark.parametrize("backend", BACKENDS)
def test_minute_trading_date_cache_matches_model(backend):
    """分时的 ``day_offset`` 也参与交易日计算，缓存键不能漏。"""
    batch = [
        MinuteBar(price=10.0, vol=1.0, hour=9, minute=30, date=20261027),
        MinuteBar(price=10.0, vol=1.0, hour=21, minute=30, date=20261027,
                  market=int(ExMarket.US_STOCK)),
        MinuteBar(price=10.0, vol=1.0, hour=3, minute=0, date=20261027,
                  market=int(ExMarket.US_STOCK), day_offset=1),
    ]
    df = to_frame(batch, backend=backend)
    got = [cell(df, backend, "trading_date", i) for i in range(rows(df, backend))]
    assert got == [b.trading_date for b in batch]


@pytest.mark.parametrize("backend", BACKENDS)
def test_every_bar_field_equals_model(backend):
    """逐字段等价：DataFrame 的每个值都必须等于 dataclass 的同名字段。

    这条是取列路径（``attrgetter`` 批量取 + 按列转 Enum）改动的兜底，
    避免「快是快了，但某列悄悄串了值」。
    """
    src = bar(minute=15, settle=3.5)
    df = to_frame([src], backend=backend)
    for name in cols(df, backend):
        want = getattr(src, name)
        assert cell(df, backend, name) == want, name
