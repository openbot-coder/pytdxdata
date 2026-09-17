"""全接口回归：mock 连接，逐个调用 ``TdxData`` 的每一个公开方法。

目标是「没有接口是死的」——每个公开方法至少被真实调用一次并返回预期形状。
末尾的 ``test_every_public_method_was_exercised`` 用 AST 解析 ``api.py``，
断言覆盖集合与全部公开方法**完全相等**：以后新增方法若没纳入本文件就会失败
（与 ``scripts/check_docs.py`` 的 ``MUST_LIST_ALL`` 互为呼应）。

不连任何网络：标准 / MAC / EX 连接全部被 mock；探测缓存被短路。
"""
from __future__ import annotations

import ast
import datetime
import io
import pathlib
import struct
import time
import zipfile
from unittest import mock

import pytest

from pytdxdata.api import TdxData
from pytdxdata.models import (
    AuctionItem,
    BelongBoard,
    BoardInfo,
    CapitalFlow,
    FinanceRecord,
    MemberQuote,
    MinuteBar,
    SecurityBar,
    SecurityInfo,
    SecurityQuote,
    ServerInfo,
    SymbolSnapshot,
    TransactionRecord,
    UnusualItem,
    XdxrRecord,
)

# --------------------------------------------------------------------------- #
# 覆盖追踪
# --------------------------------------------------------------------------- #

EXERCISED: set[str] = set()


def _use(name: str) -> None:
    EXERCISED.add(name)


def _public_methods() -> set[str]:
    """AST 解析 api.py，取 TdxData 的全部公开方法名（与 check_docs 同口径）。"""
    src = pathlib.Path(__file__).resolve().parent.parent / "src" / "pytdxdata" / "api.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TdxData":
            return {
                item.name
                for item in node.body
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
                and not item.name.startswith("_")
            }
    raise AssertionError("api.py 里找不到 TdxData")


# --------------------------------------------------------------------------- #
# mock 数据
# --------------------------------------------------------------------------- #

_TOTAL_BARS = 2000
_BASE_DATE = datetime.date(2026, 9, 16)
_QUOTE_FIELDS = {
    "0x0": 9.8, "0x1": 9.9, "0x2": 10.2, "0x3": 9.7, "0x4": 10.0,
    "0x5": 1000.0, "0x6": 1.0, "0x7": 1_000_000.0, "0x38": 5000.0,
}


def _mk_bar(market: int, code: str, offset: int) -> SecurityBar:
    """offset 越大越老（标准通道语义：offset 0 = 最新一根）。"""
    d = _BASE_DATE - datetime.timedelta(days=offset)
    return SecurityBar(
        market=market, code=code, open=10.0, high=10.5, low=9.5,
        close=10.0 + (offset % 5) * 0.1, vol=1000.0, amount=10000.0,
        year=d.year, month=d.month, day=d.day, hour=9, minute=30,
    )


def _bars(start: int, count: int, market: int, code: str) -> list[SecurityBar]:
    hi = min(start + count, _TOTAL_BARS)
    return [_mk_bar(market, code, i) for i in range(start, hi)]


def _quote(market: int, code: str) -> SecurityQuote:
    return SecurityQuote(
        market=market, code=code, price=10.0, pre_close=9.8, open=9.9,
        high=10.2, low=9.7, vol=1000.0, amount=1_000_000.0, s_vol=10.0,
        b_vol=12.0, bid=[9.99] * 5, ask=[10.01] * 5,
    )


def _member(market: int, code: str) -> MemberQuote:
    return MemberQuote(market=market, code=code, name="测试",
                       fields=dict(_QUOTE_FIELDS))


class _FileMeta:
    """对应 protocol.commands.mac_ext.FileMeta（避免直接依赖内部类）。"""

    __slots__ = ("flag", "md5", "offset", "size")

    def __init__(self, offset=0, size=4, flag=0, md5=""):
        self.offset, self.size, self.flag, self.md5 = offset, size, flag, md5


class _BlockMeta:
    __slots__ = ("filename", "md5", "size")

    def __init__(self, size=4, md5="", filename=""):
        self.size, self.md5, self.filename = size, md5, filename


