"""可复现核查：指数成分逐笔汇总的口径是否与官方日线一致。

用法::

    python scripts/verify_index_stats.py                      # 当日
    python scripts/verify_index_stats.py --date 20261021      # 历史某日
    python scripts/verify_index_stats.py --indices 000001 000300

两段校验：

1. **单股级**：``Σ(逐笔价 × 手 × 100)`` 对**当日日线 amount**。比值 1.0000~1.0002
   说明逐笔没漏条、价格与单位都对（``bj`` 也行）。
2. **指数级**：成分股合计成交额对**该指数的日线 amount**。相等说明成分清单与
   指数的量能口径一致；明显偏小则是成分表滞后（新上市未纳入）。

   ⚠️ **深市 `399xxx` 的指数 amount 是「全板块」口径**，不是样本股：实测
   `sz399001` 5081.40 亿 = 深市 2908 只股票合计，`sz399006` 2568.60 亿 = 创业板
   全部股票合计。所以这两个指数的比值天然远离 1（约 0.43），**不是数据错**。

为什么不用快照做基准：快照只有「此刻」，历史日期对不上。日线 amount 两者都能给。

注意 ``bj``（北交所）逐笔**没有笔数字段**（``trade_count`` 恒 0），脚本会打出
``有笔数`` 列便于识别。
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import date as _date


def _ymd(bar) -> int:
    return bar.year * 10000 + bar.month * 100 + bar.day


async def _daily_amount(td, symbol: str, ymd: int) -> float | None:
    from pytdxdata import KlinePeriod

    bars = await td.get_bars(symbol, KlinePeriod.DAY, count=10)
    for b in bars:
        if _ymd(b) == ymd:
            return b.amount
    return None


async def check_symbols(td, symbols: list[str], ymd: int) -> None:
    print(f"## 单股级（{ymd}）\n")
    print("| 标的 | Σ(p×手×100) / 日线amount | 笔数 |")
    print("|---|---|---|")
    for sym in symbols:
        try:
            ticks = await td.get_ticks(sym, date=ymd)
            ref = await _daily_amount(td, sym, ymd)
        except Exception as exc:  # noqa: BLE001
            print(f"| {sym} | ERR {type(exc).__name__} | - |")
            continue
        if not ticks:
            print(f"| {sym} | 无逐笔（非交易日？） | - |")
            continue
        sa = sum(t.price * t.vol * 100 for t in ticks)
        tc = sum(t.trade_count for t in ticks)
        ratio = sa / ref if ref else 0.0
        print(f"| {sym} | {ratio:.4f} | {tc:,} |")
    print()


async def check_indices(td, indices: list[str], ymd: int, day: int | None) -> None:
    print(f"## 指数级（{ymd}）\n")
    print("| 指数 | 成分 | 有成交 | 有笔数 | 成分合计 | 日线amount | 比值 | 耗时 |")
    print("|---|---|---|---|---|---|---|---|")
    for idx in indices:
        try:
            st = await td.get_index_trade_stats(idx, day)
            ref = await _daily_amount(td, st.index, ymd)
        except Exception as exc:  # noqa: BLE001
            print(f"| {idx} | ERR {type(exc).__name__}: {exc} | - | - | - | - | - | - |")
            continue
        ratio = st.total_amount / ref if ref else 0.0
        print(f"| {st.index} | {st.member_count} | {st.active_members} | "
              f"{st.trade_count_members} | {st.total_amount / 1e8:,.2f} 亿 | "
              f"{(ref or 0) / 1e8:,.2f} 亿 | {ratio:.4f} | {st.elapsed:.1f}s |")
    print()
    print("提示：`399xxx` 的比值天然远离 1 —— 深市指数的 amount 字段是全板块口径。")
    print()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", nargs="*",
                    default=["sh600000", "sz000001", "sh601398"])
    ap.add_argument("--indices", nargs="*", default=["000016", "000300"])
    ap.add_argument("--date", default=None, help="YYYYMMDD（缺省=当日）")
    ap.add_argument("--skip-symbols", action="store_true")
    args = ap.parse_args()

    from pytdxdata import TdxData

    ymd = int(args.date) if args.date else int(_date.today().strftime("%Y%m%d"))
    day = int(args.date) if args.date else None
    async with TdxData(cache_dir=None, timeout=10.0) as td:
        if not args.skip_symbols:
            await check_symbols(td, args.symbols, ymd)
        if args.indices:
            await check_indices(td, args.indices, ymd, day)


if __name__ == "__main__":
    asyncio.run(main())
