"""DataFrame 转换层（0.7.0）：把模型列表转成 pandas / polars 的 DataFrame。

为什么单独一层而不是改各方法的返回：

- **零运行时依赖**是项目的硬约束（``dependencies = []``）。pandas / polars 只在
  真正用到 ``TdxData(df=...)`` 时才导入，没装就走不到这里。
- 转换口径（哪些列留、时间怎么摆、嵌套字段怎么展开）只写一份，40 个返回表格型
  数据的方法共用，不会各自漂移。

三条口径约定（0.7.0 定稿）：

1. **时间列替换原始列**：K线/分时的 ``year/month/day/hour/minute`` 不再单列输出，
   统一给一列 ``datetime``（pandas 为 ``datetime64[ns]``，polars 为 ``Datetime('us')``），
   另带 ``trading_date``（``YYYY-MM-DD`` 字符串，美股跨日口径见模型注释）。
2. **嵌套字段定型**：``SecurityQuote.bid/ask`` 展开成 ``bid1_price``…``ask5_vol`` 共
   10 列（缺失为 ``None``）；``fields`` 这类不定长字典序列化成 JSON 字符串列，
   空字典给 ``None`` 而不是 ``"{}"``；``TdxBlock.codes`` 保持列表。
3. **嵌套调用不二次转换**：``get_kline_batch`` 这类方法内部还会调 ``get_bars``，
   用 :data:`_FRAME_DEPTH`（``ContextVar``，按 task 隔离）标记层次，内层一律返回
   原始 ``list``，只有最外层那次才转 DataFrame。
"""
from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from datetime import datetime as _dt
from enum import IntEnum
from operator import attrgetter
from typing import Any, NamedTuple

from . import models as _models
from .models import (
    IndexTradeStats,
    IndicatorSet,
    MinuteBar,
    MinuteTradeStat,
    SecurityBar,
    SecurityQuote,
    TransactionRecord,
    XdxrRecord,
)

__all__ = ["BACKENDS", "FrameBackendError", "framed", "to_frame"]

BACKENDS: tuple[str, ...] = ("polars", "pandas")
"""支持的后端名，也是 ``TdxData(df=...)`` 的合法取值。"""


class FrameBackendError(ImportError):
    """指定的 DataFrame 后端未安装。

    继承 :class:`ImportError` 是刻意的：捕获姿势和缺包一致，
    且 ``e.name`` 就是缺的那个包名。
    """

    def __init__(self, backend: str) -> None:
        self.backend = backend
        super().__init__(
            f"未安装 {backend}，无法输出 DataFrame。请执行：pip install pytdxdata[{backend}]",
            name=backend,
        )


# 转换层次计数：>0 表示当前正处于某次公开方法的执行过程中（内层），此时不转换。
_FRAME_DEPTH: ContextVar[int] = ContextVar("pytdxdata_frame_depth", default=0)


# ---------------------------------------------------------------- 后端


class _Backend:
    """后端适配：只封装「造表」和「拼表」两个动作。"""

    name: str = ""

    def frame(self, cols: dict[str, list]) -> Any:  # pragma: no cover - 抽象
        raise NotImplementedError

    def concat(self, frames: Sequence[Any]) -> Any:  # pragma: no cover - 抽象
        raise NotImplementedError

    def empty(self, cols: Sequence[str]) -> Any:
        return self.frame({name: [] for name in cols})


class _PolarsBackend(_Backend):
    name = "polars"

    def __init__(self) -> None:
        try:
            import polars as _pl
        except ImportError as exc:  # pragma: no cover - 依赖环境
            raise FrameBackendError("polars") from exc
        self._pl = _pl

    def frame(self, cols: dict[str, list]) -> Any:
        # 全空时必须显式给列名，否则 polars 会造出 0 列的表
        return self._pl.DataFrame(cols)

    def concat(self, frames: Sequence[Any]) -> Any:
        frames = [f for f in frames if f is not None and f.height > 0]
        if not frames:
            return None
        if len(frames) == 1:
            return frames[0]
        # vertical_relaxed：批量接口各标的分块建表，dtype 可能略有差异（如 Null 列）
        return self._pl.concat(frames, how="vertical_relaxed")