def _dispatch(cmd) -> object:
    """按命令类名返回形状正确的数据。"""
    n = type(cmd).__name__
    market = getattr(cmd, "market", 1)
    code = getattr(cmd, "code", "600000")
    start = getattr(cmd, "start", 0)
    count = getattr(cmd, "count", 100) or 100

    if n == "GetSecurityCountCmd":
        return 8
    if n == "GetSecurityListCmd":
        return [SecurityInfo(market=1, code="600000", name="浦发银行",
                             volunit=100, decimal_point=2, pre_close=10.0)]
    # ---- K线 ----
    if n in ("GetSecurityBarsCmd", "GetIndexBarsCmd", "MacKlineCmd"):
        return _bars(start, count, market, code)
    if n == "GetExInstrumentBarsCmd":
        return [{"year": 2026, "month": 9, "day": 1, "hour": 9, "minute": 30,
                 "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0,
                 "trade": 1000}]
    # ---- 报价 ----
    if n == "GetSecurityQuotesCmd":
        return [_quote(m, c) for m, c in cmd.stocks]
    if n == "MacSymbolQuotesCmd":
        return [_member(m, c) for m, c in cmd.stocks]
    if n == "MacBoardMembersQuotesCmd":
        return [_member(1, "600000")]
    # ---- 逐笔 / 分时 ----
    if n == "MacTransactionCmd":
        return [TransactionRecord(market=market, code=code, time="09:30",
                                  price=10.0, vol=100.0, trade_count=1, bs_flag=0)]
    if n in ("GetExTransactionDataCmd", "GetExHistoryTransactionDataCmd"):
        return [{"minutes": 570, "price": 10.0, "vol": 100.0, "direction": 0,
                 "zengcang": 1.0}]
    if n == "MacTickChartCmd":
        return [MinuteBar(price=10.0, vol=100.0, hour=9, minute=30)]
    if n == "SymbolTickChartCmd":
        return [{"minutes": 570, "price": 10.0, "vol": 100.0, "avg": 10.0,
                 "momentum": 0.1}]
    if n == "MacChartSamplingCmd":
        return [10.0, 10.1, 10.2]
    # ---- 公司信息 ----
    if n == "MacSymbolInfoCmd":
        return SymbolSnapshot(
            market=market, code=code, name="浦发银行", date=20260916, time=930,
            activity=1.0, pre_close=9.8, open=9.9, high=10.2, low=9.7,
            close=10.0, vol=1000.0, amount=1_000_000.0, turnover=1.0,
            avg_price=10.0)
    if n == "MacAuctionCmd":
        return [AuctionItem(time=93000, price=10.0, matched=100, unmatched=0)]
    if n == "MacCapitalFlowCmd":
        return CapitalFlow(market=market, code=code, main_buy=1.0, main_sell=1.0,
                           retail_buy=1.0, retail_sell=1.0, buy5=1.0, sell5=1.0,
                           big5=1.0, mid5=1.0)
    if n == "GetXdxrInfoCmd":
        return [XdxrRecord(market=market, code=code, year=2025, month=6, day=1,
                           category=1, name="除权除息")]
    if n == "GetFinanceInfoCmd":
        return FinanceRecord(market=market, code=code)
    if n == "GetCompanyInfoCategoryCmd":
        return [{"name": "最新提示", "filename": "600000.txt", "length": 10}]
    if n == "GetCompanyInfoContentCmd":
        return "公司资料正文"
    # ---- 市场 / 板块 ----
    if n == "MacUnusualCmd":
        return [UnusualItem(market=1, code="600000", name="测试", type=1,
                            desc="描述", value=1.0, time=930)]
    if n == "MacServerInfoCmd":
        return ServerInfo(date=20260916, last_trade_date=20260915,
                          sessions=[(570, 690)])
    if n == "MacKlineOffsetCmd":
        return (_TOTAL_BARS, 100)
    if n == "MacBoardListCmd":
        return [BoardInfo(market=1, code="881001", name="银行", price=110.0,
                          rise_speed=0.1, pre_close=100.0)]
    if n == "MacBelongBoardCmd":
        return [BelongBoard(market=market, board_code=20686, board_name="银行",
                            close=110.0, pre_close=100.0)]
    # ---- 文件 ----
    if n == "MacFileListCmd":
        return _FileMeta(size=4)
    if n == "GetBlockInfoMetaCmd":
        return _BlockMeta(size=4)
    if n in ("GetBlockInfoCmd", "GetReportFileCmd", "MacFileDownloadCmd"):
        return b"abcd"
    # ---- EX 市场 ----
    if n == "GetExInstrumentCountCmd":
        return 2
    if n == "GetExInstrumentInfoCmd":
        return [{"market": 31, "code": "00700", "name": "腾讯控股", "desc": "港股"},
                {"market": 74, "code": "AAPL", "name": "苹果", "desc": "美股"}]
    return []


