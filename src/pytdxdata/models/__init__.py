"""数据模型：K线/报价/列表/逐笔/分时/财务（全部dataclass，性能优先不依赖pandas）"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any




class ExMarket(IntEnum):
    """扩展市场代码（港股/期货/期权，通达信EX通道）"""
    TEMP_STOCK = 1
    ZZ_FUTURES_OPTION = 4
    DL_FUTURES_OPTION = 5
    SH_FUTURES_OPTION = 6
    CFFEX_OPTION = 7
    SH_STOCK_OPTION = 8
    SZ_STOCK_OPTION = 9
    BASIC_FX = 10
    CROSS_FX = 11
    INTL_INDEX = 12
    COMEX_FUTURES = 16
    NYMEX_FUTURES = 17
    CBOT_FUTURES = 18
    HK_FINANCIAL_FUTURES = 23
    HK_FINANCIAL_OPTIONS = 24
    HK_STOCK_FUTURES = 25
    HK_STOCK_OPTIONS = 26
    HK_INDEX = 27
    ZZ_FUTURES = 28
    DL_FUTURES = 29
    SH_FUTURES = 30
    HK_MAIN_BOARD = 31
    OPEN_END_FUND = 33
    MONETARY_FUND = 34
    MACRO_INDICATOR = 38
    FUTURES_INDEX = 42
    B_TO_H = 43
    NEEQ = 44
    SH_GOLD = 46
    CFFEX_FUTURES = 47
    HK_GEM = 48
    HK_FUND = 49
    SUNSHINE_PRIVATE_FUND = 56
    MAIN_FUTURES_CONTRACT = 60
    CSI_INDEX = 62
    GZ_ARBITRAGE_FUTURES = 65
    GZ_FUTURES = 66
    GZ_OPTIONS = 67
    EXTENDED_SECTOR_INDEX = 70
    HK_STOCK_GGT = 71
    GE_STOCK = 73
    US_STOCK = 74
    SG_STOCK = 78
    HK_DARK_POOL = 98
    CODE_MIRROR = 100
    SZSE_INDEX = 102
class Market(IntEnum):
    SZ = 0
    SH = 1
    BJ = 2


class KlinePeriod(IntEnum):
    """通达信K线周期编码（KlineCategory，与 easy-tdx 一致）"""
    MIN_5 = 0
    MIN_15 = 1
    MIN_30 = 2
    MIN_60 = 3
    DAY = 4
    WEEK = 5
    MONTH = 6
    MIN_1 = 7
    MIN_3 = 8
    YEAR = 9
    SEASON = 10
    YEAR_ALT = 11

    @property
    def is_minute(self) -> bool:
        return self in (KlinePeriod.MIN_1, KlinePeriod.MIN_5, KlinePeriod.MIN_15,
                        KlinePeriod.MIN_30, KlinePeriod.MIN_60)


class Adjust(IntEnum):
    """复权方式（MAC通道）"""
    NONE = 0
    QFQ = 1  # 前复权
    HFQ = 2  # 后复权


@dataclass(slots=True)
class SecurityBar:
    """K线记录（价格单位：元）"""
    market: int
    code: str
    open: float
    high: float
    low: float
    close: float
    vol: float      # 股票=股 / 转债=手（与vipdoc一致）
    amount: float   # 元
    year: int
    month: int
    day: int
    hour: int = 0
    minute: int = 0

    @property
    def datetime(self) -> datetime:
        return datetime(self.year, self.month, self.day, self.hour, self.minute)


@dataclass(slots=True)
class IndexBar(SecurityBar):
    """指数K线（多涨跌家数字段，结构同K线仅解析时跳过4字节）"""
    pass


@dataclass(slots=True)
class SecurityQuote:
    """实时五档报价"""
    market: int
    code: str
    price: float
    pre_close: float
    open: float
    high: float
    low: float
    vol: float
    amount: float
    s_vol: float            # 主动卖
    b_vol: float            # 主动买
    bid: list[tuple[float, float]] = field(default_factory=list)   # [(价,量)x5]
    ask: list[tuple[float, float]] = field(default_factory=list)   # [(价,量)x5]
    server_time: str = ""
    trading_status: int = 0
    cur_vol: float = 0.0        # 当前成交量(手)
    rise_speed: float = 0.0     # 涨速(%)
    limit_up: float = 0.0       # 涨停价(本地计算, 未填为0)
    limit_down: float = 0.0     # 跌停价
    decimal_point: int = 2      # 价格小数位
    open_amount: float = 0.0    # 开盘金额(个股)


@dataclass(slots=True)
class SecurityInfo:
    """证券列表条目"""
    market: int
    code: str
    name: str
    volunit: int
    decimal_point: int
    pre_close: float


@dataclass(slots=True)
class TransactionRecord:
    """逐笔成交（MAC通道含 trade_count=成交笔数）"""
    market: int
    code: str
    time: str           # HH:MM:SS
    price: float
    vol: float          # 手（MAC逐笔）
    trade_count: int    # 成交笔数（MAC=逐笔记录字段；标准当日逐笔协议也有但需单独解析）
    bs_flag: int        # 1=主动买 0=主动卖


@dataclass(slots=True)
class MinuteBar:
    """分时数据"""
    price: float
    vol: float
    avg_price: float = 0.0   # 均价(协议第2字段)
    hour: int = 0
    minute: int = 0


@dataclass(slots=True)
class XdxrRecord:
    """除权除息记录"""
    market: int
    code: str
    year: int
    month: int
    day: int
    category: int
    name: str
    fenhong: float = 0.0       # 每股分红
    peigujia: float = 0.0
    songzhuangu: float = 0.0   # 每股送转
    peigu: float = 0.0
    suogu: float = 0.0
    xingquanjia: float = 0.0
    fenshu: float = 0.0
    # 股本变动类(category 2~10): 前后流通/总股本(万股)
    panqian_liutong: float = 0.0
    qian_zongguben: float = 0.0
    panhou_liutong: float = 0.0
    hou_zongguben: float = 0.0

    @property
    def date(self) -> str:
        """日期字符串 YYYY-MM-DD。"""
        return "%04d-%02d-%02d" % (self.year, self.month, self.day)


@dataclass(slots=True)
class FinanceRecord:
    """财务快照（股本/金额单位：万股/万元）"""
    market: int
    code: str
    liutong_guben: float = 0.0
    zong_guben: float = 0.0
    guojia_gu: float = 0.0
    faqiren_faren_gu: float = 0.0
    faren_gu: float = 0.0
    b_gu: float = 0.0
    h_gu: float = 0.0
    zhigong_gu: float = 0.0
    province: int = 0
    industry: int = 0
    updated_date: int = 0
    ipo_date: int = 0
    gudong_renshu: float = 0.0
    zong_zichan: float = 0.0
    liudong_zichan: float = 0.0
    guding_zichan: float = 0.0
    wuxing_zichan: float = 0.0
    liudong_fuzhai: float = 0.0
    changqi_fuzhai: float = 0.0
    ziben_gongjijin: float = 0.0
    jing_zichan: float = 0.0
    zhuying_shouru: float = 0.0
    zhuying_lirun: float = 0.0
    yingshou_zhangkuan: float = 0.0
    yingye_lirun: float = 0.0
    touzi_shouyu: float = 0.0
    jingying_xianjinliu: float = 0.0
    zong_xianjinliu: float = 0.0
    cunhuo: float = 0.0
    lirun_zonghe: float = 0.0
    shuihou_lirun: float = 0.0
    jing_lirun: float = 0.0
    weifen_lirun: float = 0.0
    meigujing_zichan: float = 0.0
    reserve2: float = 0.0


# ===== MAC 扩展命令模型 =====

@dataclass(slots=True)
class BoardInfo:
    """板块列表条目（含领涨股信息）"""
    market: int
    code: str
    name: str
    price: float
    rise_speed: float
    pre_close: float
    # 领涨股(160B记录的后80B)
    symbol_market: int = 0
    symbol_code: str = ""
    symbol_name: str = ""
    symbol_price: float = 0.0
    symbol_rise_speed: float = 0.0
    symbol_pre_close: float = 0.0


@dataclass(slots=True)
class MemberQuote:
    """板块成分股报价（MAC自定义字段）"""
    market: int
    code: str
    name: str
    fields: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class BelongBoard:
    """个股所属板块"""
    market: int
    board_code: str
    board_name: str
    close: float
    pre_close: float


@dataclass(slots=True)
class AuctionItem:
    """集合竞价"""
    time: str
    price: float
    matched: int
    unmatched: int


@dataclass(slots=True)
class UnusualItem:
    """市场异动"""
    market: int
    code: str
    name: str
    type: int
    desc: str
    value: float
    time: str


@dataclass(slots=True)
class SymbolSnapshot:
    """个股特征快照"""
    market: int
    code: str
    name: str
    date: int
    time: int
    activity: int
    pre_close: float
    open: float
    high: float
    low: float
    close: float
    vol: float
    amount: float
    turnover: float
    avg_price: float


@dataclass(slots=True)
class GoodsItem:
    """扩展市场商品（期货/期权）"""
    category: int
    name: str
    code: float
    switch: int
    index: int


@dataclass(slots=True)
class ServerInfo:
    """服务器交易时段信息"""
    date: int
    last_trade_date: int
    sessions: list[tuple[int, int]] = field(default_factory=list)


@dataclass(slots=True)
class CapitalFlow:
    """资金流向"""
    market: int
    code: str
    main_buy: float
    main_sell: float
    retail_buy: float
    retail_sell: float
    buy5: float
    sell5: float
    big5: float
    mid5: float


# ===== 服务器文件解析模型（板块/行业/历史专业财报） =====

@dataclass(slots=True)
class TdxBlock:
    """通达信板块条目（解析自 block_zs/gn/fg.dat）。

    category: 0=行业 1=地域 2=概念 3=风格（按文件名推断）
    count:    文件内声明的成分股数量（可能大于 codes 实际解析数）
    """
    name: str
    category: int
    count: int
    codes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IndustryInfo:
    """个股行业分类（解析自 tdxhy.cfg）"""
    code: str
    tdx_industry: str          # 通达信行业代码
    sw_industry: str = ""      # 申万行业代码（部分文件缺失）


@dataclass(slots=True)
class FinancialFileInfo:
    """历史专业财报文件索引条目（解析自 tdxfin/gpcw.txt）"""
    filename: str              # 如 gpcw20260331.zip
    hash: str                  # MD5
    filesize: int              # 字节


@dataclass(slots=True)
class FinancialRecord:
    """历史专业财报记录（解析自 gpcw*.zip 内 .dat）。

    注意与 FinanceRecord（最新财务快照）区分：本类保留协议原始 float 字段，
    字段含义随 report_size 变化，需结合 gpcw.txt 字段字典解释。
    """
    code: str
    market: int                # 0=深 1=沪
    report_date: int           # 报告期 YYYYMMDD
    fields: list[float] = field(default_factory=list)