class _PandasBackend(_Backend):
    name = "pandas"

    def __init__(self) -> None:
        try:
            import pandas as _pd
        except ImportError as exc:  # pragma: no cover - 依赖环境
            raise FrameBackendError("pandas") from exc
        self._pd = _pd

    def frame(self, cols: dict[str, list]) -> Any:
        return self._pd.DataFrame(cols)

    def concat(self, frames: Sequence[Any]) -> Any:
        frames = [f for f in frames if f is not None and len(f) > 0]
        if not frames:
            return None
        if len(frames) == 1:
            return frames[0]
        return self._pd.concat(frames, ignore_index=True)


_BACKEND_CACHE: dict[str, _Backend] = {}


def get_backend(name: str) -> _Backend:
    """取后端实例（进程内缓存，重复取不会重复 import）。

    :raises ValueError: 名字不在 :data:`BACKENDS` 里。
    :raises FrameBackendError: 对应的包没装。
    """
    if name not in BACKENDS:
        raise ValueError(f"未知的 DataFrame 后端 {name!r}，可选：{', '.join(BACKENDS)}")
    be = _BACKEND_CACHE.get(name)
    if be is None:
        be = _PolarsBackend() if name == "polars" else _PandasBackend()
        _BACKEND_CACHE[name] = be
    return be


# ---------------------------------------------------------------- 列计划


def _as_int(value: Any) -> Any:
    """``IntEnum`` 转纯 ``int``：polars 对 IntEnum 的类型推断不可靠，转掉最稳。"""
    if isinstance(value, IntEnum):
        return int(value)
    return value


def _json_or_none(value: Any) -> str | None:
    """不定长字典序列化成 JSON 列；空字典给 ``None``（别让用户看到一列 ``"{}"``）。"""
    if not value:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_getter(name: str) -> Callable[[Any], Any]:
    def _get(row: Any) -> Any:
        return _json_or_none(getattr(row, name))

    return _get


def _level_getter(side: str, index: int, pos: int) -> Callable[[Any], Any]:
    """``bid``/``ask`` 第 ``index`` 档的价格(pos=0)或数量(pos=1)。"""

    def _get(row: Any) -> Any:
        levels = getattr(row, side)
        if len(levels) <= index:
            return None
        return levels[index][pos]

    return _get


def _moment(row: SecurityBar | MinuteBar) -> _dt:
    return row.datetime


def _trading_date(row: SecurityBar | MinuteBar) -> str:
    return row.trading_date


def _xdxr_day(row: XdxrRecord) -> _dt:
    # 除权日是「日」不是「时刻」，但 datetime64 / Datetime 才是可排序、可比较的列类型。
    # 全项目时间口径都是「不带 tzinfo 的北京时间」，这里保持一致。
    return _dt(row.year, row.month, row.day)  # noqa: DTZ001


#: 模型 -> 需要丢弃的原始字段（时间拆解字段，由增强列替代）
_DROPS: dict[type, tuple[str, ...]] = {
    SecurityBar: ("year", "month", "day", "hour", "minute"),
    MinuteBar: ("hour", "minute", "date", "day_offset"),
    XdxrRecord: ("year", "month", "day"),
    SecurityQuote: ("bid", "ask"),
}

#: 模型 -> 在「第一个被丢弃字段的位置」插入的增强列
_EXTRAS: dict[type, tuple[tuple[str, Callable[[Any], Any]], ...]] = {
    SecurityBar: (("datetime", _moment), ("trading_date", _trading_date)),
    MinuteBar: (("datetime", _moment), ("trading_date", _trading_date)),
    XdxrRecord: (("date", _xdxr_day),),
    SecurityQuote: tuple(
        (f"{side}{level + 1}_{part}", _level_getter(side, level, pos))
        for side in ("bid", "ask")
        for level in range(5)
        for pos, part in ((0, "price"), (1, "vol"))
    ),
}