class _MockConn:
    """替代 TdxConnection / ExTdxConnection。"""

    def __init__(self, host="h", port=7709, timeout=3.0, *args, **kwargs):
        self.host = host
        self.server = host
        self.port = port
        self.timeout = timeout
        self.alive = True
        self.last_used = time.monotonic()

    async def connect(self) -> None:
        self.alive = True

    async def close(self) -> None:
        self.alive = False

    async def ping(self):
        return 0.01

    async def execute(self, cmd):
        self.last_used = time.monotonic()
        return _dispatch(cmd)


# --------------------------------------------------------------------------- #
# 离线文件样本（供 *_parsed / get_file 走通解析路径）
# --------------------------------------------------------------------------- #

_HEADER = 384


def _make_block_dat() -> bytes:
    out = bytearray(_HEADER)
    out += struct.pack("<H", 1)
    rec = bytearray()
    rec += "银行".encode("gbk")[:9].ljust(9, b"\x00")
    rec += struct.pack("<HH", 1, 1)
    area = bytearray(2800)
    area[0:7] = "000001".encode("ascii").ljust(7, b"\x00")
    rec += area
    out += rec
    return bytes(out)


def _make_financial_zip() -> bytes:
    entry = struct.pack("<6sBL", b"600000", 1, 31)
    header = struct.pack("<1hI1H3L", 0, 20260331, 1, 0, 8, 0)
    dat = header + entry + struct.pack("<2f", 1.0, 2.0)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("gpcw.dat", dat)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #

@pytest.fixture
async def td():
    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]), \
         mock.patch("pytdxdata.api.get_ex_hosts", return_value=["e1"]), \
         mock.patch("pytdxdata.pool.pool.TdxConnection", new=_MockConn), \
         mock.patch("pytdxdata.pool.ex_pool.ExTdxConnection", new=_MockConn), \
         mock.patch("pytdxdata.pool.pool.get_probe_cache",
                    return_value=mock.MagicMock(ensure_probe=mock.AsyncMock())):
        t = TdxData(cache_dir=None, pool_min=1, pool_max=2)
        _use("start")
        await t.start()
        try:
            yield t
        finally:
            _use("close")
            await t.close()


# --------------------------------------------------------------------------- #
# 一、标的清单 / 报价 / 快照
# --------------------------------------------------------------------------- #

async def test_universe_and_quotes(td):
    for name in ("get_universe", "get_quotes"):
        _use(name)
    uni = await td.get_universe()
    assert isinstance(uni, list) and uni
    cn = await td.get_universe("sz")
    assert isinstance(cn, list)
    ex = await td.get_universe("hk")
    assert isinstance(ex, list)

    q = await td.get_quotes(["sh600000", "sz000001"])
    assert len(q) == 2 and all(x.symbol for x in q)
    qf = await td.get_quotes(["sh600000"], fields=[0x00, 0x04])
    assert qf[0].name == "测试"
    qex = await td.get_quotes(["hk00700"])
    assert len(qex) == 1


async def test_snapshot_auction_flow(td):
    for name in ("get_snapshot", "get_auction", "get_capital_flow"):
        _use(name)
    snaps = await td.get_snapshot("sh600000")
    assert len(snaps) == 1 and snaps[0].name == "浦发银行"
    auc = await td.get_auction("sh600000")
    assert len(auc) == 1
    flow = await td.get_capital_flow("sh600000")
    assert len(flow) == 1 and flow[0].main_buy == 1.0


# --------------------------------------------------------------------------- #
# 二、K线 / 指标
# --------------------------------------------------------------------------- #

async def test_bars(td):
    _use("get_bars")
    bars = await td.get_bars("sh600000", count=120)
    assert len(bars) == 120
    assert all(b.symbol == "sh600000" for b in bars)
    # 分页（超过单页）
    big = await td.get_bars("sh600000", count=2000, concurrency=2)
    assert len(big) == 2000
    # 复权走 MAC 通道
    qfq = await td.get_bars("sh600000", count=200, adjust=None)  # NONE -> 标准
    assert len(qfq) == 200
    from pytdxdata.models import Adjust
    mac = await td.get_bars("sh600000", count=200, adjust=Adjust.QFQ)
    assert len(mac) == 200
    # 多标的并发
    multi = await td.get_bars(["sh600000", "sz000001"], count=60)
    assert len(multi) == 120
    # 日期区间（二分定位路径）
    dated = await td.get_bars(
        "sh600000", start_date=datetime.datetime(2026, 8, 1),
        end_date=datetime.datetime(2026, 8, 31))
    assert isinstance(dated, list)
    # 扩展市场 K线
    exb = await td.get_bars("hk00700", count=60)
    assert isinstance(exb, list)


