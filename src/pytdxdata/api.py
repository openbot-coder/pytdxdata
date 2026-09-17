"""统一对外接口 TdxData。

对外只暴露「**一个数据类型 = 一个方法**」：跨市场（A股/港股/美股/期货/期权）与
跨通道（标准 / MAC / EX）由内部自动选路、自动分批、自动分页，标的统一用字符串
写法（``sz000001`` / ``hk00700`` / ``cffex:IFL0``，裸 6 位数字=沪市指数）。

用法::

    async with TdxData() as td:
        # 一次拿到 A股 + 港股 + 美股 的报价（内部按通道分组并发）
        quotes = await td.get_quotes(["sz000001", "hk00700", "usAAPL"])
        # 日K线（自动判指数、自动复权选路、自动并发分页）
        bars = await td.get_bars(["sz000001", "hk00700"], KlinePeriod.DAY, count=240)
        # 逐笔成交 / 分时
        ticks = await td.get_ticks("sz000001", date=20260811)
        minutes = await td.get_minutes("sz000001")

旧的分裂接口（``get_kline`` / ``get_transaction`` 等 51 个）保留为 deprecated
别名，见文件末尾，1.0 移除。
"""
from __future__ import annotations

import asyncio
import logging
import warnings
from datetime import date as _date, datetime
from pathlib import Path
from typing import Sequence

from .cache.disk import DiskCache
from .cache.key import CacheKey
from .cache.manager import CacheManager
from .cache.memory import MemoryCache
from .codec.payload import pack_payload, unpack_payload
from .config import EX_HANDSHAKES, get_ex_hosts, get_mac_hosts, get_standard_hosts
from .models import (
    Adjust,
    AuctionItem,
    BelongBoard,
    BoardInfo,
    CapitalFlow,
    ExMarket,
    FinanceRecord,
    FinancialFileInfo,
    FinancialRecord,
    IndicatorSet,
    IndustryInfo,
    KlinePeriod,
    Market,
    MemberQuote,
    MinuteBar,
    SecurityBar,
    SecurityInfo,
    SecurityQuote,
    ServerInfo,
    SymbolSnapshot,
    TdxBlock,
    TransactionRecord,
    UnusualItem,
    XdxrRecord,
)
from .pool.ex_pool import ExPool
from .pool.health import HealthEngine
from .pool.pool import DynamicPool
from .protocol.commands import (
    GetFinanceInfoCmd,
    GetIndexBarsCmd,
    GetSecurityBarsCmd,
    GetSecurityCountCmd,
    GetSecurityListCmd,
    GetSecurityQuotesCmd,
    GetXdxrInfoCmd,
    KLINE_PAGE,
    LIST_PAGE,
    MacKlineCmd,
    MacTransactionCmd,
    MAX_QUOTES,
)
from .protocol.commands.ex_ext import (
    GetExHistoryTransactionDataCmd,
    GetExInstrumentCountCmd,
    GetExInstrumentInfoCmd,
    GetExTransactionDataCmd,
    SymbolTickChartCmd,
)
from .protocol.commands.ex_proto import GetExInstrumentBarsCmd
from .protocol.commands.mac_ext import (
    MacAuctionCmd,
    MacBelongBoardCmd,
    MacBoardListCmd,
    MacBoardMembersQuotesCmd,
    MacCapitalFlowCmd,
    MacChartSamplingCmd,
    MacFileDownloadCmd,
    MacFileListCmd,
    MacKlineOffsetCmd,
    MacServerInfoCmd,
    MacSymbolInfoCmd,
    MacSymbolQuotesCmd,
    MacUnusualCmd,
)
from .protocol.commands.mac_tick import MacTickChartCmd
from .protocol.commands.std_ext import (
    GetBlockInfoCmd,
    GetBlockInfoMetaCmd,
    GetCompanyInfoCategoryCmd,
    GetCompanyInfoContentCmd,
    GetReportFileCmd,
    MarketStat,
    compute_price_limits,
)
from .router import route_for
from .scheduler.paginator import PageSpec, Paginator
from .symbols import (
    Symbol,
    format_symbol,
    is_cn_index,
    market_name,
    market_targets,
    parse_market,
    parse_symbol,
    parse_symbols,
)

log = logging.getLogger(__name__)

# 按K线周期预估最大条数上界（用于日期二分查找的初始上界）
# 日K：约30年 × 250天；分钟K：约3年 × 250天 × 对应倍数
_KLINE_MAX_BARS: dict[KlinePeriod, int] = {
    KlinePeriod.MIN_1: 200_000,
    KlinePeriod.MIN_5: 60_000,
    KlinePeriod.MIN_15: 25_000,
    KlinePeriod.MIN_30: 15_000,
    KlinePeriod.MIN_60: 10_000,
    KlinePeriod.DAY: 10_000,
    KlinePeriod.WEEK: 2_000,
    KlinePeriod.MONTH: 500,
}

# 自定义字段报价（0x122B/0x122C）基础字段位：昨收/开/高/低/现价/量/量比/额
_QUOTE_BITS = [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07]
_QUOTE_PAGE = 80          # 0x122B/0x122C 单次上限 80 只
_EX_PAGE = 1000           # EX 商品列表单页
_TICK_PAGE = 1000         # MAC 逐笔单页
_EX_TICK_PAGE = 1800      # 港股逐笔单页
_ALL_PAGES_CAP = 50       # count=None（全量）时的翻页上限

# 港股系市场：逐笔走 EX 专用 16B 协议（0x23FC/0x2406），其余扩展市场走 MAC 0x122F
_HK_MARKETS = frozenset({27, 31, 48, 49, 71, 98})


def _fields_tag(bits: Sequence[int] | None) -> str:
    """字段位集合 -> 缓存键片段。"""
    if not bits:
        return ""
    return ",".join("%02x" % int(b) for b in sorted(bits))


def _bit(fields: dict[str, float], bit: int) -> float:
    """取字段位值（协议层键形如 ``"0x7"``）。"""
    return float(fields.get(hex(bit), 0.0))


def _quote_from_member(mq: MemberQuote) -> SecurityQuote:
    """协议层 MemberQuote -> 统一 SecurityQuote（基础字段位进属性，其余留在 fields）。

    A股标准通道没有名称与自定义字段位，港股/美股/期货只有 0x122B —— 本条把
    「有名字+有字段位」的那条路归一成同一个模型。
    """
    f = dict(mq.fields)
    market = int(mq.market)
    return SecurityQuote(
        market=market,
        code=mq.code,
        symbol=format_symbol(market, mq.code),
        name=mq.name,
        price=_bit(f, 0x04),
        pre_close=_bit(f, 0x00),
        open=_bit(f, 0x01),
        high=_bit(f, 0x02),
        low=_bit(f, 0x03),
        vol=_bit(f, 0x05),
        amount=_bit(f, 0x07),
        s_vol=0.0,
        b_vol=0.0,
        fields=f,
    )


def _reject_ex(symbols: Sequence[Symbol], what: str) -> None:
    """只有 A股有数据的接口：遇到扩展市场标的直接报错，避免静默返回空。"""
    bad = [s.text for s in symbols if s.is_ex]
    if bad:
        raise ValueError(
            f"{what} 仅支持 A股（深/沪/北），不支持扩展市场标的: {', '.join(bad)}"
        )


def _deprecated(old: str, new: str) -> None:
    warnings.warn(
        f"{old} 已废弃，请改用 {new}（参数写法见 CHANGELOG 迁移表）",
        DeprecationWarning,
        stacklevel=3,
    )


def _today_ymd() -> int:
    d = _date.today()
    return d.year * 10000 + d.month * 100 + d.day