#: 模型 -> 需要 JSON 化的字典字段（按类区分，避免误伤 ``FinancialRecord.fields: list[float]``）
_JSON_FIELDS: dict[type, frozenset[str]] = {
    SecurityQuote: frozenset({"fields"}),
    TransactionRecord: frozenset({"fields"}),
    MinuteBar: frozenset({"fields"}),
}

#: 交易日列的缓存键（时间字段里参与 ``trading_date`` 计算的那些）。
#:
#: ``trading_date`` 是纯函数：A股/港股/期货就是自然日，美股凌晨段回退一天。
#: 但它每算一次都要格式化三个整数（美股还要减一天），240k 行里往往只有
#: **1~2 个不同的键** —— 按「除 minute 外的时间字段」缓存后，整列从 218ms 降到 33ms。
#: 键里保留 ``hour`` 是为了一箭双雕：既覆盖美股 ``hour < 12`` 的跨日判断，
#: 又不必把 ``_US_DAY_SPLIT_HOUR`` 的取值抄过来（切换点改了本文件也不用跟着改）。
_TD_KEYS: dict[type, tuple[str, ...]] = {
    SecurityBar: ("year", "month", "day", "hour"),
    MinuteBar: ("date", "day_offset", "hour"),
}

#: 交易日列名（走缓存路径，不按行取属性）
_TD_COL = "trading_date"

#: 增强列里可以直接用 ``attrgetter`` 取的（属性本身即得值，不需要额外加工）
_PLAIN_EXTRAS = frozenset({"datetime"})


class _ColPlan(NamedTuple):
    """某模型的输出列计划（构造一次，按类缓存）。

    分成三类是性能决定的：逐行调 Python 闭包比 C 层 ``attrgetter`` 慢一倍以上，
    而 ``trading_date`` 这种「同一批里值大量重复」的列更是值得单独缓存。
    """

    order: tuple[str, ...]                                  # 输出列顺序
    plain: tuple[str, ...]                                  # 直接取属性即可的列名
    wrapped: tuple[tuple[str, Callable[[Any], Any]], ...]   # 需要加工的列（JSON / 五档 / 除权日）
    td_name: str | None                                     # 交易日列名
    td_key: Callable[[Any], Any] | None                     # 交易日缓存键


_PLAN_CACHE: dict[type, _ColPlan] = {}


def _plan_for(cls: type) -> _ColPlan:
    """构造（并缓存）某模型的输出列计划。

    查找顺序按 MRO 走：``IndexBar`` 没有自己的计划，但它是 ``SecurityBar`` 的子类，
    直接复用父类计划即可（字段完全相同）。
    """
    cached = _PLAN_CACHE.get(cls)
    if cached is not None:
        return cached

    drops: tuple[str, ...] = ()
    extras: tuple[tuple[str, Callable[[Any], Any]], ...] = ()
    json_fields: frozenset[str] = frozenset()
    td_fields: tuple[str, ...] = ()
    for klass in cls.__mro__:
        if not drops and klass in _DROPS:
            drops = _DROPS[klass]
        if not extras and klass in _EXTRAS:
            extras = _EXTRAS[klass]
        if not json_fields and klass in _JSON_FIELDS:
            json_fields = _JSON_FIELDS[klass]
        if not td_fields and klass in _TD_KEYS:
            td_fields = _TD_KEYS[klass]

    import dataclasses

    order: list[str] = []
    plain: list[str] = []
    wrapped: list[tuple[str, Callable[[Any], Any]]] = []
    td_name: str | None = None

    def add(name: str, getter: Callable[[Any], Any] | None = None) -> None:
        nonlocal td_name
        order.append(name)
        if name == _TD_COL:
            td_name = name            # 交易日列走缓存，不看 getter
        elif getter is None or name in _PLAIN_EXTRAS:
            plain.append(name)
        else:
            wrapped.append((name, getter))

    inserted = False
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name in drops:
            if not inserted:
                for name, getter in extras:
                    add(name, getter)
                inserted = True
            continue
        add(f.name, _json_getter(f.name) if f.name in json_fields else None)
    if not inserted:
        # 没有丢弃字段的模型（如 SecurityInfo）：增强列直接追加到末尾
        for name, getter in extras:
            add(name, getter)

    plan = _ColPlan(
        order=tuple(order),
        plain=tuple(plain),
        wrapped=tuple(wrapped),
        td_name=td_name,
        td_key=attrgetter(*td_fields) if td_fields else None,
    )
    _PLAN_CACHE[cls] = plan
    return plan