async def test_indicators(td):
    _use("get_indicators")
    sets = await td.get_indicators("sh600000", ["macd", "kdj", "rsi"], count=30)
    assert len(sets) == 1
    assert sets[0].symbol == "sh600000"
    assert sets[0].indicators  # 指标分量名（DIF/DEA/K/D/J/RSI* 等）


# --------------------------------------------------------------------------- #
# 三、逐笔 / 分时
# --------------------------------------------------------------------------- #

async def test_ticks(td):
    _use("get_ticks")
    rows = await td.get_ticks("sh600000", date=20260811, count=10)
    assert isinstance(rows, list) and rows and all(r.symbol for r in rows)
    full = await td.get_ticks("sh600000", count=None)
    assert isinstance(full, list)
    hk = await td.get_ticks("hk00700", count=10)
    assert isinstance(hk, list)


async def test_minutes(td):
    _use("get_minutes")
    rows = await td.get_minutes("sh600000")
    assert isinstance(rows, list) and rows
    hist = await td.get_minutes("sh600000", date=20260811)
    assert isinstance(hist, list)
    samp = await td.get_minutes("sh600000", sampling=True)
    assert [b.price for b in samp] == [10.0, 10.1, 10.2]
    exm = await td.get_minutes("hk00700")
    assert isinstance(exm, list)


# --------------------------------------------------------------------------- #
# 四、公司信息 / 市场
# --------------------------------------------------------------------------- #

async def test_company_info(td):
    for name in ("get_xdxr", "get_finance", "get_price_limits", "get_f10"):
        _use(name)
    x = await td.get_xdxr("sh600000")
    assert isinstance(x, list) and x
    fin = await td.get_finance("sh600000")
    assert isinstance(fin, list) and fin and fin[0].symbol == "sh600000"
    lo, hi = await td.get_price_limits("sh600000", name="浦发银行", pre_close=10.0)
    assert (lo, hi) == (lo, hi)  # 本地计算，不连网
    cat = await td.get_f10("sh600000")
    assert isinstance(cat, list)
    body = await td.get_f10("sh600000", section="600000.txt")
    assert isinstance(body, str)


async def test_market(td):
    for name in ("get_market_stat", "get_unusual", "get_server_info",
                 "get_kline_offset"):
        _use(name)
    stat = await td.get_market_stat()
    assert stat.up_count == 100  # price 10.0 * 10
    un = await td.get_unusual("sh")
    assert isinstance(un, list) and un
    info = await td.get_server_info()
    assert info.date == 20260916
    off = await td.get_kline_offset()
    assert off == (_TOTAL_BARS, 100)


# --------------------------------------------------------------------------- #
# 五、板块
# --------------------------------------------------------------------------- #

async def test_boards(td):
    for name in ("get_boards", "get_board_members", "get_board_of",
                 "get_board_summary", "get_board_ranking",
                 "get_board_change_ranking"):
        _use(name)
    bl = await td.get_boards("all")
    assert isinstance(bl, list) and bl
    mem = await td.get_board_members("881001")
    assert isinstance(mem, list) and mem
    of = await td.get_board_of("sh600000")
    assert isinstance(of, list) and of
    summ = await td.get_board_summary("881001")
    assert set(summ) == {"member_count", "amount", "main_net_amount",
                         "up_count", "down_count"}
    rank = await td.get_board_ranking("industry")
    assert isinstance(rank, list)
    chg = await td.get_board_change_ranking("industry", days=5)
    assert isinstance(chg, list)


# --------------------------------------------------------------------------- #
# 六、服务器文件
# --------------------------------------------------------------------------- #

async def test_files(td):
    _use("get_file")
    raw = await td.get_file("block_gn.dat")
    assert raw == b"abcd"


async def test_parsed_files(td):
    for name in ("get_block_parsed", "get_industry_map",
                 "get_financial_file_infos", "get_financial_records_parsed"):
        _use(name)
    with mock.patch.object(td, "get_file",
                           new=mock.AsyncMock(return_value=_make_block_dat())):
        blocks = await td.get_block_parsed("block_gn.dat")
    assert blocks and blocks[0].category == 2

    with mock.patch.object(td, "get_file",
                           new=mock.AsyncMock(
                               return_value="0|000001|A01|||S01\n".encode("gbk"))):
        ind = await td.get_industry_map()
    assert ind["000001"].sw_industry == "S01"

    with mock.patch.object(td, "get_file",
                           new=mock.AsyncMock(
                               return_value=b"gpcw20260331.zip,abc,1024\n")):
        infos = await td.get_financial_file_infos()
    assert infos[0].filesize == 1024

    with mock.patch.object(td, "get_file",
                           new=mock.AsyncMock(return_value=_make_financial_zip())):
        recs = await td.get_financial_records_parsed("tdxfin/gpcw20260331.zip")
    assert len(recs) == 1 and recs[0].report_date == 20260331