class TdxData:
    """通达信行情数据统一入口。

    标的一律用字符串（见 :mod:`pytdxdata.symbols`）；方法名与方法数只按数据类型分，
    不再按通道/市场分。旧的按通道分裂的方法保留为 deprecated 别名。
    """

    def __init__(
        self,
        *,
        standard_servers: Sequence[str] | None = None,
        mac_servers: Sequence[str] | None = None,
        ex_servers: Sequence[str] | None = None,
        port: int = 7709,
        timeout: float = 3.0,
        cache_dir: str | Path | None = None,
        default_adjust: Adjust = Adjust.NONE,
        max_inflight: int = 64,
        pool_min: int = 3,
        pool_max: int = 12,
        mac_pool_cap: int = 4,
        std_pool_cap: int = 4,
    ) -> None:
        self.port = port
        self.timeout = timeout
        self.default_adjust = default_adjust
        self.mac_pool_cap = mac_pool_cap
        self._std_servers = list(standard_servers or get_standard_hosts())
        self._mac_servers = list(mac_servers or get_mac_hosts())
        self._ex_servers = list(ex_servers or get_ex_hosts())
        self._cache_dir = Path(cache_dir) if cache_dir else None

        # 双池 + 缓存
        self._std_pool = DynamicPool(
            self._std_servers, port=port, timeout=timeout,
            min_size=pool_min, max_size=pool_max,
            per_server_cap=std_pool_cap,
            health=HealthEngine(),
        )
        # 单连接约束下：MAC 并发上限 = MAC 服务器数（每服务器1连接），
        # 故池上限取 pool_max 与服务器数的较大者，确保用满所有可用 MAC 节点。
        mac_cap = max(pool_max, len(self._mac_servers))
        self._mac_pool = DynamicPool(
            self._mac_servers, port=port, timeout=timeout,
            min_size=min(pool_min, len(self._mac_servers) or 1),
            max_size=mac_cap,
            per_server_cap=mac_pool_cap,
            health=HealthEngine(),
            probe_factory=lambda: MacKlineCmd(1, "600000", KlinePeriod.DAY, Adjust.NONE, 0, 100),
        )
        # EX扩展市场池（港股/期货/美股，端口7727；MAC EX login + pytdx EX setup 混合）
        self._ex_pool = ExPool(self._ex_servers, timeout=timeout,
                                min_size=min(pool_min, 2), max_size=min(pool_max, 6),
                                handshake_map=EX_HANDSHAKES)
        disk = None
        if self._cache_dir is not None:
            disk = DiskCache(self._cache_dir / "tdx_cache.db")
        self._cache = CacheManager(
            MemoryCache(), disk=disk, max_inflight=max_inflight,
        )
        self._paginator_std = Paginator(self._std_pool)
        self._paginator_mac = Paginator(self._mac_pool)
        self._started = False

    async def start(self) -> None:
        await self._std_pool.start()
        await self._mac_pool.start()
        await self._ex_pool.start()
        await self._cache.start()
        self._started = True

    async def close(self) -> None:
        await self._std_pool.close()
        await self._mac_pool.close()
        await self._ex_pool.close()
        await self._cache.close()
        self._started = False

    async def __aenter__(self) -> "TdxData":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ================================================================== #
    # 一、标的清单
    # ================================================================== #

    async def get_universe(self, market: str | None = None) -> list[SecurityInfo]:
        """标的清单（A股证券列表 + 扩展市场商品列表），一次覆盖所有市场。

        参数:
            market: 市场选择器 —— ``None``/``"all"``=全部市场；``"sz"/"sh"/"bj"``=A股某市；
                    ``"hk"/"us"/"cffex"/"zz"/"dl"/"shf"/"gz"``=扩展市场（也可用完整枚举名，
                    如 ``"hk_gem"``）。

        返回:
            ``list[SecurityInfo]``，每条带 ``symbol`` 规范化标的串与 ``kind`` 分类描述。
            EX 通道无昨收/单位，对应字段为 0。
        """
        out: list[SecurityInfo] = []
        for mkt, channel in market_targets(market):
            if channel == "ex":
                out.extend(await self._universe_ex(mkt))
            else:
                out.extend(await self._universe_cn(mkt))
        return out

    async def _universe_cn(self, market: int) -> list[SecurityInfo]:
        key = CacheKey("list", market)

        async def fetch() -> bytes:
            total = await self._security_count(market)

            def make_cmd(s: int, c: int):
                return GetSecurityListCmd(market, s)

            spec = PageSpec(page_size=LIST_PAGE, total=total, stop_when_short=True)
            items = await self._fetch_paginated_list(
                "standard", make_cmd, spec,
                concurrency=min(self._std_pool.max_size, 8))
            for it in items:
                it.symbol = format_symbol(it.market, it.code)
            return pack_payload(items)

        return unpack_payload(await self._cache.get_or_fetch(key, fetch))

    async def _ex_all(self) -> list[SecurityInfo]:
        """扩展市场全量商品（一次拉全，缓存1天；按市场过滤在内存里做）。"""
        from .protocol.commands.ex_ext import (
            GetExInstrumentCountCmd,
            GetExInstrumentInfoCmd,
        )
        key = CacheKey("list", None, extra="ex")

        async def fetch() -> bytes:
            total = int(await self._execute_ex(GetExInstrumentCountCmd()))
            items: list[SecurityInfo] = []
            pos = 0
            while pos < total:
                page = await self._execute_ex(
                    GetExInstrumentInfoCmd(0, pos, _EX_PAGE))
                if not page:
                    break
                for g in page:
                    mkt = int(g["market"])
                    code = str(g["code"]).strip()
                    items.append(SecurityInfo(
                        market=mkt,
                        code=code,
                        name=str(g.get("name", "")).strip(),
                        volunit=0,
                        decimal_point=2,
                        pre_close=0.0,
                        symbol=format_symbol(mkt, code),
                        kind=str(g.get("desc", "")).strip(),
                    ))
                pos += len(page)
            return pack_payload(items)

        return unpack_payload(await self._cache.get_or_fetch(key, fetch))

    async def _universe_ex(self, market: int) -> list[SecurityInfo]:
        return [it for it in await self._ex_all() if it.market == market]

    async def _security_count(self, market: int) -> int:
        key = CacheKey("count", market)

        async def fetch() -> bytes:
            n = await self._execute_standard(GetSecurityCountCmd(market))
            return pack_payload(n)

        return unpack_payload(await self._cache.get_or_fetch(key, fetch))

    # ================================================================== #
    # 二、报价
    # ================================================================== #

    async def get_quotes(self, symbols: str | Sequence[str], *,
                         fields: Sequence[int] | None = None
                         ) -> list[SecurityQuote]:
        """报价，一次可跨市场、跨通道混合查询。

        参数:
            symbols: 一个或多个标的字符串（``"sz000001"`` / ``"hk00700"`` / ``"usAAPL"`` /
                     ``"cffex:IFL0"``）。
            fields: 可选自定义字段位（如 ``[0x00, 0x04, 0x38]``）。给了就走 MAC/EX 的
                    0x122B 字段位通道（有 ``name``，无五档）；不给时 A股走标准通道
                    （**有五档 bid/ask、买卖量、涨跌停价，无 name**），扩展市场走 0x122B。

        返回:
            ``list[SecurityQuote]``，按入参顺序排列（各记录带 ``symbol``）。
            A股标准通道与扩展市场的可用字段不同：五档只在 A股标准通道有，
            名称只在 ``fields=`` / 扩展市场有，其余字段位统一落在 ``.fields``。
        """
        syms = parse_symbols(symbols)
        bits = [int(b) for b in fields] if fields else None
        tag = "mac:" + _fields_tag(bits) if bits else "std"
        key = CacheKey.quote_key([s.as_pair() for s in syms], tag=tag)
        payload = await self._cache.get_or_fetch(
            key, lambda: self._fetch_quotes(syms, bits))
        quotes = unpack_payload(payload)
        order = {s.as_pair(): i for i, s in enumerate(syms)}
        quotes.sort(key=lambda q: order.get((int(q.market), q.code), len(order)))
        return quotes

    async def _fetch_quotes(self, syms: list[Symbol], bits: list[int] | None) -> bytes:
        out: list[SecurityQuote] = []
        cn = [s for s in syms if not s.is_ex]
        ex = [s for s in syms if s.is_ex]
        if bits is None:
            if cn:
                out.extend(await self._quotes_std(cn))
            if ex:
                rows = await self._execute_ex(
                    MacSymbolQuotesCmd([s.as_pair() for s in ex], None))
                out.extend(_quote_from_member(m) for m in rows)
        else:
            for i in range(0, len(syms), _QUOTE_PAGE):
                batch = syms[i:i + _QUOTE_PAGE]
                pairs = [s.as_pair() for s in batch]
                cn_pairs = [p for p, s in zip(pairs, batch) if not s.is_ex]
                ex_pairs = [p for p, s in zip(pairs, batch) if s.is_ex]
                if cn_pairs:
                    rows = await self._execute_mac(MacSymbolQuotesCmd(cn_pairs, bits))
                    out.extend(_quote_from_member(m) for m in rows)
                if ex_pairs:
                    rows = await self._execute_ex(MacSymbolQuotesCmd(ex_pairs, bits))
                    out.extend(_quote_from_member(m) for m in rows)
        return pack_payload(out)

    async def _quotes_std(self, syms: list[Symbol]) -> list[SecurityQuote]:
        """A股标准通道五档报价（>80 只自动切批）。"""
        out: list[SecurityQuote] = []
        for i in range(0, len(syms), MAX_QUOTES):
            batch = syms[i:i + MAX_QUOTES]
            cmd = GetSecurityQuotesCmd([s.as_pair() for s in batch])
            rows = await self._execute_standard(cmd)
            for q in rows:
                q.symbol = format_symbol(q.market, q.code)
            out.extend(rows)
        return out

    async def get_snapshot(self, symbols: str | Sequence[str]) -> list[SymbolSnapshot]:
        """个股特征快照（当日/换手/活跃度等，仅 A股）。"""
        syms = parse_symbols(symbols)
        _reject_ex(syms, "个股特征快照")
        out: list[SymbolSnapshot] = []
        for sym in syms:
            key = CacheKey("snapshot", sym.market, sym.code)

            async def fetch(s: Symbol = sym) -> bytes:
                rec = await self._execute_mac(MacSymbolInfoCmd(s.market, s.code))
                if rec is not None:
                    rec.symbol = s.text
                return pack_payload(rec)

            rec = unpack_payload(await self._cache.get_or_fetch(key, fetch))
            if rec is not None:
                out.append(rec)
        return out

    async def get_auction(self, symbols: str | Sequence[str]) -> list[AuctionItem]:
        """集合竞价（仅 A股）。"""
        syms = parse_symbols(symbols)
        _reject_ex(syms, "集合竞价")
        out: list[AuctionItem] = []
        for sym in syms:
            items = await self._execute_mac(MacAuctionCmd(sym.market, sym.code))
            for it in items:
                it.symbol = sym.text
            out.extend(items)
        return out

    async def get_capital_flow(self, symbols: str | Sequence[str]) -> list[CapitalFlow]:
        """个股资金流向（仅 A股）。"""
        syms = parse_symbols(symbols)
        _reject_ex(syms, "资金流向")
        out: list[CapitalFlow] = []
        for sym in syms:
            flow = await self._execute_mac(MacCapitalFlowCmd(sym.market, sym.code))
            if flow is not None:
                flow.symbol = sym.text
                out.append(flow)
        return out

    # ================================================================== #
    # 三、K线
    # ================================================================== #

    async def get_bars(
        self,
        symbols: str | Sequence[str],
        period: KlinePeriod = KlinePeriod.DAY,
        *,
        start: int = 0,
        count: int = 800,
        adjust: Adjust | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        concurrency: int = 6,
    ) -> list[SecurityBar]:
        """K线（多标的并发，自动判指数、自动复权选路、自动跨市场）。

        参数:
            symbols: 一个或多个标的字符串；混合市场会按通道并发拉取后按入参顺序拼接。
            period: K线周期（``KlinePeriod.DAY`` 等）。
            start: 起始偏移量（从最早K线开始的索引，默认0）。
            count: 获取条数（默认800；超过单页会自动多连接并发分页）。
            adjust: 复权方式（默认用实例 ``default_adjust``）；``QFQ``/``HFQ`` 自动走 MAC 通道。
            start_date / end_date: 按日期区间取（与 start/count 互斥，优先日期过滤）。
                只给 ``start_date`` 即增量更新语义（取该日期之后的全部K线）。

        返回:
            ``list[SecurityBar]``，按入参标的顺序拼接，每根带 ``symbol``；组内按时间正序。
        """
        period = KlinePeriod(int(period))
        syms = parse_symbols(symbols)
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(sym: Symbol) -> list[SecurityBar]:
            async with sem:
                return await self._bars_one(
                    sym, period, start=start, count=count, adjust=adjust,
                    start_date=start_date, end_date=end_date)

        groups = await asyncio.gather(*(one(s) for s in syms))
        return [b for g in groups for b in g]

    async def _bars_one(
        self, sym: Symbol, period: KlinePeriod, *,
        start: int, count: int, adjust: Adjust | None,
        start_date: datetime | None, end_date: datetime | None,
    ) -> list[SecurityBar]:
        if start_date is not None or end_date is not None:
            return await self._bars_by_date(sym, period, start_date, end_date, adjust)
        adj = self.default_adjust if adjust is None else adjust
        if sym.is_ex:
            return await self._bars_ex(sym, period, adj, start, count)

        route = route_for("kline", adjust=adj, period=period)
        key = CacheKey("kline", sym.market, sym.code, period.name, start, count, adj.name)
        mk = sym.market
        index = is_cn_index(mk, sym.code)

        def make_cmd(s: int, c: int):
            if route == "mac":
                return MacKlineCmd(mk, sym.code, period, adj, s, c)
            if index:
                return GetIndexBarsCmd(mk, sym.code, period, s, c)
            return GetSecurityBarsCmd(mk, sym.code, period, s, c)

        if route == "mac":
            payload = await self._cache.get_or_fetch(
                key, lambda: self._fetch_mac_bars_serial(
                    make_cmd, start + count, start=start, page_size=700,
                )
            )
        else:
            payload = await self._cache.get_or_fetch(
                key, lambda: self._fetch_paginated("std", make_cmd, PageSpec(
                    page_size=KLINE_PAGE, total=start + count, min_page=100,
                    start_offset=start,
                ))
            )
        bars = unpack_payload(payload)
        return self._finish_bars(bars, sym)

    async def _bars_ex(self, sym: Symbol, period: KlinePeriod, adj: Adjust,
                       start: int, count: int) -> list[SecurityBar]:
        """扩展市场K线（EX 0x23FF；category=周期编码，与 TDXParams 一致）。"""
        from .protocol.commands.ex_proto import GetExInstrumentBarsCmd
        cat = int(period)
        fetch_count = max(700, start + count)
        key = CacheKey("kline", sym.market, sym.code, period.name, start,
                       fetch_count, f"{adj.name}@{cat}")

        async def fetch() -> bytes:
            rows = await self._fetch_paginated_ex(
                lambda s2, c2: GetExInstrumentBarsCmd(sym.market, sym.code, cat, s2, c2),
                PageSpec(page_size=700, total=fetch_count, min_page=100))
            bars = [SecurityBar(
                market=sym.market, code=sym.code,
                year=d["year"], month=d["month"], day=d["day"],
                hour=d.get("hour", 0), minute=d.get("minute", 0),
                open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                vol=float(d.get("trade", 0) or 0), amount=0.0,
            ) for d in rows]
            return pack_payload(bars)

        bars = self._finish_bars(unpack_payload(await self._cache.get_or_fetch(key, fetch)), sym)
        # EX 0x23FF 按时间升序返回(老→新)：start=0 取最新 count 根
        if count < len(bars):
            return bars[-count:] if start == 0 else bars[start:start + count]
        return bars

    @staticmethod
    def _finish_bars(bars: list[SecurityBar], sym: Symbol) -> list[SecurityBar]:
        """统一补 symbol + 按时间正序（不依赖协议层顺序保证）。"""
        for b in bars:
            b.symbol = sym.text
        bars.sort(key=lambda b: (b.year, b.month, b.day, b.hour, b.minute))
        return bars

    async def _bars_by_date(
        self, sym: Symbol, period: KlinePeriod,
        start_date: datetime | None, end_date: datetime | None,
        adjust: Adjust | None,
    ) -> list[SecurityBar]:
        """按日期区间取K线：先二分定位偏移，再只拉目标区间。

        关键坑：**偏移方向各通道不一致**（实测标准通道 offset 0 是最新一根、
        越大越老；MAC/EX 反过来），所以先探一次方向再二分，不做静态假设。
        """
        # 归一化日期边界
        start_dt = (start_date if isinstance(start_date, datetime)
                    else datetime.combine(start_date, datetime.min.time())) \
            if start_date is not None else None
        end_dt = (end_date if isinstance(end_date, datetime)
                  else datetime.combine(end_date, datetime.max.time())) \
            if end_date is not None else None
        # end_date 传入 datetime 但时间为 00:00 时视为"当天结束"
        if end_dt is not None and end_dt.time() == datetime.min.time().replace(tzinfo=None):
            end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999999)

        est = _KLINE_MAX_BARS.get(period, 10_000)
        total = await self._bar_total(sym, period, adjust, est)
        if total <= 0:
            return []
        desc = await self._offset_descending(sym, period, adjust, total)

        if desc:
            # offset 越大越老：[0,k) 全都 >= start_date，[j,∞) 全都 <= end_date
            k = total
            if start_dt is not None:
                k = await self._bisect_offset(
                    sym, period, adjust, total, lambda b: b.datetime < start_dt)
            j = 0
            if end_dt is not None:
                j = await self._bisect_offset(
                    sym, period, adjust, total, lambda b: b.datetime <= end_dt)
            lo, hi = j, k
        else:
            # offset 越大越新：[lo,hi) 即目标区间
            lo = 0
            if start_dt is not None:
                lo = await self._bisect_offset(
                    sym, period, adjust, total, lambda b: b.datetime >= start_dt)
            hi = total
            if end_dt is not None:
                hi = await self._bisect_offset(
                    sym, period, adjust, total, lambda b: b.datetime > end_dt)

        if lo >= hi:
            return []
        bars = await self.get_bars(sym.text, period, start=lo, count=hi - lo,
                                   adjust=adjust)
        # 边界可能多拉（页对齐），做精确日期裁剪
        if start_dt is not None:
            bars = [b for b in bars if b.datetime >= start_dt]
        if end_dt is not None:
            bars = [b for b in bars if b.datetime <= end_dt]
        return bars

    async def _bar_total(self, sym: Symbol, period: KlinePeriod,
                         adjust: Adjust | None, est: int) -> int:
        """指数扩张找K线总根数上界（探测到越界为止）。"""
        n = est
        for _ in range(6):      # 最多放大 64 倍
            if await self._probe_bar(sym, period, adjust, n) is None:
                return n        # 偏移 n 越界 → 总根数 <= n
            n *= 2
        return n

    async def _offset_descending(self, sym: Symbol, period: KlinePeriod,
                                 adjust: Adjust | None, total: int) -> bool:
        """探测偏移方向：True=offset 0 是最新一根（标准通道），False=最老一根。

        比 0 号偏移和随便一个**在范围内**的偏移即可；``total`` 只是上界，
        所以从近到远试几个候选，全都越界时按标准通道口径兜底。
        """
        if total < 2:
            return True
        newest = await self._probe_bar(sym, period, adjust, 0)
        if newest is None:
            return True
        for off in (1, 10, 100, total - 1):
            if off <= 0 or off >= total:
                continue
            bar = await self._probe_bar(sym, period, adjust, off)
            if bar is not None:
                return bar.datetime < newest.datetime
        return True

    async def _bisect_offset(self, sym: Symbol, period: KlinePeriod,
                             adjust: Adjust | None, total: int, pred) -> int:
        """二分找第一个满足 ``pred(bar)`` 的偏移量。

        越界（无数据）视为「边界在左侧」→ 收缩 hi；两个方向都靠 pred 表达，
        所以递增/递减通道共用同一份代码。
        """
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi) // 2
            bar = await self._probe_bar(sym, period, adjust, mid)
            if bar is None or pred(bar):
                hi = mid
            else:
                lo = mid + 1
        return lo

    async def _probe_bar(self, sym: Symbol, period: KlinePeriod,
                         adjust: Adjust | None, offset: int) -> SecurityBar | None:
        """探测指定偏移量处的单条K线（越界返回 None）。"""
        adj = self.default_adjust if adjust is None else adjust
        route = route_for("kline", adjust=adj, period=period)
        mk = sym.market
        if route == "mac" or sym.is_ex:
            cmd = MacKlineCmd(mk, sym.code, period, adj, offset, 1)
            run = self._execute_ex if sym.is_ex else self._execute_mac
        else:
            cmd = (GetIndexBarsCmd(mk, sym.code, period, offset, 1)
                   if is_cn_index(mk, sym.code)
                   else GetSecurityBarsCmd(mk, sym.code, period, offset, 1))
            run = self._execute_standard
        try:
            bars = await run(cmd)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            return None
        return bars[0] if bars else None

    async def get_indicators(
        self, symbols: str | Sequence[str], indicators: Sequence[str], *,
        period: KlinePeriod = KlinePeriod.DAY, count: int = 30,
        adjust: Adjust | None = None, params: dict | None = None,
    ) -> list[IndicatorSet]:
        """K线 + 技术指标（MACD/KDJ/RSI/BOLL/MA/EMA）。

        返回 ``list[IndicatorSet]``（每个标的一组：``bars`` + ``indicators``），
        指标序列与 ``bars`` 等长（尾部对齐）。
        """
        from .indicator import compute_indicators
        syms = parse_symbols(symbols)
        bars = await self.get_bars(syms, period, count=max(120 + count, 200),
                                   adjust=adjust)
        grouped: dict[str, list[SecurityBar]] = {}
        for b in bars:
            grouped.setdefault(b.symbol, []).append(b)
        out: list[IndicatorSet] = []
        for sym in syms:
            rows = grouped.get(sym.text, [])
            if not rows:
                out.append(IndicatorSet(symbol=sym.text, bars=[], indicators={}))
                continue
            ind = compute_indicators(
                [r.high for r in rows], [r.low for r in rows],
                [r.close for r in rows], indicators, params, tail=count)
            out.append(IndicatorSet(
                symbol=sym.text,
                bars=rows[-count:],
                indicators={k: (v[-count:] if len(v) >= count else v)
                            for k, v in ind.items()},
            ))
        return out

    # ================================================================== #
    # 四、逐笔成交 / 分时
    # ================================================================== #

    async def get_ticks(
        self, symbols: str | Sequence[str], *,
        date: int | None = None, start: int = 0, count: int | None = None,
        concurrency: int = 6,
    ) -> list[TransactionRecord]:
        """逐笔成交（A股 MAC 0x122F / 港股 EX 0x23FC·0x2406 / 其他扩展市场 MAC）。

        参数:
            date: ``None``=当日，``YYYYMMDD``=历史某日。
            start: 起始偏移。
            count: ``None``=尽量取全量（自动翻页，上限 50 页）；给了就取这么多条。

        返回:
            ``list[TransactionRecord]``，按入参标的顺序拼接（A股含 ``trade_count``，
            港股为 HH:MM 时间 + ``fields["zengcang"]`` 增仓）。
        """
        syms = parse_symbols(symbols)
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(sym: Symbol) -> list[TransactionRecord]:
            async with sem:
                return await self._ticks_one(sym, date=date, start=start, count=count)

        groups = await asyncio.gather(*(one(s) for s in syms))
        return [t for g in groups for t in g]

    async def _ticks_one(self, sym: Symbol, *, date: int | None, start: int,
                         count: int | None) -> list[TransactionRecord]:
        key = CacheKey("transaction", sym.market, sym.code, "", start, count,
                       "" if date is None else str(date))

        async def fetch() -> bytes:
            if sym.is_ex:
                rows = await self._ticks_ex(sym, date=date, start=start, count=count)
            else:
                def make_cmd(s: int, c: int):
                    return MacTransactionCmd(sym.market, sym.code, date or 0, s, c)

                spec = PageSpec(
                    page_size=_TICK_PAGE,
                    total=None if count is None else start + count,
                    min_page=100,
                    stop_when_short=True,
                    max_pages=_ALL_PAGES_CAP if count is None else None,
                    start_offset=start,
                )
                rows = await self._fetch_paginated_list("mac", make_cmd, spec)
            return pack_payload(rows)

        records = unpack_payload(await self._cache.get_or_fetch(key, fetch))
        for r in records:
            r.symbol = sym.text
        return records

    async def _ticks_ex(self, sym: Symbol, *, date: int | None, start: int,
                        count: int | None) -> list[TransactionRecord]:
        from .protocol.commands.ex_ext import (
            GetExHistoryTransactionDataCmd,
            GetExTransactionDataCmd,
        )
        hk = sym.market in _HK_MARKETS
        page = _EX_TICK_PAGE if hk else _TICK_PAGE
        out: list[TransactionRecord] = []
        pos = start
        remaining = count
        for _ in range(_ALL_PAGES_CAP):
            n = page if remaining is None else min(page, remaining)
            if n <= 0:
                break
            if hk:
                cmd = (GetExHistoryTransactionDataCmd(date, sym.market, sym.code, pos, n)
                       if date else
                       GetExTransactionDataCmd(sym.market, sym.code, pos, n))
                rows = await self._execute_ex(cmd)
                out.extend(self._ex_tick(sym, r) for r in rows)
            else:
                cmd = MacTransactionCmd(sym.market, sym.code, date or 0, pos, n)
                rows = await self._execute_ex(cmd)
                for r in rows:
                    r.symbol = sym.text
                out.extend(rows)
            if not rows:
                break
            pos += len(rows)
            if remaining is not None:
                remaining -= len(rows)
                if remaining <= 0:
                    break
            if len(rows) < n:
                break
        return out

    @staticmethod
    def _ex_tick(sym: Symbol, row: dict) -> TransactionRecord:
        """扩展市场逐笔原始 dict -> 统一 TransactionRecord。"""
        minutes = int(row.get("minutes", 0) or 0)
        fields = {k: float(v) for k, v in row.items()
                  if k in ("zengcang", "momentum") and v is not None}
        return TransactionRecord(
            market=sym.market, code=sym.code, symbol=sym.text,
            time="%02d:%02d" % (minutes // 60, minutes % 60),
            price=float(row.get("price", 0.0) or 0.0),
            vol=float(row.get("vol", 0.0) or 0.0),
            trade_count=0,
            bs_flag=int(row.get("direction", 0) or 0),
            fields=fields,
        )

    async def get_minutes(
        self, symbols: str | Sequence[str], *,
        date: int | None = None, sampling: bool = False, concurrency: int = 6,
    ) -> list[MinuteBar]:
        """分时（A股 MAC 0x122D / 扩展市场 0x122D 同构）。

        参数:
            date: ``None``=今天；``YYYYMMDD``=历史某日（A股支持历史分时）。
            sampling: ``True`` 取缩略采样（约240个价格点，无时间；A股 0x254D，
                扩展市场同命令），``vol``/``avg_price`` 为 0，点位序号在 ``fields["i"]``。

        返回:
            ``list[MinuteBar]``，按入参标的顺序拼接，每条带 ``symbol``/``market``/
            ``code``/``date``（扩展市场的动量落在 ``fields["momentum"]``）。
        """
        syms = parse_symbols(symbols)
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(sym: Symbol) -> list[MinuteBar]:
            async with sem:
                return await self._minutes_one(sym, date=date, sampling=sampling)

        groups = await asyncio.gather(*(one(s) for s in syms))
        return [m for g in groups for m in g]

    async def _minutes_one(self, sym: Symbol, *, date: int | None,
                           sampling: bool) -> list[MinuteBar]:
        ymd = _today_ymd() if date is None else int(date)
        key = CacheKey("minute", sym.market, sym.code,
                       "SAMPLE" if sampling else "", 0, 0, str(ymd))

        async def fetch() -> bytes:
            if sampling:
                run = self._execute_ex if sym.is_ex else self._execute_mac
                prices = await run(MacChartSamplingCmd(sym.market, sym.code))
                bars = [MinuteBar(price=float(p), vol=0.0, symbol=sym.text,
                                  market=sym.market, code=sym.code, date=ymd,
                                  fields={"i": float(i)})
                        for i, p in enumerate(prices)]
                return pack_payload(bars)
            if sym.is_ex:
                rows = await self._execute_ex(SymbolTickChartCmd(
                    sym.market, sym.code, 0 if date is None else ymd))
                bars = []
                for row in rows:
                    minutes = int(row.get("minutes", 0) or 0)
                    bars.append(MinuteBar(
                        price=float(row.get("price", 0.0) or 0.0),
                        vol=float(row.get("vol", 0.0) or 0.0),
                        avg_price=float(row.get("avg", 0.0) or 0.0),
                        hour=minutes // 60, minute=minutes % 60,
                        symbol=sym.text, market=sym.market, code=sym.code, date=ymd,
                        fields={"momentum": float(row.get("momentum", 0.0) or 0.0)},
                    ))
                return pack_payload(bars)
            bars = await self._execute_mac(MacTickChartCmd(sym.market, sym.code, ymd))
            return pack_payload(bars)

        bars = unpack_payload(await self._cache.get_or_fetch(key, fetch))
        for b in bars:
            b.symbol = sym.text
            b.market = sym.market
            b.code = sym.code
            b.date = ymd
        return bars

    # ================================================================== #
    # 五、公司信息
    # ================================================================== #

    async def get_xdxr(self, symbols: str | Sequence[str]) -> list[XdxrRecord]:
        """除权除息历史，仅 A股。标的用字符串（``"sz000001"``）。"""
        syms = parse_symbols(symbols)
        _reject_ex(syms, "除权除息")
        out: list[XdxrRecord] = []
        for sym in syms:
            key = CacheKey("xdxr", sym.market, sym.code)

            async def fetch(s: Symbol = sym) -> bytes:
                recs = await self._execute_standard(GetXdxrInfoCmd(s.market, s.code))
                return pack_payload(recs)

            recs = unpack_payload(await self._cache.get_or_fetch(key, fetch))
            for r in recs:
                r.symbol = sym.text
            out.extend(recs)
        return out

    async def get_finance(self, symbols: str | Sequence[str]) -> list[FinanceRecord]:
        """财务快照（仅 A股）。无数据的标的会被跳过（原单标的接口返回 None）。"""
        syms = parse_symbols(symbols)
        _reject_ex(syms, "财务快照")
        out: list[FinanceRecord] = []
        for sym in syms:
            key = CacheKey("finance", sym.market, sym.code)

            async def fetch(s: Symbol = sym) -> bytes:
                rec = await self._execute_standard(GetFinanceInfoCmd(s.market, s.code))
                return pack_payload(rec)

            rec = unpack_payload(await self._cache.get_or_fetch(key, fetch))
            if rec is not None:
                rec.symbol = sym.text
                out.append(rec)
        return out

    async def get_price_limits(self, symbol: str, name: str | None = None,
                               pre_close: float | None = None,
                               listed_days: int = 9999
                               ) -> tuple[float | None, float | None]:
        """涨跌停价（本地计算，无网络；仅 A股，指数/基金返回 (None, None)）。"""
        sym = parse_symbol(symbol)
        _reject_ex([sym], "涨跌停价")
        return compute_price_limits(Market(sym.market), sym.code, name, pre_close,
                                    listed_days)

    async def get_f10(self, symbol: str, section: str | None = None, *,
                      offset: int = 0, length: int = 65535) -> list | str:
        """F10 公司资料（仅 A股）。

        ``section=None`` 返回目录（``list[CompanyInfoCategory]``，含 ``name``/
        ``filename``/``length``）；给了 ``section`` 则返回该条目正文
        （``str``，用 ``offset``/``length`` 分块读取长文）。
        """
        sym = parse_symbol(symbol)
        _reject_ex([sym], "F10")
        if section is None:
            return await self._execute_standard(
                GetCompanyInfoCategoryCmd(sym.market, sym.code))
        return await self._execute_standard(
            GetCompanyInfoContentCmd(sym.market, sym.code, section, offset, length))

    # ================================================================== #
    # 六、市场
    # ================================================================== #

    async def get_market_stat(self) -> MarketStat:
        """全市场统计（涨跌家数/总成交额/总市值/涨跌停家数，A股）。"""
        q = await self.get_quotes(["sh880005", "sh880001", "sh880006"])
        stat = MarketStat()
        qmap = {x.code: x for x in q}
        q5 = qmap.get("880005")
        if q5:
            stat.up_count = int(q5.price * 10)
            stat.down_count = int(q5.open * 10)
            stat.flat_count = int(q5.low * 10)
            stat.total_count = int(q5.high * 10)
            stat.total_amount = q5.amount
            stat.total_vol = q5.vol
        q1 = qmap.get("880001")
        if q1:
            stat.total_market_cap = q1.price * 1e10
        q6 = qmap.get("880006")
        if q6:
            stat.limit_up_count = int(q6.price * 10)
            stat.limit_down_count = int(q6.open * 10)
        return stat

    async def get_unusual(self, market: str = "all", *, start: int = 0,
                          count: int = 600) -> list[UnusualItem]:
        """市场异动（仅 A股；``market="all"`` = 深市 + 沪市）。"""
        markets = [mkt for mkt, ch in market_targets(market) if ch == "cn"]
        out: list[UnusualItem] = []
        for mkt in markets:
            items = await self._execute_mac(MacUnusualCmd(mkt, start, count))
            for it in items:
                it.symbol = format_symbol(it.market, it.code)
            out.extend(items)
        return out

    async def get_server_info(self) -> ServerInfo:
        """服务器交易时段信息（当日/上一交易日/交易时段）。"""
        return await self._execute_mac(MacServerInfoCmd())

    async def get_kline_offset(self, offset: int = 0, count: int = 1) -> tuple[int, int]:
        """K线偏移信息（MAC 0x124A，返回 (总根数, 本次返回数)）。"""
        return await self._execute_mac(MacKlineOffsetCmd(offset, count))

    # ================================================================== #
    # 七、板块
    # ================================================================== #

    @staticmethod
    def _board_type(kind: str | int) -> int:
        if isinstance(kind, int):
            return kind
        key = str(kind).strip().lower()
        table = {"all": 0, "industry": 1, "concept": 2, "style": 3}
        if key not in table:
            raise ValueError(
                f"未知板块类别: {kind}（可选: {'/'.join(table)} 或数字）")
        return table[key]

    async def get_boards(self, kind: str = "all", *, count: int = 10000) -> list[BoardInfo]:
        """板块列表（``kind``: ``all``/``industry``/``concept``/``style``，也接受数字）。

        每条除板块本身外还带领涨股信息（``symbol_*`` 字段）。
        """
        board_type = self._board_type(kind)
        items: list[BoardInfo] = []
        start = 0
        while start < count:
            # 沿用既有分页步进（实盘未校验，改动风险大于收益）
            batch = await self._execute_mac(MacBoardListCmd(board_type, start, 150))
            if not batch:
                break
            items.extend(batch)
            start += len(batch) // 2
            if len(batch) < 150:
                break
        for b in items:
            b.symbol = format_symbol(b.market, b.code)
        return items

    async def get_board_members(self, board: str, count: int = 100000,
                                sort_type: int = 0, *,
                                fields: Sequence[int] | None = None
                                ) -> list[SecurityQuote]:
        """板块成分股报价（``board`` 如 ``"881001"``；返回归一报价模型）。

        ``fields`` 可加取自定义字段位（如 ``[0x38]`` 主力净流入），默认取基础字段位
        （昨收/开/高/低/现价/量/量比/额），全部字段位同时保留在 ``.fields``。
        """
        board_code = self._convert_board_code(parse_symbol(board).code)
        bits = [int(b) for b in fields] if fields else list(_QUOTE_BITS)
        items: list[SecurityQuote] = []
        start = 0
        while start < count:
            batch = await self._execute_mac(MacBoardMembersQuotesCmd(
                board_code, sort_type=sort_type, start=start,
                page_size=_QUOTE_PAGE, bits=bits))
            if not batch:
                break
            items.extend(_quote_from_member(m) for m in batch)
            start += len(batch)
            if len(batch) < _QUOTE_PAGE:
                break
        return items

    async def get_board_of(self, symbols: str | Sequence[str]) -> list[BelongBoard]:
        """个股所属板块（仅 A股；``symbol`` 记录被查询的个股）。"""
        syms = parse_symbols(symbols)
        _reject_ex(syms, "个股所属板块")
        out: list[BelongBoard] = []
        for sym in syms:
            items = await self._execute_mac(MacBelongBoardCmd(sym.market, sym.code))
            for it in items:
                it.symbol = sym.text
            out.extend(items)
        return out

    async def get_board_summary(self, board: str) -> dict:
        """板块汇总：成分数/总成交额/主力净流入/涨跌家数。"""
        # 0x38 = 主力净流入（基础字段位之外额外取）
        members = await self.get_board_members(board, fields=_QUOTE_BITS + [0x38])
        amount = sum(q.amount for q in members)
        main_net = sum(float(q.fields.get("0x38", 0.0)) for q in members)
        up = sum(1 for q in members if q.pre_close and q.price > q.pre_close)
        down = sum(1 for q in members if q.pre_close and q.price < q.pre_close)
        return {
            "member_count": len(members),
            "amount": amount,
            "main_net_amount": main_net,
            "up_count": up,
            "down_count": down,
        }

    async def get_board_ranking(self, kind: str = "industry", *,
                                top_n: int = 20) -> list[dict]:
        """板块当日涨跌幅排行（基于板块报价的现价/昨收）。"""
        return self._rank_boards(await self.get_boards(kind, count=200), top_n)

    async def get_board_change_ranking(self, kind: str = "industry", *,
                                       days: int = 20, top_n: int = 20) -> list[dict]:
        """板块 N 日涨跌幅排行（基于板块指数K线）。"""
        boards = self._board_codes(await self.get_boards(kind, count=200))
        result: list[dict] = []
        for code, b in list(boards.items())[:top_n * 3]:
            try:
                bars = await self.get_bars(format_symbol(b.market, code),
                                           KlinePeriod.DAY, count=days + 5,
                                           adjust=Adjust.NONE)
            except Exception:
                continue
            if len(bars) < 2:
                continue
            first, last = bars[0].close, bars[-1].close
            if first <= 1.0:   # 过滤异常基数(板块指数首根过小)
                continue
            result.append({"code": code, "name": b.name, "close_end": last,
                           "close_start": first,
                           "change_pct": round((last / first - 1) * 100, 2)})
        result.sort(key=lambda x: x["change_pct"], reverse=True)
        return result[:top_n]

    @staticmethod
    def _board_codes(boards: list[BoardInfo]) -> dict[str, BoardInfo]:
        """去重取板块段（board_list 返回板块+领涨股混合，只保留 881xxx）。"""
        out: dict[str, BoardInfo] = {}
        for b in boards:
            if not b.code.startswith("88"):
                continue
            if b.code not in out or len(b.name) < len(out[b.code].name):
                out[b.code] = b
        return out

    @classmethod
    def _rank_boards(cls, boards: list[BoardInfo], top_n: int) -> list[dict]:
        result: list[dict] = []
        for code, b in list(cls._board_codes(boards).items())[:top_n * 3]:
            if b.pre_close <= 0:
                continue
            result.append({"code": code, "name": b.name, "price": b.price,
                           "change_pct": round((b.price / b.pre_close - 1) * 100, 2)})
        result.sort(key=lambda x: x["change_pct"], reverse=True)
        return result[:top_n]

    # ================================================================== #
    # 八、服务器文件（原始字节 + 解析）
    # ================================================================== #

    async def get_file(self, name: str) -> bytes:
        """下载服务器文件原始字节（按文件名自动选通道）。

        - ``block_*.dat`` —— 板块文件（标准 0x1869/0x186A，先查元数据再分块）
        - ``tdxhy.cfg`` / ``tdxfin/*`` —— 报告财务文件（标准 0x06B9，分块）
        - 其他 —— MAC 远程文件（0x1215 查元数据 + 0x1217 分块）

        通常不用直接调它：板块/行业/财报都有解析好的 ``get_*_parsed``。
        """
        base = str(name).strip()
        low = base.lower()
        if low.startswith("block_"):
            meta = await self._execute_standard(GetBlockInfoMetaCmd(base))
            if meta is None:
                return b""
            return await self._download(
                self._execute_standard,
                lambda pos, idx, n: GetBlockInfoCmd(base, pos, n), meta.size)
        if low.startswith("tdx"):
            return await self._download(
                self._execute_standard,
                lambda pos, idx, n: GetReportFileCmd(base, pos, n), 0)
        meta = await self._execute_mac(MacFileListCmd(base))
        size = meta.size if meta else 0
        return await self._download(
            self._execute_mac,
            lambda pos, idx, n: MacFileDownloadCmd(base, idx, pos, n), size)

    async def _download(self, run, make_cmd, size: int, *,
                        chunk: int = 30000) -> bytes:
        """分块下载（``size<=0`` 时翻到空块为止）。"""
        data = bytearray()
        pos = 0
        idx = 1
        while size <= 0 or pos < size:
            part = await run(make_cmd(pos, idx, chunk))
            if not part:
                break
            data.extend(part)
            pos += len(part)
            idx += 1
            if len(part) < chunk:
                break
        return bytes(data)

    async def get_block_parsed(self, filename: str) -> list[TdxBlock]:
        """板块文件 -> ``list[TdxBlock]``（板块名/类别/成分股）。

        常用文件名：``block_zs.dat``（行业）/ ``block_gn.dat``（概念）/ ``block_fg.dat``（风格）。
        category 由文件名推断（0=行业 1=地域 2=概念 3=风格）。
        """
        from .codec.block import parse_block_dat
        return parse_block_dat(await self.get_file(filename), filename)

    async def get_industry_map(self) -> dict[str, IndustryInfo]:
        """tdxhy.cfg -> ``{6位代码: IndustryInfo}``（通达信 + 申万行业）。"""
        from .codec.industry import parse_tdxhy_cfg
        return parse_tdxhy_cfg(await self.get_file("tdxhy.cfg"))

    async def get_financial_file_infos(self) -> list[FinancialFileInfo]:
        """tdxfin/gpcw.txt -> ``list[FinancialFileInfo]``（文件名/MD5/大小）。"""
        from .codec.financial import parse_financial_file_list
        return parse_financial_file_list(await self.get_file("tdxfin/gpcw.txt"))

    async def get_financial_records_parsed(self, filename: str) -> list[FinancialRecord]:
        """财务 zip（``tdxfin/gpcw*.zip``）解压解析 -> ``list[FinancialRecord]``。

        报告期优先取文件名中的 8 位日期，缺失时回退到 .dat 头部内嵌值。
        """
        import io
        import re
        import zipfile

        from .codec.financial import parse_financial_dat

        zip_data = await self.get_file(filename)
        if not zip_data:
            return []
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            dat_names = [n for n in zf.namelist() if n.endswith(".dat")]
            if not dat_names:
                return []
            dat_data = zf.read(dat_names[0])
        match = re.search(r"(\d{8})", filename)
        report_date = int(match.group(1)) if match else 0
        return parse_financial_dat(dat_data, report_date)

    # ================================================================== #
    # 内部执行（连接池 + 失败换连接 + 分页）
    # ================================================================== #

    async def _execute_standard(self, cmd) -> object:
        """标准通道单命令执行（带连接池+失败换连接）。"""
        pc = await self._std_pool.acquire()
        try:
            for attempt in range(3):
                try:
                    return await pc.execute(cmd)
                except Exception:
                    if attempt >= 2:
                        raise
                    await self._std_pool.release(pc, broken=True)
                    pc = await self._std_pool.acquire()
        finally:
            await self._std_pool.release(pc)

    async def _execute_mac(self, cmd) -> object:
        """MAC通道单命令执行（带连接池+失败换连接）。"""
        pc = await self._mac_pool.acquire()
        try:
            for attempt in range(3):
                try:
                    return await pc.execute(cmd)
                except Exception:
                    if attempt >= 2:
                        raise
                    await self._mac_pool.release(pc, broken=True)
                    pc = await self._mac_pool.acquire()
        finally:
            await self._mac_pool.release(pc)

    async def _execute_ex(self, cmd) -> object:
        """EX扩展市场单命令执行（连接池+失败/空结果重建）。"""
        conn = await self._ex_pool.acquire()
        try:
            for attempt in range(3):
                try:
                    result = await conn.execute(cmd)
                    # 空结果可能是login会话失效（服务器静默返回0条）→ 新连接重试
                    if result in (None, []) and attempt < 2:
                        await self._ex_pool.release(conn, broken=True)
                        conn = await self._ex_pool.acquire()
                        continue
                    return result
                except Exception:
                    if attempt >= 2:
                        raise
                    await self._ex_pool.release(conn, broken=True)
                    conn = await self._ex_pool.acquire()
        finally:
            await self._ex_pool.release(conn)

    async def _fetch_paginated(self, route: str, make_cmd, spec: PageSpec,
                               *, concurrency: int | None = None) -> bytes:
        pag = self._paginator_mac if route == "mac" else self._paginator_std
        result = await pag.fetch_all(make_cmd, spec, concurrency=concurrency)
        return pack_payload(result)

    async def _fetch_paginated_list(self, route: str, make_cmd, spec: PageSpec,
                                    *, concurrency: int | None = None) -> list:
        """分页拉取，直接返回列表（避免 pack/unpack 序列化开销）。"""
        pag = self._paginator_mac if route == "mac" else self._paginator_std
        return await pag.fetch_all(make_cmd, spec, concurrency=concurrency)

    async def _fetch_mac_bars_serial(self, make_cmd, total: int, *,
                                     start: int = 0, page_size: int = 700) -> bytes:
        """MAC K线串行分页（单连接），避免多服务器数据深度不一致导致空洞。"""
        pc = await self._mac_pool.acquire()
        try:
            result: list = []
            while start < total:
                cnt = min(page_size, total - start)
                cmd = make_cmd(start, cnt)
                for attempt in range(3):
                    try:
                        bars = await pc.execute(cmd)
                        break
                    except Exception:
                        if attempt >= 2:
                            raise
                        await self._mac_pool.release(pc, broken=True)
                        pc = await self._mac_pool.acquire()
                        bars = []
                if not bars:
                    break
                result.extend(bars)
                start += len(bars)
                if len(bars) < cnt:
                    break
            return pack_payload(result)
        finally:
            await self._mac_pool.release(pc)

    async def _fetch_paginated_ex(self, make_cmd, spec: PageSpec) -> list:
        """EX通道分页拉取（串行逐页，扩展市场页数少）。"""
        result = []
        start = 0
        for _ in range(spec.max_pages if spec.max_pages else 64):
            if spec.total is not None and start >= spec.total:
                break
            cnt = spec.page_size
            if spec.total is not None:
                cnt = min(cnt, spec.total - start)
            items = await self._execute_ex(make_cmd(start, cnt))
            if items is None:
                break
            result.extend(items)
            start += len(items)
            if not items or (spec.stop_when_short and len(items) < cnt):
                break
        return result

    @staticmethod
    def _convert_board_code(board_symbol: str) -> int:
        """板块代码转换（881001→20686 等，参照 easy-tdx）。"""
        s = board_symbol.strip()
        if s.startswith("US"):
            return 30000 + int(s[2:])
        if s.startswith("HK"):
            return 20000 + int(s[2:])
        if len(s) == 6:
            if s.startswith("88"):
                return int(s) - 880000 + 20000
            if s.startswith("399"):
                return int(s) - 399000 + 30000
            if s.startswith("899"):
                return int(s) - 899000 + 32000
            if s.startswith("000"):
                return 31000 + int(s)
        return int(s)

    # ================================================================== #
    # 九、旧接口（deprecated 别名；入参沿用旧口径，1.0 移除）
    # ================================================================== #

    async def get_kline(self, market, code, period, *, start: int = 0,
                        count: int = 800, adjust: Adjust | None = None,
                        start_date: datetime | None = None,
                        end_date: datetime | None = None) -> list[SecurityBar]:
        """已废弃 -> :meth:`get_bars`。"""
        _deprecated("get_kline", "get_bars")
        return await self.get_bars(format_symbol(int(market), code), period,
                                   start=start, count=count, adjust=adjust,
                                   start_date=start_date, end_date=end_date)

    async def get_kline_since(self, market, code, period, since: datetime,
                              adjust: Adjust | None = None) -> list[SecurityBar]:
        """已废弃 -> :meth:`get_bars`（``start_date=since``）。"""
        _deprecated("get_kline_since", "get_bars(start_date=...)")
        return await self.get_bars(format_symbol(int(market), code), period,
                                   start_date=since, adjust=adjust)

    async def get_kline_batch(self, stocks, period, *, start: int = 0,
                              count: int = 800, adjust: Adjust | None = None,
                              concurrency: int = 6) -> dict[str, list[SecurityBar]]:
        """已废弃 -> :meth:`get_bars`（返回值由 ``{code: bars}`` 变为平铺列表）。"""
        _deprecated("get_kline_batch", "get_bars")
        bars = await self.get_bars(
            [format_symbol(int(m), c) for m, c in stocks], period, start=start,
            count=count, adjust=adjust, concurrency=concurrency)
        out: dict[str, list[SecurityBar]] = {}
        for b in bars:
            out.setdefault(b.code, []).append(b)
        return out

    async def get_index_kline(self, market, code, period, *, start: int = 0,
                              count: int = 800) -> list[SecurityBar]:
        """已废弃 -> :meth:`get_bars`（指数自动识别，无需单独接口）。"""
        _deprecated("get_index_kline", "get_bars")
        return await self.get_bars(format_symbol(int(market), code), period,
                                   start=start, count=count)

    async def get_stock_kline_with_indicators(self, market, code, indicators, *,
                                              period: KlinePeriod = KlinePeriod.DAY,
                                              count: int = 30,
                                              adjust: Adjust | None = None,
                                              params: dict | None = None) -> dict:
        """已废弃 -> :meth:`get_indicators`。"""
        _deprecated("get_stock_kline_with_indicators", "get_indicators")
        sets = await self.get_indicators(format_symbol(int(market), code), indicators,
                                         period=period, count=count, adjust=adjust,
                                         params=params)
        one = sets[0]
        return {"bars": one.bars, "indicators": one.indicators}

    async def get_stock_quotes(self, stocks, bits=None) -> list[SecurityQuote]:
        """已废弃 -> :meth:`get_quotes`（``fields=bits``）。"""
        _deprecated("get_stock_quotes", "get_quotes")
        return await self.get_quotes([format_symbol(int(m), c) for m, c in stocks],
                                     fields=bits)

    async def get_stock_quotes_list(self, category: int, *, start: int = 0,
                                    count: int = 80, sort_type: int = 0
                                    ) -> list[SecurityQuote]:
        """已废弃 -> :meth:`get_board_members`（分类码 = 板块码口径）。"""
        _deprecated("get_stock_quotes_list", "get_board_members")
        return await self._board_members_raw(category, start=start, count=count,
                                             sort_type=sort_type)

    async def get_transactions(self, market, code, *, date: int | None = None,
                               start: int = 0, count: int = 2000
                               ) -> list[TransactionRecord]:
        """已废弃 -> :meth:`get_ticks`。"""
        _deprecated("get_transactions", "get_ticks")
        return await self.get_ticks(format_symbol(int(market), code), date=date,
                                    start=start, count=count)

    async def get_minute(self, market, code, *, date: int | None = None
                         ) -> list[MinuteBar]:
        """已废弃 -> :meth:`get_minutes`。"""
        _deprecated("get_minute", "get_minutes")
        return await self.get_minutes(format_symbol(int(market), code), date=date)

    async def get_minute_batch(self, stocks, *, date: int | None = None,
                               concurrency: int = 6) -> dict[str, list[MinuteBar]]:
        """已废弃 -> :meth:`get_minutes`（返回值由 ``{code: bars}`` 变为平铺列表）。"""
        _deprecated("get_minute_batch", "get_minutes")
        bars = await self.get_minutes([format_symbol(int(m), c) for m, c in stocks],
                                      date=date, concurrency=concurrency)
        out: dict[str, list[MinuteBar]] = {}
        for b in bars:
            out.setdefault(b.code, []).append(b)
        return out

    async def get_chart_sampling(self, market, code) -> list[float]:
        """已废弃 -> :meth:`get_minutes`（``sampling=True``）。"""
        _deprecated("get_chart_sampling", "get_minutes(sampling=True)")
        bars = await self.get_minutes(format_symbol(int(market), code), sampling=True)
        return [b.price for b in bars]

    async def get_security_count(self, market) -> int:
        """已废弃 -> ``len(get_universe(...))``。"""
        _deprecated("get_security_count", "get_universe")
        return await self._security_count(int(market))

    async def get_security_list(self, market, *, start: int = 0,
                                count: int | None = None) -> list[SecurityInfo]:
        """已废弃 -> :meth:`get_universe`（本地切片 ``[start:start+count]``）。"""
        _deprecated("get_security_list", "get_universe")
        items = await self.get_universe(market_name(int(market)))
        return items[start:] if count is None else items[start:start + count]

    async def get_security_list_all(self, market) -> list[SecurityInfo]:
        """已废弃 -> :meth:`get_universe`。"""
        _deprecated("get_security_list_all", "get_universe")
        return await self.get_universe(market_name(int(market)))

    async def get_financial_file_list(self) -> list[str]:
        """已废弃 -> :meth:`get_financial_file_infos`（返回 ``FinancialFileInfo``）。"""
        _deprecated("get_financial_file_list", "get_financial_file_infos")
        raw = await self.get_file("tdxfin/gpcw.txt")
        text = raw.decode("gbk", errors="replace")
        return [l.strip() for l in text.splitlines() if l.strip()]

    async def get_financial_records(self, filename: str) -> bytes:
        """已废弃 -> :meth:`get_financial_records_parsed`。"""
        _deprecated("get_financial_records", "get_financial_records_parsed")
        return await self.get_file(filename)

    async def get_company_info_category(self, market, code) -> list:
        """已废弃 -> :meth:`get_f10`。"""
        _deprecated("get_company_info_category", "get_f10")
        return await self.get_f10(format_symbol(int(market), code))

    async def get_company_info_content(self, market, code, filename: str,
                                       offset: int = 0, length: int = 65535) -> str:
        """已废弃 -> :meth:`get_f10`。"""
        _deprecated("get_company_info_content", "get_f10")
        return await self.get_f10(format_symbol(int(market), code), filename,
                                  offset=offset, length=length)

    async def get_block_info(self, filename: str) -> bytes:
        """已废弃 -> :meth:`get_file`。"""
        _deprecated("get_block_info", "get_file")
        return await self.get_file(filename)

    async def get_report_file(self, filename: str) -> bytes:
        """已废弃 -> :meth:`get_file`。"""
        _deprecated("get_report_file", "get_file")
        return await self.get_file(filename)

    async def get_file_meta(self, filename: str):
        """已废弃 -> :meth:`get_file`（直接取内容）。"""
        _deprecated("get_file_meta", "get_file")
        return await self._execute_mac(MacFileListCmd(filename))

    async def download_file(self, filename: str, filesize: int = 0) -> bytes:
        """已废弃 -> :meth:`get_file`。"""
        _deprecated("download_file", "get_file")
        return await self.get_file(filename)

    async def get_board_list(self, board_type: int | str = 0,
                             count: int = 10000) -> list[BoardInfo]:
        """已废弃 -> :meth:`get_boards`。"""
        _deprecated("get_board_list", "get_boards")
        return await self.get_boards(board_type, count=count)

    async def get_belong_board(self, market, code) -> list[BelongBoard]:
        """已废弃 -> :meth:`get_board_of`。"""
        _deprecated("get_belong_board", "get_board_of")
        return await self.get_board_of(format_symbol(int(market), code))

    async def get_symbol_info(self, market, code) -> SymbolSnapshot | None:
        """已废弃 -> :meth:`get_snapshot`（旧版返回单个对象）。"""
        _deprecated("get_symbol_info", "get_snapshot")
        items = await self.get_snapshot(format_symbol(int(market), code))
        return items[0] if items else None

    async def get_goods_list(self, market, start: int = 0,
                             count: int = 100) -> list[SecurityInfo]:
        """已废弃 -> :meth:`get_universe`（扩展到 ``SecurityInfo`` 统一模型）。"""
        _deprecated("get_goods_list", "get_universe")
        items = await self._universe_ex(int(market))
        return items[start:start + count]

    async def get_goods_count(self, market: int | None = None) -> int:
        """已废弃 -> ``len(get_universe(...))``。"""
        _deprecated("get_goods_count", "get_universe")
        if market is None:
            return len(await self._ex_all())
        return len(await self._universe_ex(int(market)))

    async def get_goods_quotes(self, stocks, bits=None) -> list[SecurityQuote]:
        """已废弃 -> :meth:`get_quotes`。"""
        _deprecated("get_goods_quotes", "get_quotes")
        return await self.get_quotes([format_symbol(int(m), c) for m, c in stocks],
                                     fields=bits)

    async def get_goods_quotes_list(self, market, count: int = 80
                                    ) -> list[SecurityQuote]:
        """已废弃 -> :meth:`get_quotes`（或 ``get_universe`` 取清单后自选）。"""
        _deprecated("get_goods_quotes_list", "get_quotes")
        items = await self._universe_ex(int(market))
        if not items:
            return []
        return await self.get_quotes([it.symbol for it in items[:count]])

    async def get_goods_chart_sampling(self, market, code) -> list[float]:
        """已废弃 -> :meth:`get_minutes`（``sampling=True``）。"""
        _deprecated("get_goods_chart_sampling", "get_minutes(sampling=True)")
        bars = await self.get_minutes(format_symbol(int(market), code), sampling=True)
        return [b.price for b in bars]

    async def get_goods_tick_chart(self, market, code,
                                   ymd: int = 0) -> list[MinuteBar]:
        """已废弃 -> :meth:`get_minutes`（扩展到统一分时模型）。"""
        _deprecated("get_goods_tick_chart", "get_minutes")
        return await self.get_minutes(format_symbol(int(market), code),
                                      date=ymd or None)

    async def get_goods_transaction(self, market, code, *, ymd: int = 0,
                                    start: int = 0, count: int = 1800
                                    ) -> list[TransactionRecord]:
        """已废弃 -> :meth:`get_ticks`（扩展到统一逐笔模型）。"""
        _deprecated("get_goods_transaction", "get_ticks")
        return await self.get_ticks(format_symbol(int(market), code),
                                    date=ymd or None, start=start, count=count)

    async def get_goods_transaction_all(self, market, code,
                                        ymd: int = 0) -> list[TransactionRecord]:
        """已废弃 -> :meth:`get_ticks`（``count=None`` 即全量翻页）。"""
        _deprecated("get_goods_transaction_all", "get_ticks(count=None)")
        return await self.get_ticks(format_symbol(int(market), code),
                                    date=ymd or None, count=None)

    async def _board_members_raw(self, board_code: int, *, start: int = 0,
                                 count: int = 80, sort_type: int = 0
                                 ) -> list[SecurityQuote]:
        """按原始板块/分类码取成分报价（旧分类报价接口用）。"""
        items: list[SecurityQuote] = []
        offset = start
        while len(items) < count:
            batch = await self._execute_mac(MacBoardMembersQuotesCmd(
                board_code, sort_type=sort_type, start=offset,
                page_size=min(_QUOTE_PAGE, count - len(items))))
            if not batch:
                break
            items.extend(_quote_from_member(m) for m in batch)
            offset += len(batch)
            if len(batch) < _QUOTE_PAGE:
                break
        return items


