"""统一对外接口 TdxData：标准/MAC自动选路 + 自动缓存 + 自动并发分页"""
from __future__ import annotations

import asyncio
import logging
from datetime import date as _date, datetime
from pathlib import Path
from typing import Sequence

from .cache.disk import DiskCache
from .cache.key import CacheKey
from .cache.manager import CacheManager
from .cache.memory import MemoryCache
from .codec.payload import pack_payload, unpack_payload
from .config import get_ex_hosts, get_mac_hosts, get_standard_hosts
from .models import (
    Adjust,
    ExMarket,
    FinanceRecord,
    FinancialFileInfo,
    FinancialRecord,
    IndustryInfo,
    KlinePeriod,
    Market,
    MinuteBar,
    SecurityBar,
    SecurityInfo,
    SecurityQuote,
    TdxBlock,
    TransactionRecord,
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
    GetTransactionDataCmd,
    GetXdxrInfoCmd,
    KLINE_PAGE,
    LIST_PAGE,
    MacKlineCmd,
    MacTransactionCmd,
    MAX_QUOTES,
)
from .router import route_for
from .scheduler.paginator import PageSpec, Paginator

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


class TdxData:
    """通达信行情数据统一入口。

    用法::

        async with TdxData() as td:
            bars = await td.get_kline(1, "600000", KlinePeriod.DAY, count=2400)
            q = await td.get_quotes([(1, "600000"), (0, "000001")])
            t = await td.get_transactions(0, "000001", date=20260811)
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
            health=HealthEngine(),
        )
        from .protocol.commands.mac import MacKlineCmd
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
        from .config import EX_HANDSHAKES
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

    # ------------------------------------------------------------------ #
    # K线
    # ------------------------------------------------------------------ #

    async def get_kline(
        self, market: Market, code: str, period: KlinePeriod, *,
        start: int = 0, count: int = 800, adjust: Adjust | None = None,
        start_date: datetime | None = None, end_date: datetime | None = None,
    ) -> list[SecurityBar]:
        """获取K线（count>页大小自动并发分页；adjust!=NONE 走MAC复权通道）。

        参数:
            market: 市场 (Market.SH / Market.SZ)
            code: 股票代码
            period: K线周期
            start: 起始偏移量（从最早K线开始的索引，默认0）
            count: 获取条数（默认800）
            adjust: 复权方式（默认使用实例 default_adjust）
            start_date: 起始日期（与 start/count 互斥，优先使用日期过滤）
            end_date: 截止日期（可选，配合 start_date 使用）

        增量更新示例:
            # 获取 2026-01-01 之后的所有日K线
            bars = await client.get_kline(Market.SH, "600000", KlinePeriod.DAY,
                                          start_date=datetime(2026, 1, 1))

            # 获取 2026-01-01 至 2026-06-30 之间的日K线
            bars = await client.get_kline(Market.SH, "600000", KlinePeriod.DAY,
                                          start_date=datetime(2026, 1, 1),
                                          end_date=datetime(2026, 6, 30))

            # 便捷方法：获取指定日期之后的所有K线
            bars = await client.get_kline_since(Market.SH, "600000",
                                                KlinePeriod.DAY, datetime(2026, 1, 1))
        """
        # 日期过滤模式：获取全量数据后按日期筛选
        if start_date is not None or end_date is not None:
            return await self._get_kline_by_date(
                market, code, period, start_date, end_date, adjust)

        adj = self.default_adjust if adjust is None else adjust
        # EX扩展市场（港股/期货）：market为ExMarket值，走ex池
        if isinstance(market, ExMarket) or int(market) not in (0, 1, 2):
            # EX通道统一用pytdx EX协议(0x23FF): category=K线周期(与TDXParams一致, DAY=4)
            from .protocol.commands.ex_proto import GetExInstrumentBarsCmd
            cat = int(period)
            fetch_count = max(700, start + count)
            key = CacheKey("kline", int(market), code, period.name, start, fetch_count,
                           f"{adj.name}@{cat}")
            async def fetch() -> bytes:
                result = await self._fetch_paginated_ex(
                    lambda s2, c2: GetExInstrumentBarsCmd(int(market), code, cat, s2, c2),
                    PageSpec(page_size=700, total=fetch_count, min_page=100))
                bars = []
                for d in result:
                    bars.append(SecurityBar(
                        market=int(market), code=code,
                        year=d["year"], month=d["month"], day=d["day"],
                        hour=d.get("hour", 0), minute=d.get("minute", 0),
                        open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                        vol=float(d.get("trade", 0) or 0), amount=0.0,
                    ))
                return pack_payload(bars)
            bars = unpack_payload(await self._cache.get_or_fetch(key, fetch))
            # EX 0x23FF 按时间升序返回(老→新)：start=0 取最新 count 根
            if count < len(bars):
                return bars[-count:] if start == 0 else bars[start:start + count]
            return bars

        route = route_for("kline", adjust=adj, period=period)
        key = CacheKey("kline", int(market), code, period.name, start, count, adj.name)

        def make_cmd(s: int, c: int):
            if route == "mac":
                return MacKlineCmd(int(market), code, period, adj, s, c)
            # 指数类(88开头板块指数 / 沪市000指数 / 深市399指数)走指数K线命令(多4字节涨跌家数)
            # 注意: 深市股票000001.SZ(market=0)不是指数, 必须加market判断
            mk = int(market)
            is_index = code.startswith("88") or (
                code.startswith("000") and mk == int(Market.SH)) or (
                code.startswith("399") and mk == int(Market.SZ))
            if is_index:
                return GetIndexBarsCmd(mk, code, period, s, c)
            return GetSecurityBarsCmd(mk, code, period, s, c)

        if route == "mac":
            payload = await self._cache.get_or_fetch(
                key, lambda: self._fetch_mac_kline_serial(
                    make_cmd, start + count, start=start, page_size=700,
                )
            )
        else:
            payload = await self._cache.get_or_fetch(
                key, lambda: self._fetch_paginated(route, make_cmd, PageSpec(
                    page_size=KLINE_PAGE, total=start + count, min_page=100,
                    start_offset=start,
                ))
            )
        bars = unpack_payload(payload)
        # K线按时间正序（无论 start 是否 0 都排序，避免依赖协议层顺序保证）
        bars.sort(key=lambda b: (b.year, b.month, b.day, b.hour, b.minute))
        return bars

    async def _get_kline_by_date(
        self, market: Market, code: str, period: KlinePeriod,
        start_date: datetime | None, end_date: datetime | None,
        adjust: Adjust | None,
    ) -> list[SecurityBar]:
        """按日期范围获取K线（内部方法）。

        高效策略：TDX K线按时间正序返回，start 是偏移量。用二分查找在
        O(log N) 次单条探测内定位日期对应的偏移量，再只拉取目标区间。
        优化点：按周期预估上界省掉指数探测；仅 start_date 时合并为单次
        二分；需同时查 start/end 日期时用 asyncio.gather 并行。
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

        # 按周期预估总条数上界（保守值，二分中越界自然修正）
        est = _KLINE_MAX_BARS.get(period, 10_000)

        # 仅 start_date 的常见增量场景：单次二分定位起点，再取剩余全部
        if start_dt is not None and end_dt is None:
            lo = await self._probe_kline_lower_bound(
                market, code, period, adjust, est, start_dt)
            # 命中上界说明实际条数可能超出预估，逐步扩大范围直到定位成功
            MAX_EXPAND = 5  # 最多扩大 2^5 = 32 倍
            for _ in range(MAX_EXPAND):
                if lo < est:
                    break
                if await self._probe_kline_bar(
                        market, code, period, adjust, est) is None:
                    break  # est 处已无数据，说明 lo 就是总数上限
                est *= 2
                lo = await self._probe_kline_lower_bound(
                    market, code, period, adjust, est, start_dt)
            if lo >= est:
                return []
            return await self.get_kline(
                market, code, period, start=lo, count=est - lo, adjust=adjust)

        # 需要 end_date 时，并行二分起点和终点
        tasks = []
        if start_dt is not None:
            tasks.append(self._probe_kline_lower_bound(
                market, code, period, adjust, est, start_dt))
        if end_dt is not None:
            tasks.append(self._probe_kline_upper_bound(
                market, code, period, adjust, est, end_dt))
        results = await asyncio.gather(*tasks) if tasks else []

        lo = results[0] if start_dt is not None else 0
        hi = results[-1] if end_dt is not None else est
        if start_dt is not None and end_dt is not None:
            lo, hi = results[0], results[1]

        # 命中上界时逐步扩大范围重试（与上面相同的回退逻辑）
        MAX_EXPAND = 5  # 最多扩大 2^5 = 32 倍
        for _ in range(MAX_EXPAND):
            need_expand = False
            if start_dt is not None and lo >= est:
                if await self._probe_kline_bar(
                        market, code, period, adjust, est) is not None:
                    need_expand = True
            if end_dt is not None and hi >= est:
                if await self._probe_kline_bar(
                        market, code, period, adjust, est) is not None:
                    need_expand = True
            if not need_expand:
                break
            est *= 2
            tasks2 = []
            if start_dt is not None:
                tasks2.append(self._probe_kline_lower_bound(
                    market, code, period, adjust, est, start_dt))
            if end_dt is not None:
                tasks2.append(self._probe_kline_upper_bound(
                    market, code, period, adjust, est, end_dt))
            results2 = await asyncio.gather(*tasks2)
            lo = results2[0] if start_dt is not None else 0
            hi = results2[-1] if end_dt is not None else est
            if start_dt is not None and end_dt is not None:
                lo, hi = results2[0], results2[1]

        if lo >= hi:
            return []
        bars = await self.get_kline(
            market, code, period, start=lo, count=hi - lo, adjust=adjust)
        # min_page 可能导致多拉，做精确日期裁剪
        if start_dt is not None:
            bars = [b for b in bars if b.datetime >= start_dt]
        if end_dt is not None:
            bars = [b for b in bars if b.datetime <= end_dt]
        return bars

    async def _probe_kline_lower_bound(
        self, market: Market, code: str, period: KlinePeriod,
        adjust: Adjust | None, total: int, target: datetime,
    ) -> int:
        """二分查找第一个 datetime >= target 的偏移量（在 [0, total] 内）。"""
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi) // 2
            bar = await self._probe_kline_bar(market, code, period, adjust, mid)
            if bar is None:
                hi = mid              # 越界，数据边界在左侧
            elif bar.datetime < target:
                lo = mid + 1          # 太早，向右
            else:
                hi = mid              # 命中，向左收缩
        return lo

    async def _probe_kline_upper_bound(
        self, market: Market, code: str, period: KlinePeriod,
        adjust: Adjust | None, total: int, target: datetime,
    ) -> int:
        """二分查找第一个 datetime > target 的偏移量（在 [0, total] 内）。"""
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi) // 2
            bar = await self._probe_kline_bar(market, code, period, adjust, mid)
            if bar is None:
                hi = mid              # 越界，数据边界在左侧
            elif bar.datetime <= target:
                lo = mid + 1          # 还在范围内，向右
            else:
                hi = mid              # 超出，向左收缩
        return lo

    async def _probe_kline_bar(
        self, market: Market, code: str, period: KlinePeriod,
        adjust: Adjust | None, offset: int,
    ) -> SecurityBar | None:
        """探测指定偏移量处的单条K线（越界返回 None）。"""
        adj = self.default_adjust if adjust is None else adjust
        route = route_for("kline", adjust=adj, period=period)
        if route == "mac":
            cmd = MacKlineCmd(int(market), code, period, adj, offset, 1)
            try:
                bars = await self._execute_mac(cmd)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                return None
        else:
            mk = int(market)
            is_index = code.startswith("88") or (
                code.startswith("000") and mk == int(Market.SH)) or (
                code.startswith("399") and mk == int(Market.SZ))
            cmd = (GetIndexBarsCmd(mk, code, period, offset, 1)
                   if is_index else GetSecurityBarsCmd(mk, code, period, offset, 1))
            try:
                bars = await self._execute_standard(cmd)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                return None
        return bars[0] if bars else None

    async def get_kline_since(
        self, market: Market, code: str, period: KlinePeriod,
        since: datetime, adjust: Adjust | None = None,
    ) -> list[SecurityBar]:
        """获取指定日期之后的所有K线（增量更新便捷方法）。

        参数:
            since: 起始日期（含），只返回 datetime >= since 的K线

        增量更新示例:
            # 获取2026年1月1日之后的所有日K线
            bars = await client.get_kline_since(
                Market.SH, "600000", KlinePeriod.DAY, datetime(2026, 1, 1))

            # 增量更新：本地已有数据截止到 last_date，拉取新数据
            last_date = datetime(2026, 8, 19)
            new_bars = await client.get_kline_since(
                Market.SH, "600000", KlinePeriod.DAY, last_date)
            # 注意：结果包含 last_date 当天的K线，追加时需去重
        """
        return await self._get_kline_by_date(
            market, code, period, since, None, adjust)

    async def get_kline_batch(
        self, stocks: list[tuple[Market, str]], period: KlinePeriod, *,
        start: int = 0, count: int = 800, adjust: Adjust | None = None,
        concurrency: int = 6,
    ) -> dict[str, list[SecurityBar]]:
        """批量获取K线，自动并发不同服务器。返回 {code: bars}。"""
        sem = asyncio.Semaphore(concurrency)

        async def _fetch(mkt: Market, code: str):
            async with sem:
                return code, await self.get_kline(mkt, code, period, start=start, count=count, adjust=adjust)

        results = await asyncio.gather(*[_fetch(m, c) for m, c in stocks])
        return {code: bars for code, bars in results}

    async def get_index_kline(
        self, market: Market, code: str, period: KlinePeriod, *,
        start: int = 0, count: int = 800,
    ) -> list[SecurityBar]:
        key = CacheKey("index_kline", int(market), code, period.name, start, count, "")

        def make_cmd(s: int, c: int):
            return GetIndexBarsCmd(int(market), code, period, s, c)

        payload = await self._cache.get_or_fetch(
            key, lambda: self._fetch_paginated("standard", make_cmd, PageSpec(
                page_size=KLINE_PAGE, total=start + count, min_page=100,
            ))
        )
        return unpack_payload(payload)

    # ------------------------------------------------------------------ #
    # 报价
    # ------------------------------------------------------------------ #

    async def get_quotes(self, stocks: Sequence[tuple[Market, str]]) -> list[SecurityQuote]:
        """批量报价（>80自动切批，缓存键=规范化股票集，TTL 5s）。"""
        stocks = list(stocks)
        if not stocks:
            return []
        key = CacheKey.quote_key(stocks)
        payload = await self._cache.get_or_fetch(
            key, lambda: self._fetch_quotes(stocks)
        )
        return unpack_payload(payload)

    async def _fetch_quotes(self, stocks: list[tuple[Market, str]]) -> bytes:
        out: list[SecurityQuote] = []
        for i in range(0, len(stocks), MAX_QUOTES):
            batch = stocks[i:i + MAX_QUOTES]
            cmds = GetSecurityQuotesCmd([(int(m), c) for m, c in batch])
            result = await self._execute_standard(cmds)
            out.extend(result)
        return pack_payload(out)

    # ------------------------------------------------------------------ #
    # 逐笔成交（MAC通道，含成交笔数）
    # ------------------------------------------------------------------ #

    async def get_transactions(
        self, market: Market, code: str, *,
        date: int | None = None, start: int = 0, count: int = 2000,
    ) -> list[TransactionRecord]:
        """获取逐笔成交（MAC 0x122F，每条含 trade_count=成交笔数）。"""
        key = CacheKey("transaction", int(market), code, "", start, count,
                       "" if date is None else str(date))

        def make_cmd(s: int, c: int):
            return MacTransactionCmd(int(market), code, date or 0, s, c)

        payload = await self._cache.get_or_fetch(
            key, lambda: self._fetch_paginated("mac", make_cmd, PageSpec(
                page_size=1000, total=start + count, min_page=100,
            ))
        )
        return unpack_payload(payload)

    # ------------------------------------------------------------------ #
    # 证券列表
    # ------------------------------------------------------------------ #

    async def get_security_count(self, market: Market) -> int:
        key = CacheKey("count", int(market))

        async def fetch() -> bytes:
            n = await self._execute_standard(GetSecurityCountCmd(int(market)))
            return pack_payload(n)

        payload = await self._cache.get_or_fetch(key, fetch)
        return unpack_payload(payload)

    async def get_security_list(self, market: Market, *,
                                start: int = 0, count: int | None = None) -> list[SecurityInfo]:
        """分页获取证券列表（count=None=全部，先查总数避免死循环）。"""
        if count is None:
            count = await self.get_security_count(market)
        total = count
        key = CacheKey("list", int(market), "", "", start, total or 0, "")

        def make_cmd(s: int, c: int):
            return GetSecurityListCmd(int(market), s)

        spec = PageSpec(page_size=LIST_PAGE, total=total, stop_when_short=True)

        async def fetch() -> bytes:
            return await self._fetch_paginated("standard", make_cmd, spec)

        payload = await self._cache.get_or_fetch(key, fetch)
        return unpack_payload(payload)

    # ------------------------------------------------------------------ #
    # 分时 / 除权除息 / 财务
    # ------------------------------------------------------------------ #

    async def get_minute(self, market: Market, code: str, *,
                         date: int | None = None) -> list[MinuteBar]:
        """获取分时（date=None=今天；已切换为 MAC 通道 0x122D，可获取完整历史分时）。"""
        if date is None:
            d = _date.today()
            date = d.year * 10000 + d.month * 100 + d.day
        key = CacheKey("minute", int(market), code, "", 0, 0, str(date))
        payload = await self._cache.get_or_fetch(
            key, lambda: self._fetch_minute(market, code, date)
        )
        return unpack_payload(payload)

    async def _fetch_minute(self, market, code: str, date: int) -> bytes:
        from .protocol.commands.mac_tick import MacTickChartCmd
        cmd = MacTickChartCmd(int(market), code, date)
        result = await self._execute_mac(cmd)
        return pack_payload(result)

    async def get_minute_batch(
        self, stocks: list[tuple[Market, str]], *,
        date: int | None = None, concurrency: int = 6,
    ) -> dict[str, list[MinuteBar]]:
        """批量获取分时，自动并发不同服务器。返回 {code: bars}。"""
        sem = asyncio.Semaphore(concurrency)

        async def _fetch(mkt: Market, code: str):
            async with sem:
                return code, await self.get_minute(mkt, code, date=date)

        results = await asyncio.gather(*[_fetch(m, c) for m, c in stocks])
        return {code: bars for code, bars in results}

    async def get_xdxr(self, market: Market, code: str) -> list[XdxrRecord]:
        key = CacheKey("xdxr", int(market), code)

        async def fetch() -> bytes:
            recs = await self._execute_standard(GetXdxrInfoCmd(int(market), code))
            return pack_payload(recs)

        payload = await self._cache.get_or_fetch(key, fetch)
        return unpack_payload(payload)

    async def get_finance(self, market: Market, code: str) -> FinanceRecord | None:
        key = CacheKey("finance", int(market), code)

        async def fetch() -> bytes:
            rec = await self._execute_standard(GetFinanceInfoCmd(int(market), code))
            return pack_payload(rec)

        payload = await self._cache.get_or_fetch(key, fetch)
        return unpack_payload(payload)

    # ------------------------------------------------------------------ #
    # 内部执行
    # ------------------------------------------------------------------ #

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

    async def _fetch_mac_kline_serial(self, make_cmd, total: int, *,
                                      start: int = 0,
                                      page_size: int = 700) -> bytes:
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

    async def _fetch_paginated_ex(self, make_cmd, spec: PageSpec) -> list:
        """EX通道分页拉取（串行逐页，扩展市场页数少）。"""
        result = []
        start = 0
        for page in range(spec.max_pages if spec.max_pages else 64):
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

    # ------------------------------------------------------------------ #
    # MAC 扩展接口：板块/竞价/异动/快照/资金流/服务器/扩展市场
    # ------------------------------------------------------------------ #

    async def get_board_list(self, board_type: int = 0, count: int = 10000) -> list:
        """板块列表（board_type: 0=全部 1=行业 2=概念）。"""
        from .protocol.commands.mac_ext import MacBoardListCmd
        items: list = []
        start = 0
        while start < count:
            batch = await self._execute_mac(MacBoardListCmd(board_type, start, 150))
            if not batch:
                break
            items.extend(batch)
            start += len(batch) // 2
            if len(batch) < 150:
                break
        return items

    async def get_board_members(self, board_symbol: str, count: int = 100000,
                                sort_type: int = 0) -> list:
        """板块成分股报价（board_symbol 如 881001）。"""
        from .protocol.commands.mac_ext import MacBoardMembersQuotesCmd
        board_code = self._convert_board_code(board_symbol)
        items: list = []
        start = 0
        while start < count:
            batch = await self._execute_mac(
                MacBoardMembersQuotesCmd(board_code, sort_type=sort_type, start=start, page_size=80))
            if not batch:
                break
            items.extend(batch)
            start += len(batch)
            if len(batch) < 80:
                break
        return items

    async def get_belong_board(self, market: Market, code: str) -> list:
        """个股所属板块。"""
        from .protocol.commands.mac_ext import MacBelongBoardCmd
        return await self._execute_mac(MacBelongBoardCmd(int(market), code))

    async def get_auction(self, market: Market, code: str) -> list:
        """集合竞价。"""
        from .protocol.commands.mac_ext import MacAuctionCmd
        return await self._execute_mac(MacAuctionCmd(int(market), code))

    async def get_unusual(self, market: Market, start: int = 0, count: int = 600) -> list:
        """市场异动。"""
        from .protocol.commands.mac_ext import MacUnusualCmd
        return await self._execute_mac(MacUnusualCmd(int(market), start, count))

    async def get_symbol_info(self, market: Market, code: str):
        """个股特征快照。"""
        from .protocol.commands.mac_ext import MacSymbolInfoCmd
        return await self._execute_mac(MacSymbolInfoCmd(int(market), code))

    async def _ex_find_market_offset(self, market: int) -> int:
        """二分定位指定市场在全局商品列表中的起始偏移。"""
        from .protocol.commands.ex_ext import (
            GetExInstrumentCountCmd, GetExInstrumentInfoCmd)
        total = await self._execute_ex(GetExInstrumentCountCmd())
        if total == 0:
            return -1
        lo, hi = 0, total
        while lo < hi:
            mid = (lo + hi) // 2
            items = await self._execute_ex(GetExInstrumentInfoCmd(0, mid, 1))
            if not items:
                hi = mid
                continue
            m = int(items[0]["market"])
            if m < market:
                lo = mid + 1
            else:
                hi = mid
        return lo

    async def get_goods_list(self, market: int, start: int = 0,
                             count: int = 100) -> list[dict]:
        """扩展市场商品列表（EX通道 0x23F5，二分定位市场边界+过滤）。"""
        from .protocol.commands.ex_ext import (
            GetExInstrumentCountCmd, GetExInstrumentInfoCmd)
        offset = await self._ex_find_market_offset(market)
        if offset < 0:
            return []
        items: list[dict] = []
        pos = offset + start
        total = await self._execute_ex(GetExInstrumentCountCmd())
        while pos < total and len(items) < count:
            page = await self._execute_ex(GetExInstrumentInfoCmd(0, pos, 1000))
            if not page:
                break
            for g in page:
                if int(g["market"]) == market:
                    items.append(g)
                    if len(items) >= count:
                        break
                elif int(g["market"]) > market:
                    return items
            pos += len(page)
        return items

    async def get_goods_count(self, market: int | None = None) -> int:
        """扩展市场商品数量（EX通道 0x23F0）。
        market=None 返回全市场总数；指定市场=二分定位偏移+扫描计数。"""
        from .protocol.commands.ex_ext import (
            GetExInstrumentCountCmd, GetExInstrumentInfoCmd)
        total = await self._execute_ex(GetExInstrumentCountCmd())
        if market is None:
            return int(total)
        offset = await self._ex_find_market_offset(market)
        if offset < 0:
            return 0
        n = 0
        pos = offset
        while pos < total:
            page = await self._execute_ex(GetExInstrumentInfoCmd(0, pos, 1000))
            if not page:
                break
            for g in page:
                m = int(g["market"])
                if m == market:
                    n += 1
                elif m > market:
                    return n
            pos += len(page)
        return n

    async def get_goods_quotes(self, stocks: list[tuple[int, str]],
                               bits: list[int] | None = None) -> list[MemberQuote]:
        """扩展市场报价（EX通道 0x122B，stocks=[(ExMarket, code)]）。"""
        from .protocol.commands.mac_ext import MacSymbolQuotesCmd
        return await self._execute_ex(MacSymbolQuotesCmd(stocks, bits))

    async def get_goods_quotes_list(self, market: int, count: int = 80) -> list[MemberQuote]:
        """扩展市场报价列表（goods_list + 报价组合）。"""
        goods = await self.get_goods_list(market, 0, min(count, 80))
        if not goods:
            return []
        stocks = [(int(g["market"] or market), g["code"]) for g in goods[:80]]
        return await self.get_goods_quotes(stocks)

    async def get_goods_chart_sampling(self, market: int, code: str) -> list[float]:
        """扩展市场分时采样（EX通道 0x254D）。"""
        from .protocol.commands.mac_ext import MacChartSamplingCmd
        return await self._execute_ex(MacChartSamplingCmd(market, code))

    async def get_goods_tick_chart(self, market: int, code: str,
                                   ymd: int = 0) -> list[dict]:
        """扩展市场分时tick（EX通道 0x122D）。"""
        from .protocol.commands.ex_ext import SymbolTickChartCmd
        return await self._execute_ex(SymbolTickChartCmd(market, code, ymd))

    async def get_goods_transaction(self, market: int, code: str, *,
                                    ymd: int = 0, start: int = 0,
                                    count: int = 1800) -> list[dict]:
        """扩展市场逐笔（港股0x23FC/0x2406 16B/条；其他0x122F 18B/条）。"""
        hk_markets = {27, 31, 48, 49, 71, 98}
        if market in hk_markets:
            from .protocol.commands.ex_ext import (
                GetExHistoryTransactionDataCmd, GetExTransactionDataCmd)
            if ymd:
                return await self._execute_ex(
                    GetExHistoryTransactionDataCmd(ymd, market, code, start, count))
            return await self._execute_ex(
                GetExTransactionDataCmd(market, code, start, count))
        from .protocol.commands.transaction import MacTransactionCmd
        items = await self._execute_ex(
            MacTransactionCmd(market, code, ymd, start, count))
        return [dict(t._asdict()) if hasattr(t, "_asdict") else t for t in items]

    async def get_goods_transaction_all(self, market: int, code: str,
                                        ymd: int = 0) -> list[dict]:
        """扩展市场（仅港股）全部逐笔，自动翻页（最多50页/9万条）。"""
        hk_markets = {27, 31, 48, 49, 71, 98}
        if market not in hk_markets:
            raise ValueError("仅港股支持全量逐笔（get_goods_transaction_all）")
        out: list[dict] = []
        start = 0
        for _ in range(50):
            page = await self.get_goods_transaction(market, code, ymd=ymd,
                                                    start=start, count=1800)
            if not page:
                break
            out.extend(page)
            start += len(page)
            if len(page) < 1800:
                break
        return out

    async def get_server_info(self):
        """服务器交易时段信息。"""
        from .protocol.commands.mac_ext import MacServerInfoCmd
        return await self._execute_mac(MacServerInfoCmd())

    async def get_kline_offset(self, offset: int = 0, count: int = 1) -> tuple[int, int]:
        """K线偏移信息。"""
        from .protocol.commands.mac_ext import MacKlineOffsetCmd
        return await self._execute_mac(MacKlineOffsetCmd(offset, count))

    async def get_capital_flow(self, market: Market, code: str):
        """个股资金流向。"""
        from .protocol.commands.mac_ext import MacCapitalFlowCmd
        return await self._execute_mac(MacCapitalFlowCmd(int(market), code))

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

    # ------------------------------------------------------------------ #
    # 标准通道扩展：F10/板块文件/市场统计/涨跌停价
    # ------------------------------------------------------------------ #

    async def get_company_info_category(self, market: Market, code: str) -> list:
        """F10目录。"""
        from .protocol.commands.std_ext import GetCompanyInfoCategoryCmd
        return await self._execute_standard(GetCompanyInfoCategoryCmd(market, code))

    async def get_company_info_content(self, market: Market, code: str,
                                       filename: str, offset: int = 0,
                                       length: int = 65535) -> str:
        """F10内容（分块读取）。"""
        from .protocol.commands.std_ext import GetCompanyInfoContentCmd
        return await self._execute_standard(
            GetCompanyInfoContentCmd(market, code, filename, offset, length))

    async def get_block_info(self, filename: str) -> bytes:
        """板块文件（先查元数据再下载全部）。"""
        from .protocol.commands.std_ext import GetBlockInfoCmd, GetBlockInfoMetaCmd
        meta = await self._execute_standard(GetBlockInfoMetaCmd(filename))
        if meta is None:
            return b""
        data = bytearray()
        pos = 0
        chunk = 30000
        while pos < meta.size:
            part = await self._execute_standard(GetBlockInfoCmd(filename, pos, chunk))
            if not part:
                break
            data.extend(part)
            pos += len(part)
            if len(part) < chunk:
                break
        return bytes(data)

    async def get_price_limits(self, market: Market, code: str, name: str,
                               pre_close: float, listed_days: int = 9999) -> tuple[float | None, float | None]:
        """涨跌停价（本地计算）。"""
        from .protocol.commands.std_ext import compute_price_limits
        return compute_price_limits(market, code, name, pre_close, listed_days)

    async def get_market_stat(self):
        """全市场统计（用报价查上证指数880005/880001/880006组合）。"""
        from .protocol.commands.std_ext import MarketStat
        # 上证指数代码
        q = await self.get_quotes([(Market.SH, "880005"), (Market.SH, "880001"),
                                   (Market.SH, "880006")])
        stat = MarketStat()
        qmap = {q2.code: q2 for q2 in q}
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

    # ------------------------------------------------------------------ #
    # MAC 自定义字段报价 / 分类列表
    # ------------------------------------------------------------------ #

    async def get_stock_quotes(self, stocks: list[tuple[Market, str]],
                               bits: list[int] | None = None) -> list:
        """MAC自定义字段报价（≤80只/次，bits=FieldBit列表）。"""
        from .protocol.commands.mac_ext import MacSymbolQuotesCmd
        out: list = []
        for i in range(0, len(stocks), 80):
            batch = [(int(m), c) for m, c in stocks[i:i + 80]]
            out.extend(await self._execute_mac(MacSymbolQuotesCmd(batch, bits)))
        return out

    async def get_stock_quotes_list(self, category: int, *, start: int = 0,
                                    count: int = 80, sort_type: int = 0) -> list:
        """市场分类报价列表（category: 0=沪A 2=深A 6=全A 8=科创 14=创业板等）。"""
        from .protocol.commands.mac_ext import MacBoardMembersQuotesCmd
        items: list = []
        offset = start
        while len(items) < count:
            batch = await self._execute_mac(
                MacBoardMembersQuotesCmd(category, sort_type=sort_type,
                                         start=offset, page_size=min(80, count - len(items))))
            if not batch:
                break
            items.extend(batch)
            offset += len(batch)
            if len(batch) < 80:
                break
        return items

    # ------------------------------------------------------------------ #
    # 远程文件
    # ------------------------------------------------------------------ #

    async def get_file_meta(self, filename: str):
        """远程文件元信息（MAC 0x1215）。"""
        from .protocol.commands.mac_ext import MacFileListCmd
        return await self._execute_mac(MacFileListCmd(filename))

    async def download_file(self, filename: str, filesize: int = 0) -> bytes:
        """下载完整远程文件（MAC 0x1217，分块）。"""
        from .protocol.commands.mac_ext import MacFileDownloadCmd
        if filesize <= 0:
            meta = await self.get_file_meta(filename)
            filesize = meta.size if meta else 0
        data = bytearray()
        pos = 0
        idx = 1
        chunk = 30000
        while pos < filesize:
            part = await self._execute_mac(
                MacFileDownloadCmd(filename, idx, pos, chunk))
            if not part:
                break
            data.extend(part)
            pos += len(part)
            idx += 1
            if len(part) < chunk:
                break
        return bytes(data)

    async def get_report_file(self, filename: str) -> bytes:
        """下载报告/财务文件（标准 0x06B9，分块）。"""
        from .protocol.commands.std_ext import GetReportFileCmd
        data = bytearray()
        pos = 0
        chunk = 30000
        while True:
            part = await self._execute_standard(GetReportFileCmd(filename, pos, chunk))
            if not part:
                break
            data.extend(part)
            pos += len(part)
            if len(part) < chunk:
                break
        return bytes(data)

    async def get_financial_file_list(self) -> list[str]:
        """财务文件列表（tdxfin/gpcw.txt，返回 文件名,md5,size 行列表）。"""
        raw = await self.get_report_file("tdxfin/gpcw.txt")
        text = raw.decode("gbk", errors="replace")
        return [l.strip() for l in text.splitlines() if l.strip()]

    async def get_financial_records(self, filename: str) -> bytes:
        """财务文件内容（tdxfin/gpcw*.zip，返回原始字节）。"""
        return await self.get_report_file(filename)

    # ------------------------------------------------------------------ #
    # 服务器文件解析（板块 / 行业分类 / 历史专业财报）
    # ------------------------------------------------------------------ #

    async def get_block_parsed(self, filename: str) -> list[TdxBlock]:
        """下载并解析板块文件，返回 TdxBlock 列表。

        常用文件名：``block_zs.dat``（行业）/ ``block_gn.dat``（概念）/ ``block_fg.dat``（风格）。
        category 由文件名推断（0=行业 2=概念 3=风格）。
        """
        from .codec.block import parse_block_dat
        return parse_block_dat(await self.get_block_info(filename), filename)

    async def get_industry_map(self) -> dict[str, IndustryInfo]:
        """下载并解析 tdxhy.cfg，返回 ``{6位代码: IndustryInfo}``（通达信+申万行业）。"""
        from .codec.industry import parse_tdxhy_cfg
        return parse_tdxhy_cfg(await self.get_report_file("tdxhy.cfg"))

    async def get_financial_file_infos(self) -> list[FinancialFileInfo]:
        """下载并解析 tdxfin/gpcw.txt，返回 FinancialFileInfo 列表（文件名/MD5/大小）。"""
        from .codec.financial import parse_financial_file_list
        return parse_financial_file_list(await self.get_report_file("tdxfin/gpcw.txt"))

    async def get_financial_records_parsed(self, filename: str) -> list[FinancialRecord]:
        """下载财报 zip（``tdxfin/gpcw*.zip``）并解压解析为 FinancialRecord 列表。

        报告期优先取文件名中的 8 位日期，缺失时回退到 .dat 头部内嵌值。
        """
        import io
        import re
        import zipfile

        from .codec.financial import parse_financial_dat

        zip_data = await self.get_report_file(filename)
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

    # ------------------------------------------------------------------ #
    # 板块聚合（基于 board_list/members/K线 组合）
    # ------------------------------------------------------------------ #

    async def get_board_summary(self, board_symbol: str) -> dict:
        """板块汇总：成分数/总成交额/总成交量/主力净流入/涨跌家数。"""
        # 需要金额(0x07)与主力净流入(0x38)字段
        bits = list(range(0x00, 0x08)) + [0x38]
        members = await self.get_board_members(board_symbol, count=100000)
        # 重新拉含主力净流入字段（get_board_members 默认不含）
        from .protocol.commands.mac_ext import MacBoardMembersQuotesCmd
        board_code = self._convert_board_code(board_symbol)
        all_m = []
        start = 0
        while start < 100000:
            batch = await self._execute_mac(
                MacBoardMembersQuotesCmd(board_code, sort_type=0, start=start,
                                         page_size=80, bits=bits))
            if not batch:
                break
            all_m.extend(batch)
            start += len(batch)
            if len(batch) < 80:
                break
        amount = sum(m.fields.get("0x7", 0) for m in all_m)
        main_net = sum(m.fields.get("0x38", 0) for m in all_m)
        up = down = 0
        for m in all_m:
            c, pre = m.fields.get("0x4", 0), m.fields.get("0x0", 0)
            if pre and c > pre:
                up += 1
            elif pre and c < pre:
                down += 1
        return {
            "member_count": len(all_m),
            "amount": amount, "main_net_amount": main_net,
            "up_count": up, "down_count": down,
        }

    async def get_board_ranking(self, board_type: int = 1, top_n: int = 20) -> list[dict]:
        """板块涨跌幅排行（行业/概念，基于板块K线计算涨跌幅）。"""
        boards = await self.get_board_list(board_type, count=200)
        # 去重（board_list返回板块+领涨股混合，取板块段）
        seen = {}
        for b in boards:
            if not b.code.startswith("88"):   # 只保留板块段(881xxx), 过滤领涨股
                continue
            if b.code not in seen or len(b.name) < len(seen[b.code].name):
                seen[b.code] = b
        result = []
        for code, b in list(seen.items())[:top_n * 3]:
            if b.pre_close <= 0:
                continue
            chg = (b.price / b.pre_close - 1) * 100
            result.append({"code": code, "name": b.name, "price": b.price,
                           "change_pct": round(chg, 2)})
        result.sort(key=lambda x: x["change_pct"], reverse=True)
        return result[:top_n]

    async def get_board_change_ranking(self, board_type: int = 1, days: int = 20,
                                       top_n: int = 20) -> list[dict]:
        """板块N日涨跌幅排行（用板块指数K线）。"""
        from .models import Adjust, KlinePeriod
        boards = await self.get_board_list(board_type, count=200)
        seen = {}
        for b in boards:
            if not b.code.startswith("88"):   # 只保留板块段, 过滤领涨股
                continue
            if b.code not in seen or len(b.name) < len(seen[b.code].name):
                seen[b.code] = b
        result = []
        for code, b in list(seen.items())[:top_n * 3]:
            try:
                bars = await self.get_kline(1, code, KlinePeriod.DAY, count=days + 5,
                                            adjust=Adjust.NONE)
                if len(bars) < 2:
                    continue
                first, last = bars[0].close, bars[-1].close
                if first <= 1.0:   # 过滤异常基数(板块指数首根过小)
                    continue
                result.append({"code": code, "name": b.name,
                               "close_end": last, "close_start": first,
                               "change_pct": round((last / first - 1) * 100, 2)})
            except Exception:
                continue
        result.sort(key=lambda x: x["change_pct"], reverse=True)
        return result[:top_n]

    # ------------------------------------------------------------------ #
    # 剩余接口：分时采样 / 列表all / K线+指标（带缓存）
    # ------------------------------------------------------------------ #

    async def get_chart_sampling(self, market: Market, code: str) -> list[float]:
        """分时缩略采样价格点（MAC 0x254D）。"""
        from .protocol.commands.mac_ext import MacChartSamplingCmd
        key = CacheKey("minute_sampling", int(market), code)
        async def fetch() -> bytes:
            return pack_payload(await self._execute_mac(MacChartSamplingCmd(int(market), code)))
        return unpack_payload(await self._cache.get_or_fetch(key, fetch))

    async def get_security_list_all(self, market: Market) -> list[SecurityInfo]:
        """全市场证券列表（高并发分页拉取，缓存1天）。

        优化：高并发一次并发拉完所有页。
        """
        key = CacheKey("list_all", int(market))
        async def fetch() -> bytes:
            # 先获取总数（带缓存），精确规划页数
            total = await self.get_security_count(market)
            def make_cmd(s: int, c: int):
                return GetSecurityListCmd(int(market), s)
            spec = PageSpec(page_size=LIST_PAGE, total=total, stop_when_short=True)
            result = await self._fetch_paginated_list(
                "standard", make_cmd, spec,
                concurrency=min(self._std_pool.max_size, 8))
            return pack_payload(result)
        return unpack_payload(await self._cache.get_or_fetch(key, fetch))

    async def get_stock_kline_with_indicators(
        self, market: Market, code: str, indicators: list[str], *,
        period: KlinePeriod = KlinePeriod.DAY, count: int = 30,
        adjust: Adjust | None = None, params: dict | None = None,
    ) -> dict:
        """K线+技术指标（MACD/KDJ/RSI/BOLL/MA/EMA，缓存=复权K线TTL）。"""
        from .indicator import compute_indicators
        adj = self.default_adjust if adjust is None else adjust
        fetch_count = max(120 + count, 200)   # 指标预热
        bars = await self.get_kline(market, code, period, count=fetch_count, adjust=adj)
        if not bars:
            return {"bars": [], "indicators": {}}
        high = [b.high for b in bars]
        low = [b.low for b in bars]
        close = [b.close for b in bars]
        ind = compute_indicators(high, low, close, indicators, params, tail=count)
        return {
            "bars": bars[-count:],
            "indicators": {k: (v[-count:] if len(v) >= count else v) for k, v in ind.items()},
        }