def _trading_dates(key_of: Callable[[Any], Any], rows: Sequence[Any]) -> list[str]:
    """按缓存键批量算交易日串（同一批里绝大多数行是重复键）。"""
    cache: dict[Any, str] = {}
    out: list[str] = []
    ap = out.append
    for row in rows:
        key = key_of(row)
        value = cache.get(key)
        if value is None:            # trading_date 永远是字符串，不会与 None 混淆
            value = cache[key] = row.trading_date
        ap(value)
    return out


def _columns(cls: type, rows: Sequence[Any]) -> dict[str, list]:
    """列式采集：逐列一次遍历，比逐行 ``asdict()`` 快得多。

    - 普通列用 ``list(map(attrgetter(name), rows))``：取值发生在 C 层，没有 Python 帧；
    - ``trading_date`` 按时间字段缓存（240k 行常只有 1~2 个键）；
    - 其余列（JSON / 五档 / 除权日）才走逐行回调；
    - ``IntEnum`` 归一化放在最后**按列**做一次，而不是每个单元格都判一次。
    """
    plan = _plan_for(cls)
    if not rows:
        return {name: [] for name in plan.order}

    cols: dict[str, list] = {}
    for name in plan.plain:
        cols[name] = list(map(attrgetter(name), rows))
    if plan.td_name is not None and plan.td_key is not None:
        cols[plan.td_name] = _trading_dates(plan.td_key, rows)
    for name, get in plan.wrapped:
        cols[name] = [get(row) for row in rows]

    # polars 对 IntEnum 的类型推断不可靠（会退化成 Object 列），统一转纯 int。
    # 只在首元素就是 IntEnum 时才整列转，普通 int 列一次都不多花。
    for name, col in cols.items():
        if col and isinstance(col[0], IntEnum):
            cols[name] = [_as_int(value) for value in col]

    return {name: cols[name] for name in plan.order}


def _columns_of_dicts(rows: Sequence[dict]) -> dict[str, list]:
    """``list[dict]``（板块排行这类字段不固定的返回）按首次出现顺序并集列名。"""
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return {key: [row.get(key) for row in rows] for key in keys}


# ---------------------------------------------------------------- 容器分发


def _model_by_name(name: str | None) -> type | None:
    if not name:
        return None
    obj = getattr(_models, name, None)
    return obj if isinstance(obj, type) else None


def _resolve_model(data: Any, model: str | None) -> type | None:
    """推断元素类型：非空列表看首元素；空列表只能靠调用方给的类型提示。"""
    if isinstance(data, list) and data:
        return type(data[0])
    return _model_by_name(model)


def _frames_of_rows(be: _Backend, cls: type | None, rows: Sequence[Any]) -> Any:
    if cls is not None and rows and hasattr(cls, "__dataclass_fields__"):
        return be.frame(_columns(cls, rows))
    if rows and isinstance(rows[0], dict):
        return be.frame(_columns_of_dicts(rows))
    return be.empty(())


def _from_indicator_sets(be: _Backend, items: Sequence[IndicatorSet]) -> Any:
    frames = []
    for item in items:
        cols = _columns(SecurityBar, item.bars)
        size = len(item.bars)
        if item.symbol:
            cols["symbol"] = [item.symbol] * size
        for name, series in item.indicators.items():
            values = list(series[:size])
            if len(values) < size:
                values.extend([None] * (size - len(values)))
            cols[name] = values
        frames.append(be.frame(cols))
    merged = be.concat(frames)
    return merged if merged is not None else be.frame(_columns(SecurityBar, []))