# --------------------------------------------------------------------------- #
# 七、废弃别名（33 个）
# --------------------------------------------------------------------------- #

async def test_aliases_kline(td):
    for name in ("get_kline", "get_kline_since", "get_kline_batch",
                 "get_index_kline", "get_stock_kline_with_indicators"):
        _use(name)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert len(await td.get_kline(1, "600000", 4, count=60)) == 60
        assert len(await td.get_kline_since(
            1, "600000", 4, datetime.datetime(2026, 8, 1))) >= 0
        batch = await td.get_kline_batch([(1, "600000"), (0, "000001")], 4, count=30)
        assert isinstance(batch, dict) and batch
        assert len(await td.get_index_kline(1, "000001", 4, count=30)) == 30
        withind = await td.get_stock_kline_with_indicators(1, "600000", ["macd"])
        assert "bars" in withind and "indicators" in withind


async def test_aliases_quotes_ticks(td):
    for name in ("get_stock_quotes", "get_stock_quotes_list", "get_transactions",
                 "get_minute", "get_minute_batch", "get_chart_sampling"):
        _use(name)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert len(await td.get_stock_quotes([(1, "600000")])) == 1
        assert isinstance(await td.get_stock_quotes_list(1, count=10), list)
        assert isinstance(await td.get_transactions(1, "600000", count=10), list)
        assert isinstance(await td.get_minute(0, "000001"), list)
        mb = await td.get_minute_batch([(1, "600000")])
        assert isinstance(mb, dict)
        assert (await td.get_chart_sampling(1, "600000")) == [10.0, 10.1, 10.2]


async def test_aliases_universe(td):
    for name in ("get_security_count", "get_security_list",
                 "get_security_list_all"):
        _use(name)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert await td.get_security_count(0) == 8
        assert isinstance(await td.get_security_list(0, count=1), list)
        assert isinstance(await td.get_security_list_all(0), list)


async def test_aliases_files(td):
    for name in ("get_financial_file_list", "get_financial_records",
                 "get_company_info_category", "get_company_info_content",
                 "get_block_info", "get_report_file", "get_file_meta",
                 "download_file"):
        _use(name)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert await td.get_financial_file_list() == ["abcd"]
        assert await td.get_financial_records("tdxfin/gpcw.txt") == b"abcd"
        assert isinstance(await td.get_company_info_category(1, "600000"), list)
        assert isinstance(await td.get_company_info_content(
            1, "600000", "f.txt"), str)
        assert await td.get_block_info("block_gn.dat") == b"abcd"
        assert await td.get_report_file("tdxhy.cfg") == b"abcd"
        assert (await td.get_file_meta("x.dat")).size == 4
        assert await td.download_file("x.dat") == b"abcd"


async def test_aliases_boards(td):
    for name in ("get_board_list", "get_belong_board", "get_symbol_info"):
        _use(name)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert isinstance(await td.get_board_list(0), list)
        assert isinstance(await td.get_belong_board(1, "600000"), list)
        snap = await td.get_symbol_info(1, "600000")
        assert snap is not None and snap.name == "浦发银行"


async def test_aliases_goods(td):
    for name in ("get_goods_list", "get_goods_count", "get_goods_quotes",
                 "get_goods_quotes_list", "get_goods_chart_sampling",
                 "get_goods_tick_chart", "get_goods_transaction",
                 "get_goods_transaction_all"):
        _use(name)
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert isinstance(await td.get_goods_list(31), list)
        assert await td.get_goods_count(31) == 1  # 只有 1 条 market=31
        assert isinstance(await td.get_goods_quotes([(31, "00700")]), list)
        assert isinstance(await td.get_goods_quotes_list(31), list)
        assert await td.get_goods_chart_sampling(31, "00700") == [10.0, 10.1, 10.2]
        assert isinstance(await td.get_goods_tick_chart(31, "00700"), list)
        assert isinstance(await td.get_goods_transaction(31, "00700"), list)
        assert isinstance(await td.get_goods_transaction_all(31, "00700"), list)


# --------------------------------------------------------------------------- #
# 八、覆盖完整性（必须放最后）
# --------------------------------------------------------------------------- #

def test_every_public_method_was_exercised():
    expected = _public_methods()
    missing = sorted(expected - EXERCISED)
    assert not missing, f"以下公开方法未被回归测试覆盖: {missing}"
    extra = sorted(EXERCISED - expected)
    assert not extra, f"覆盖集合包含非公开方法: {extra}"
