"""pytdxdata CLI 工具

用法:
  tdx quotes sz000001                    # 行情报价(五档)
  tdx kline sz000001 --period day --count 30 --adjust qfq
  tdx kline hk00700 --count 30           # 港股K线
  tdx kline cffex:IFL0 --count 30        # 期货K线(郑商所:zz / 大商所:dl / 上期所:sh / 中金所:cffex / 广期所:gz)
  tdx transactions sz000001 --count 50   # 逐笔成交
  tdx minute sz000001                    # 分时(今日/历史--date)
  tdx index 000001 --count 30            # 指数K线
  tdx board list [--type 1]              # 板块列表(0全部/1行业/2概念)
  tdx board members 881001 --count 20    # 板块成分
  tdx board ranking [--top 10]           # 板块涨跌幅排行
  tdx board change --days 5 --top 10     # 板块N日涨幅排行
  tdx info sz000001                      # 个股特征快照
  tdx list [--market sz] [--count 100]   # 证券列表
  tdx auction sz000001                   # 集合竞价
  tdx unusual [--market sh] [--count 30] # 市场异动
  tdx xdxr sz000001                      # 除权除息
  tdx finance sz000001                   # 财务快照
  tdx flow sz000001                      # 资金流向
  tdx server-info                        # 服务器交易时段
  tdx market-stat                        # 市场统计
  tdx block [block_gn.dat] [--limit N]   # 板块文件下载+解析(成分股)
  tdx industry [codes...] [--search 词]  # 行业分类下载+解析(tdxhy.cfg)
  tdx financial-list                     # 历史专业财报文件索引(gpcw.txt)
  tdx financial gpcw20260331.zip         # 历史专业财报记录下载+解析(gpcw*.zip)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime

from .api import TdxData
from dataclasses import asdict

from .models import Adjust, ExMarket, KlinePeriod, Market

PERIODS = {
    "min1": KlinePeriod.MIN_1, "min5": KlinePeriod.MIN_5, "min15": KlinePeriod.MIN_15,
    "min30": KlinePeriod.MIN_30, "min60": KlinePeriod.MIN_60,
    "day": KlinePeriod.DAY, "week": KlinePeriod.WEEK, "month": KlinePeriod.MONTH,
    "year": KlinePeriod.YEAR, "season": KlinePeriod.SEASON,
}
ADJUSTS = {"none": Adjust.NONE, "qfq": Adjust.QFQ, "hfq": Adjust.HFQ}
MARKET_NAMES = {"sz": Market.SZ, "sh": Market.SH, "bj": Market.BJ}
EX_MARKETS = {
    "hk": ExMarket.HK_MAIN_BOARD, "us": ExMarket.US_STOCK,
    "zz": ExMarket.ZZ_FUTURES, "dl": ExMarket.DL_FUTURES,
    "shf": ExMarket.SH_FUTURES, "cffex": ExMarket.CFFEX_FUTURES, "gz": ExMarket.GZ_FUTURES,
}


class Symbol:
    """解析后的标的: kind=cn(标准) / ex(扩展市场) / index(指数)"""

    def __init__(self, kind: str, market: int, code: str):
        self.kind = kind
        self.market = market
        self.code = code

    def to_market(self) -> Market:
        return Market(self.market)


def parse_symbol(sym: str) -> Symbol:
    """sz000001 / sh600000 / bj920992 / hk00700 / usAAPL / cffex:IFL0 / 000001(指数)"""
    sym = sym.strip()
    if ":" in sym:
        mkt, code = sym.split(":", 1)
        mkt = mkt.lower()
        if mkt in EX_MARKETS:
            return Symbol("ex", int(EX_MARKETS[mkt]), code.upper())
        if mkt in MARKET_NAMES:
            return Symbol("cn", int(MARKET_NAMES[mkt]), code.upper())
        raise ValueError("未知市场前缀: %s (cn: sz/sh/bj; ex: hk/us/zz/dl/shf/cffex/gz)" % mkt)
    prefix, code = sym[:2].lower(), sym[2:]
    if prefix in MARKET_NAMES and len(code) == 6:
        return Symbol("cn", int(MARKET_NAMES[prefix]), code.upper())
    if prefix == "hk" and len(code) == 5:
        return Symbol("ex", int(ExMarket.HK_MAIN_BOARD), code.upper())
    if prefix == "us":
        return Symbol("ex", int(ExMarket.US_STOCK), code.upper())
    if len(sym) == 6 and sym.isdigit():
        return Symbol("index", int(Market.SH), sym.upper())
    raise ValueError("无法识别标的: %s" % sym)


# ------------------------------------------------------------------ #
# 打印辅助
# ------------------------------------------------------------------ #

def _fmt_num(v: float) -> str:
    if v >= 1e8:
        return "%.2f亿" % (v / 1e8)
    if v >= 1e4:
        return "%.2f万" % (v / 1e4)
    return str(int(v))


def _pct(price: float, pre: float) -> str:
    if not pre:
        return ""
    return "%+.2f%%" % ((price / pre - 1) * 100)


# ------------------------------------------------------------------ #
# 各子命令
# ------------------------------------------------------------------ #

async def cmd_quotes(td: TdxData, symbols: list[str]) -> None:
    cn, ex = [], []
    for sym in symbols:
        s = parse_symbol(sym)
        if s.kind == "cn":
            cn.append((Market(s.market), s.code))
        else:
            ex.append((s.market, s.code))
    for q in await td.get_quotes(cn):
        _print_cn_quote(q)
    if ex:
        from .protocol.commands.ex_proto import GetExInstrumentQuoteCmd
        for market, code in ex:
            q = await td._execute_ex(GetExInstrumentQuoteCmd(market, code))
            _print_ex_quote(market, code, q)


def _print_cn_quote(q) -> None:
    pct = (q.price / q.pre_close - 1) * 100 if q.pre_close else 0
    print("=" * 58)
    print(" %s  %s" % (q.code, q.server_time or datetime.now().strftime("%H:%M:%S")))
    print(" 最新 %-10.2f  涨跌 %+8.2f (%+.2f%%)" % (q.price, q.price - q.pre_close, pct))
    print(" 今开 %-10.2f  最高 %.2f  最低 %.2f" % (q.open, q.high, q.low))
    print(" 昨收 %-10.2f  成交量 %s  成交额 %s" % (q.pre_close, _fmt_num(q.vol), _fmt_num(q.amount)))
    if q.bid and q.ask:
        print(" " + "-" * 56)
        for i in range(max(len(q.bid), len(q.ask))):
            b = q.bid[i] if i < len(q.bid) else (0, 0)
            a = q.ask[i] if i < len(q.ask) else (0, 0)
            print(" 买%-1d %8.2f %10s  |  卖%-1d %8.2f %10s" % (
                i + 1, b[0], _fmt_num(b[1]), i + 1, a[0], _fmt_num(a[1])))
    print()


def _print_ex_quote(market: int, code: str, q: dict | None) -> None:
    if not q:
        print(" %s: 无报价数据" % code)
        return
    price, pre = q.get("price") or 0, q.get("pre_close") or 0
    mkt = {31: "港股", 74: "美股"}.get(market, "扩展")
    print("=" * 58)
    print(" %s %s" % (mkt, code))
    print(" 最新 %-10.2f  涨跌 %+8.2f (%s)" % (price, price - pre, _pct(price, pre)))
    print(" 今开 %-10.2f  最高 %.2f  最低 %.2f" % (
        q.get("open") or 0, q.get("high") or 0, q.get("low") or 0))
    print(" 昨收 %-10.2f  总量 %s" % (pre, _fmt_num(q.get("zongliang") or 0)))
    print()


async def cmd_kline(td: TdxData, sym: str, period: KlinePeriod, count: int,
                    start: int, adjust: Adjust) -> None:
    s = parse_symbol(sym)
    if s.kind == "cn":
        bars = await td.get_kline(Market(s.market), s.code, period, start=start,
                                  count=count, adjust=adjust)
    elif s.kind == "index":
        bars = await td.get_index_kline(Market(s.market), s.code, period, start=start, count=count)
    else:
        bars = await td.get_kline(ExMarket(s.market), s.code, period, start=start, count=count)
    print("%s K线 (%s, %d根):" % (sym, period.name, len(bars)))
    print("  %-12s %8s %8s %8s %8s %12s" % ("日期", "开盘", "最高", "最低", "收盘", "成交量"))
    for b in bars:
        dt = "%04d-%02d-%02d" % (b.year, b.month, b.day)
        if b.hour:
            dt += " %02d:%02d" % (b.hour, b.minute)
        print("  %-12s %8.2f %8.2f %8.2f %8.2f %12s" % (
            dt, b.open, b.high, b.low, b.close, _fmt_num(b.vol)))


async def cmd_transactions(td: TdxData, sym: str, count: int, date: int | None) -> None:
    s = parse_symbol(sym)
    tx = await td.get_transactions(Market(s.market), s.code, date=date, count=count)
    print("%s 逐笔成交 (%d条):" % (sym, len(tx)))
    print("  时间       价格     成交量   方向")
    for t in tx:
        bs = "买" if getattr(t, "bs_flag", 0) == 1 else "卖" if getattr(t, "bs_flag", 0) == 2 else "-"
        tc = getattr(t, "trade_count", 0)
        print("  %-10s %8.2f %10s   %s%s" % (t.time, t.price, _fmt_num(t.vol), bs,
                                             " (%d笔)" % tc if tc else ""))


async def cmd_minute(td: TdxData, sym: str, date: int | None) -> None:
    s = parse_symbol(sym)
    bars = await td.get_minute(Market(s.market), s.code, date=date)
    print("%s 分时 (%d条):" % (sym, len(bars)))
    print("  时间       价格     均价    成交量")
    for i, b in enumerate(bars):
        print("  %02d:%02d %8.2f %8.2f %10s" % (
            i // 60, i % 60, b.price, b.avg_price, _fmt_num(b.vol)))


async def cmd_board(td: TdxData, args) -> None:
    if args.action == "list":
        items = await td.get_board_list(args.type, args.count)
        print("板块列表 (%d条, type=%d):" % (len(items), args.type))
        print("  %-10s %-16s %10s %10s" % ("代码", "名称", "涨跌幅%", "成交额"))
        for b in items:
            name = getattr(b, "name", "")
            code = getattr(b, "code", "")
            chg = getattr(b, "change_pct", None)
            amt = getattr(b, "amount", None)
            print("  %-10s %-16s %10s %10s" % (
                code, name, ("%.2f" % chg) if chg is not None else "-",
                _fmt_num(amt) if amt else "-"))
    elif args.action == "members":
        items = await td.get_board_members(args.symbol, count=args.count)
        print("%s 成分股 (%d条):" % (args.symbol, len(items)))
        for m in items:
            f = getattr(m, "fields", {})
            price = f.get("0x4") or f.get("price") or 0
            chg = f.get("0x2e")
            print("  %-10s %-10s 价=%.2f%s" % (
                m.code, m.name[:6], price, " 涨%.2f%%" % chg if chg else ""))
    elif args.action == "ranking":
        rows = await td.get_board_ranking(args.type, args.top)
        print("板块涨跌幅排行 (type=%d, top%d):" % (args.type, args.top))
        for r in rows:
            print("  %-12s %+8.2f%%" % (r.get("name", ""), r.get("change_pct", 0)))
    elif args.action == "change":
        rows = await td.get_board_change_ranking(args.type, args.days, args.top)
        print("板块%d日涨幅排行 (top%d):" % (args.days, args.top))
        for r in rows:
            print("  %-12s %+8.2f%%" % (r.get("name", ""), r.get("change_pct", 0)))


async def cmd_info(td: TdxData, sym: str) -> None:
    s = parse_symbol(sym)
    info = await td.get_symbol_info(Market(s.market), s.code)
    if isinstance(info, dict):
        items = info.items()
    else:
        items = asdict(info).items()
    for k, v in items:
        print("  %-24s %s" % (k, v))


async def cmd_list(td: TdxData, market: str, count: int) -> None:
    m = MARKET_NAMES[market]
    n = await td.get_security_count(m)
    items = await td.get_security_list(m, count=count)
    print("%s市场证券数: %d, 前%d条:" % (market.upper(), n, len(items)))
    for it in items:
        print("  %s  %s" % (it.code, it.name.strip()))


async def cmd_auction(td: TdxData, sym: str) -> None:
    s = parse_symbol(sym)
    items = await td.get_auction(Market(s.market), s.code)
    print("%s 集合竞价 (%d条):" % (sym, len(items)))
    for a in items:
        print("  %s  价格=%.3f  量=%d  %s" % (
            getattr(a, "time", ""), getattr(a, "price", 0),
            getattr(a, "volume", 0), getattr(a, "side", "")))


async def cmd_unusual(td: TdxData, market: str, count: int) -> None:
    items = await td.get_unusual(MARKET_NAMES[market], count=count)
    print("市场异动 (%d条):" % len(items))
    for u in items:
        print("  %s %s %s" % (getattr(u, "code", ""), getattr(u, "name", ""),
                              getattr(u, "describe", "") or getattr(u, "type", "")))


async def cmd_xdxr(td: TdxData, sym: str) -> None:
    s = parse_symbol(sym)
    items = await td.get_xdxr(Market(s.market), s.code)
    print("%s 除权除息 (%d条):" % (sym, len(items)))
    for x in items[:20]:
        print("  %s  送%g 配%g 派%g" % (
            getattr(x, "year", ""), getattr(x, "songzhuangu", 0) or 0,
            getattr(x, "peigu", 0) or 0, getattr(x, "peixian", 0) or 0))


async def cmd_finance(td: TdxData, sym: str) -> None:
    s = parse_symbol(sym)
    f = await td.get_finance(Market(s.market), s.code)
    if not f:
        print("无财务数据")
        return
    for k, v in asdict(f).items():
        print("  %-24s %s" % (k, v))


async def cmd_flow(td: TdxData, sym: str) -> None:
    s = parse_symbol(sym)
    f = await td.get_capital_flow(Market(s.market), s.code)
    if isinstance(f, dict):
        items = f.items()
    else:
        items = asdict(f).items()
    for k, v in items:
        print("  %-20s %s" % (k, v))


_BLOCK_CATEGORY = {0: "行业", 1: "地域", 2: "概念", 3: "风格"}


async def cmd_block(td: TdxData, filename: str, limit: int, show_codes: bool) -> None:
    blocks = await td.get_block_parsed(filename)
    print("%s 板块文件 (%d 个板块):" % (filename, len(blocks)))
    if not blocks:
        return
    print("  %-4s %-14s %6s  %s" % ("类别", "板块名", "成分数", "代码示例"))
    shown = blocks if limit <= 0 else blocks[:limit]
    for b in shown:
        cat = _BLOCK_CATEGORY.get(getattr(b, "category", -1), "?")
        codes = getattr(b, "codes", None) or []
        count = getattr(b, "count", len(codes))
        sample = " ".join(codes[:5]) + (" ..." if len(codes) > 5 else "")
        print("  %-4s %-14s %6d  %s" % (cat, getattr(b, "name", ""), count, sample))
        if show_codes and codes:
            print("        " + " ".join(codes))
    if 0 < limit < len(blocks):
        print("  ... 其余 %d 个板块省略（--limit 0 显示全部）" % (len(blocks) - limit))


async def cmd_industry(td: TdxData, codes: list[str], limit: int, search: str | None) -> None:
    hy = await td.get_industry_map()
    if codes:
        print("行业分类 (共 %d 只):" % len(hy))
        for c in codes:
            info = hy.get(c)
            if info is None:
                print("  %-8s 未收录" % c)
            else:
                print("  %-8s 通达信=%-12s 申万=%s" % (
                    c, info.tdx_industry or "-", info.sw_industry or "-"))
        return
    rows = list(hy.items())
    if search:
        rows = [(c, i) for c, i in rows
                if search in (i.tdx_industry or "") or search in (i.sw_industry or "")]
    tip = "，匹配 '%s' %d 条" % (search, len(rows)) if search else ""
    print("行业分类 tdxhy.cfg (共 %d 只%s):" % (len(hy), tip))
    print("  %-8s %-14s %s" % ("代码", "通达信行业", "申万行业"))
    shown = rows if limit <= 0 else rows[:limit]
    for c, i in shown:
        print("  %-8s %-14s %s" % (c, i.tdx_industry or "-", i.sw_industry or "-"))
    if 0 < limit < len(rows):
        print("  ... 其余 %d 条省略（--limit 0 显示全部）" % (len(rows) - limit))


async def cmd_financial_list(td: TdxData, limit: int) -> None:
    infos = await td.get_financial_file_infos()
    print("历史专业财报文件 (gpcw.txt, 共 %d 个):" % len(infos))
    if not infos:
        return
    print("  %-24s %-34s %12s" % ("文件名", "MD5", "大小"))
    shown = infos if limit <= 0 else infos[:limit]
    for i in shown:
        print("  %-24s %-34s %12d" % (i.filename, i.hash, i.filesize))
    if 0 < limit < len(infos):
        print("  ... 其余 %d 个省略（--limit 0 显示全部）" % (len(infos) - limit))
    print("\n  提示: 用 `tdx financial %s --limit 5` 查看最新一期财报" % infos[-1].filename)


async def cmd_financial(td: TdxData, filename: str, code: str | None,
                        limit: int, fields: int) -> None:
    records = await td.get_financial_records_parsed(filename)
    if code:
        records = [r for r in records if r.code == code]
    print("%s 财报记录 (%d 条):" % (filename, len(records)))
    if not records:
        return
    shown = records if limit <= 0 else records[:limit]
    for r in shown:
        sample = " ".join("%g" % v for v in r.fields[:fields]) if fields > 0 else ""
        print("  %-8s 市场=%s  报告期=%s  字段数=%d  %s" % (
            r.code, "沪" if r.market == 1 else "深",
            r.report_date or "-", len(r.fields), sample))
    if 0 < limit < len(records):
        print("  ... 其余 %d 条省略（--limit 0 显示全部）" % (len(records) - limit))


# ------------------------------------------------------------------ #
# 入口
# ------------------------------------------------------------------ #

def main() -> None:
    ap = argparse.ArgumentParser(prog="tdx", description="通达信行情CLI (pytdxdata)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, help):
        return sub.add_parser(name, help=help)

    q = add("quotes", "行情报价")
    q.add_argument("symbols", nargs="+")
    q.set_defaults(func=lambda td, a: cmd_quotes(td, a.symbols))

    k = add("kline", "K线")
    k.add_argument("symbol")
    k.add_argument("--period", choices=list(PERIODS), default="day")
    k.add_argument("--count", type=int, default=30)
    k.add_argument("--start", type=int, default=0)
    k.add_argument("--adjust", choices=list(ADJUSTS), default="none")
    k.set_defaults(func=lambda td, a: cmd_kline(
        td, a.symbol, PERIODS[a.period], a.count, a.start, ADJUSTS[a.adjust]))

    t = add("transactions", "逐笔成交")
    t.add_argument("symbol")
    t.add_argument("--count", type=int, default=30)
    t.add_argument("--date", type=str, default=None)
    t.set_defaults(func=lambda td, a: cmd_transactions(
        td, a.symbol, a.count, int(a.date) if a.date else None))

    m = add("minute", "分时")
    m.add_argument("symbol")
    m.add_argument("--date", type=str, default=None)
    m.set_defaults(func=lambda td, a: cmd_minute(td, a.symbol, int(a.date) if a.date else None))

    ix = add("index", "指数K线")
    ix.add_argument("code")
    ix.add_argument("--count", type=int, default=30)
    ix.set_defaults(func=lambda td, a: cmd_kline(
        td, a.code, KlinePeriod.DAY, a.count, 0, Adjust.NONE))

    b = add("board", "板块")
    b.add_argument("action", choices=["list", "members", "ranking", "change"])
    b.add_argument("symbol", nargs="?", default="")
    b.add_argument("--type", type=int, default=1)
    b.add_argument("--count", type=int, default=30)
    b.add_argument("--top", type=int, default=10)
    b.add_argument("--days", type=int, default=20)
    b.set_defaults(func=lambda td, a: cmd_board(td, a))

    i = add("info", "个股特征快照")
    i.add_argument("symbol")
    i.set_defaults(func=lambda td, a: cmd_info(td, a.symbol))

    ls = add("list", "证券列表")
    ls.add_argument("--market", choices=list(MARKET_NAMES), default="sz")
    ls.add_argument("--count", type=int, default=30)
    ls.set_defaults(func=lambda td, a: cmd_list(td, a.market, a.count))

    au = add("auction", "集合竞价")
    au.add_argument("symbol")
    au.set_defaults(func=lambda td, a: cmd_auction(td, a.symbol))

    un = add("unusual", "市场异动")
    un.add_argument("--market", choices=list(MARKET_NAMES), default="sh")
    un.add_argument("--count", type=int, default=30)
    un.set_defaults(func=lambda td, a: cmd_unusual(td, a.market, a.count))

    xd = add("xdxr", "除权除息")
    xd.add_argument("symbol")
    xd.set_defaults(func=lambda td, a: cmd_xdxr(td, a.symbol))

    fn = add("finance", "财务快照")
    fn.add_argument("symbol")
    fn.set_defaults(func=lambda td, a: cmd_finance(td, a.symbol))

    fl = add("flow", "资金流向")
    fl.add_argument("symbol")
    fl.set_defaults(func=lambda td, a: cmd_flow(td, a.symbol))

    bk = add("block", "板块文件 下载+解析 (block_*.dat)")
    bk.add_argument("filename", nargs="?", default="block_gn.dat",
                    help="板块文件名（默认 block_gn.dat；常用 block_zs.dat 行业 / block_fg.dat 风格）")
    bk.add_argument("--limit", type=int, default=30, help="最多显示板块数（0=全部）")
    bk.add_argument("--codes", action="store_true", help="显示每个板块完整成分股代码")
    bk.set_defaults(func=lambda td, a: cmd_block(td, a.filename, a.limit, a.codes))

    ind = add("industry", "行业分类 下载+解析 (tdxhy.cfg)")
    ind.add_argument("codes", nargs="*", help="查询指定 6 位代码（缺省列出全部）")
    ind.add_argument("--limit", type=int, default=30, help="最多显示条数（0=全部）")
    ind.add_argument("--search", default=None, help="按行业名关键词过滤")
    ind.set_defaults(func=lambda td, a: cmd_industry(td, a.codes, a.limit, a.search))

    ff = add("financial-list", "历史专业财报文件索引 下载+解析 (gpcw.txt)")
    ff.add_argument("--limit", type=int, default=30, help="最多显示条数（0=全部）")
    ff.set_defaults(func=lambda td, a: cmd_financial_list(td, a.limit))

    fr = add("financial", "历史专业财报记录 下载+解析 (gpcw*.zip)")
    fr.add_argument("filename", help="如 gpcw20260331.zip（用 financial-list 查看可用文件）")
    fr.add_argument("--code", default=None, help="只显示指定 6 位股票代码")
    fr.add_argument("--limit", type=int, default=5, help="最多显示条数（0=全部）")
    fr.add_argument("--fields", type=int, default=4, help="每条显示前 N 个原始字段（0=不显示）")
    fr.set_defaults(func=lambda td, a: cmd_financial(td, a.filename, a.code, a.limit, a.fields))

    si = add("server-info", "服务器交易时段")
    si.set_defaults(func=_server_info)

    ms = add("market-stat", "市场统计")
    ms.set_defaults(func=_market_stat)

    args = ap.parse_args()

    async def run() -> None:
        td = TdxData(cache_dir=None, pool_min=2, pool_max=6)
        await td.start()
        try:
            await args.func(td, args)
        finally:
            await td.close()

    try:
        asyncio.run(run())
    except ValueError as e:
        print(e)
        sys.exit(1)


async def _server_info(td: TdxData, args) -> None:
    await _print_obj(await td.get_server_info())


async def _market_stat(td: TdxData, args) -> None:
    await _print_obj(await td.get_market_stat())


async def _print_obj(obj) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            print("  %-24s %s" % (k, v))
    elif hasattr(obj, "_fields"):
        for f in obj._fields:
            print("  %-24s %s" % (f, getattr(obj, f)))
    elif hasattr(obj, "__dataclass_fields__"):
        for k, v in asdict(obj).items():
            print("  %-24s %s" % (k, v))
    elif hasattr(obj, "__slots__"):
        for k in obj.__slots__:
            print("  %-24s %s" % (k, getattr(obj, k)))
    else:
        print(obj)


if __name__ == "__main__":
    main()