def _from_index_trade_stats(be: _Backend, items: Sequence[IndexTradeStats]) -> Any:
    frames = []
    for stat in items:
        cols = _columns(MinuteTradeStat, stat.minutes)
        size = len(stat.minutes)
        # 元信息补成常量列：一张表就能看出是哪个指数、哪一天
        cols = {"index": [stat.index] * size, "date": [stat.date] * size, **cols}
        frames.append(be.frame(cols))
    merged = be.concat(frames)
    return merged if merged is not None else be.frame(_columns(MinuteTradeStat, []))


def to_frame(data: Any, *, backend: str = "polars", model: str | None = None) -> Any:
    """把模型返回值转成 DataFrame。

    :param data: ``list[dataclass]`` / ``list[dict]`` / ``dict[str, list[dataclass]]``
        / ``list[IndicatorSet]`` / ``IndexTradeStats`` / ``list[IndexTradeStats]``。
    :param backend: ``"polars"`` 或 ``"pandas"``。
    :param model: 元素类型的名字（如 ``"SecurityBar"``）；只在 ``data`` 为空、
        无法从元素推断类型时使用，由 :func:`framed` 从返回注解里自动填。
    """
    be = get_backend(backend)

    if isinstance(data, IndexTradeStats):
        return _from_index_trade_stats(be, [data])
    if isinstance(data, IndicatorSet):
        return _from_indicator_sets(be, [data])

    if isinstance(data, dict):
        # 批量接口：{标的: [行]} → 拼成一张表（symbol/code 列本来就在行里）
        frames = []
        for rows in data.values():
            if isinstance(rows, list) and rows:
                frames.append(_frames_of_rows(be, type(rows[0]), rows))
        merged = be.concat(frames)
        if merged is not None:
            return merged
        cls = _model_by_name(model)
        return be.empty(_plan_for(cls).order if cls else ())

    if isinstance(data, list):
        if data and isinstance(data[0], IndexTradeStats):
            return _from_index_trade_stats(be, data)
        if data and isinstance(data[0], IndicatorSet):
            return _from_indicator_sets(be, data)
        cls = _resolve_model(data, model)
        if data and isinstance(data[0], dict):
            return _frames_of_rows(be, None, data)
        if cls is not None and data and hasattr(cls, "__dataclass_fields__"):
            return _frames_of_rows(be, cls, data)
        if data:
            # 元素既不是 dataclass 也不是 dict（如 list[float]）——不该走到这里
            raise TypeError(f"无法转换为 DataFrame 的列表元素：{type(data[0])!r}")
        return (be.frame({name: [] for name in _plan_for(cls).order})
                if cls else be.empty(()))

    raise TypeError(f"无法转换为 DataFrame 的返回值类型：{type(data)!r}")


# ---------------------------------------------------------------- 接线


_RETURN_MODEL = re.compile(r"list\[(\w+)\]")


def framed(method: Callable[..., Any]) -> Callable[..., Any]:
    """给 ``TdxData`` 的表格型方法套一层「按需转 DataFrame」。

    - ``self._df_backend is None``（默认）时零开销，原样返回 ``list[dataclass]``。
    - 处于某次公开方法执行过程中（:data:`_FRAME_DEPTH` > 0）时不转换，
      保证批量/别名方法内部拿到的仍是原始 ``list``。
    """
    model: str | None = None
    annotation = getattr(method, "__annotations__", {}).get("return")
    if isinstance(annotation, str):
        matched = _RETURN_MODEL.search(annotation)
        if matched:
            model = matched.group(1)

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        backend = getattr(self, "_df_backend", None)
        if backend is None or _FRAME_DEPTH.get() > 0:
            return await method(self, *args, **kwargs)

        token = _FRAME_DEPTH.set(1)
        try:
            result = await method(self, *args, **kwargs)
        finally:
            _FRAME_DEPTH.reset(token)
        return to_frame(result, backend=backend, model=model)

    return wrapper
