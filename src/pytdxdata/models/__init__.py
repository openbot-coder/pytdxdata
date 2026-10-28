"""数据模型：K线/报价/列表/逐笔/分时/财务（全部dataclass，性能优先不依赖pandas）"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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


# 扩展市场中「时间戳跨时区拼接」的市场号：通达信 EX 通道对美股返回的分钟级 K线
# 里，日期字段是**美东交易日**、时分字段是**北京时间**，两个时区被拼进同一个
# 五元组，必须归一化（见 SecurityBar.__post_init__）。
_US_EX_MARKETS: frozenset[int] = frozenset({int(ExMarket.US_STOCK)})

# 归一化判据：北京时间 hour >= 12 的 bar 与美东交易日属同一自然日。美股常规时段
# 09:30~16:00 EDT = 北京 21:30~次日 04:00（冬令时 22:30~次日 05:00），夏令时/
# 冬令时下分界都稳定落在 12 点。
_US_DAY_SPLIT_HOUR = 12


# SecurityBar 上「后加的」可选字段：旧磁盘缓存反序列化时要补默认值（见 __setstate__）
_BAR_LATE_FIELDS: tuple[str, ...] = ("settle",)


@dataclass(slots=True)
class SecurityBar:
    """K线记录（价格单位：元）。跨市场统一模型（A股/港股/美股/期货共用）。

    时间戳口径**统一为北京时间**：A股/港股/期货原始即为北京时间；美股原始是
    「日期=美东交易日 + 时分=北京时间」的混合值，构造时已还原为真实北京时间
    （见 ``__post_init__``）。该 bar 归属的交易日见 :attr:`trading_date`。

    ``amount`` / ``settle`` 的可取性**按通道**：A股标准/MAC 通道两者都有；
    EX 通道（0x23FF）一个 32B 记录里只有**一个** 4B 槽位可复用，权益类市场用它
    装成交额、期货期权类用它装结算价，两者不可能同时拿到 —— 详见 :attr:`amount`。
    """
    market: int
    code: str
    open: float
    high: float
    low: float
    close: float
    # vol 单位按品种：A股/ETF/美股=股；转债=手（1手=10张）；⚠️港股主板/基金=手（1手=100股）
    vol: float
    # 元。⚠️ amount/vol 只在 A股上等于均价；转债还要再 /10（手=10张）、港股再 /100（手=100股）。
    # ⚠️ EX 通道的期货/期权（中金所、上期所、郑商所、大商所及各期权）协议帧内**没有成交额**，
    #   恒为 0.0 —— 要成交额请改用 EX 报价（``get_quotes``）或其它数据源。
    amount: float
    year: int
    month: int
    day: int
    hour: int = 0
    minute: int = 0
    symbol: str = ""        # 规范化标的串（sz000001 / hk00700），由 API 层填充
    # 结算价。仅 EX 通道的**期货/期权**有（日线才有值，分钟线为 None）；其余通道恒 None。
    settle: float | None = None

    def __setstate__(self, state: Any) -> None:
        """反序列化兜底：早期版本写下的磁盘缓存里没有后加的可选字段（如 ``settle``）。

        ``@dataclass(slots=True)`` 的默认反序列化只把 pickle 里存在的槽位塞回去，
        缺键的槽位**不会**被赋默认值 —— 直接读 ``bar.settle`` 会抛 ``AttributeError``。
        缓存键里没有 schema 版本号，所以这里必须容错，否则升级后读旧缓存即崩。
        """
        if isinstance(state, tuple) and len(state) == 2:   # slots 形式的 (dict, slots)
            data = dict(state[1] or {})
        else:
            data = dict(state or {})
        for name in _BAR_LATE_FIELDS:
            data.setdefault(name, None)
        for key, value in data.items():
            object.__setattr__(self, key, value)

    def __post_init__(self) -> None:
        """美股（EX 通道）时间戳归一化：还原成真实北京时间。

        原始报文里 ``zipday`` 是**美东交易日**、``tminutes`` 是**北京时间分钟数**，
        直拼出的 ``datetime`` 会把同一交易日的下午半场（北京 00:00~04:00）排到
        上午半场（北京 21:30~23:59）**之前** —— 单日 390 根全部错位、首末颠倒。
        这里把凌晨段（北京 hour < 12）的日期 +1 天，得到严格单调的北京时间序列。
        """
        if self.market in _US_EX_MARKETS and self.hour < _US_DAY_SPLIT_HOUR:
            d = date(self.year, self.month, self.day) + timedelta(days=1)
            self.year, self.month, self.day = d.year, d.month, d.day

    @property
    def is_us(self) -> bool:
        """是否美股（EX 通道 ``US_STOCK``）。"""
        return self.market in _US_EX_MARKETS

    @property
    def datetime(self) -> datetime:
        """bar 的时刻（北京时间）。"""
        return datetime(self.year, self.month, self.day, self.hour, self.minute)

    @property
    def trading_date(self) -> str:
        """该 bar 归属的交易日，``YYYY-MM-DD``。

        美股：``datetime`` 已还原为北京时间，凌晨段（北京 hour < 12）属于**前一天**
        的美东交易日，故回退一天；A股/港股/期货的交易日即自然日。按交易日聚合
        （如分钟线合成日线）时应当用本字段，而不是 ``datetime.date()``。
        """
        if self.is_us and self.hour < _US_DAY_SPLIT_HOUR:
            return (date(self.year, self.month, self.day) - timedelta(days=1)).isoformat()
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d}"


@dataclass(slots=True)
class IndexBar(SecurityBar):
    """指数K线（多涨跌家数字段，结构同K线仅解析时跳过4字节）"""
    pass


@dataclass(slots=True)
class IndicatorSet:
    """K线 + 技术指标组合（``TdxData.get_indicators`` 的返回单元）。

    ``indicators`` 里每个序列与 ``bars`` 等长、尾部对齐；预热期为 ``None`` 的指标
    （如 BOLL 前三档）保留 ``None``。
    """
    symbol: str
    bars: list[SecurityBar] = field(default_factory=list)
    indicators: dict[str, list[float | None]] = field(default_factory=dict)


@dataclass(slots=True)
class SecurityQuote:
    """报价（跨市场统一模型）。

    A股（标准通道）：五档 ``bid``/``ask`` + 买卖量 + 涨跌停价齐全，``name`` 为空。
    A股（``fields=[...]`` 走 MAC 0x122B）：``name`` 与 ``fields`` 齐全，五档为空。
    扩展市场（港股/美股/期货）：``name`` + 基础价量在属性上，其余字段位在 ``fields``。
    """
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
    symbol: str = ""            # 规范化标的串
    name: str = ""              # 名称（仅 MAC/EX 通道有）
    fields: dict[str, float] = field(default_factory=dict)  # 原始字段位（"0x7" 等）


@dataclass(slots=True)
class SecurityInfo:
    """标的清单条目（跨市场统一模型：A股证券列表 + 扩展市场商品列表）"""
    market: int
    code: str
    name: str
    volunit: int
    decimal_point: int
    pre_close: float
    symbol: str = ""            # 规范化标的串
    kind: str = ""              # 分类描述（扩展市场的 category/desc）


@dataclass(slots=True)
class TransactionRecord:
    """逐笔成交（跨市场统一模型；A股 MAC 通道含 trade_count=成交笔数）。

    注意（实测 2026-10-21）：

    - 服务端是按**约 3 秒聚合**返回的切片，不是交易所原始逐笔成交。
    - ``vol`` / ``trade_count`` 都是**该切片自身的增量**，不是当日累计值。
    - ``trade_count`` 为**有符号**值：集合竞价段（``bs_flag=8``）会是负数
      （实测 sh600000 09:15 段为 -874），不要对全表直接求 max/mean。
    - 返回列表已按时间升序，但同秒多条时顺序不保证。
    - ``bs_flag=8`` 的**竞价委托记录已被 ``get_ticks()`` 过滤掉**，不会出现在结果里。

    **指数语义完全不同**（``is_cn_index`` 为真时，如 ``sh000016``）：

    - ``vol`` 单位是**百元**（= 成交额 / 100），不是「手」。实测
      ``快照amount / Σtick.vol = 100.0002``，且 ``Σtick.vol`` 与分时 Σvol **逐位相等**。
      想和成分股成交量对比必须先换算，直接比会得到 ~26 倍的假比例。
    - ``trade_count`` **不是笔数**，而是**当时均价 × 1000**（实测 tc/1000 与分时
      ``avg_price`` 的差 < 1.1）。指数没有「成交笔数」概念。
    - ``bs_flag`` 语义不成立（实测返回 2，而模型只定义 0=卖 / 1=买）。

    **场内基金同样没有笔数**（ETF / 多数 LOF，见
    :func:`~pytdxdata.symbols.has_order_count`）：

    - ``trade_count`` 是 ``价格 × 1000``（实测 ``sh510310`` 收盘 tc=3994，
      当时价 3.989 → 3989，偏差 σ≈2；货币 ETF ``sh511990`` 恒为 0）。
    - ``vol`` 仍是**手**，``bs_flag`` 正常。
    - 这类标的的 :attr:`trade_count_available` 为 ``False``，``trade_count`` 已被
      置 ``0``，不要拿去求和/求均值。

    ``trade_count_available=False`` 表示该品种**协议上就不带笔数**，
    与「本切片恰好 0 笔」不是一回事。

    **可转债（110/111/113/118/123/127/128/132）三样都是真的**（实测 2026-10-22，
    16 只 A股可转债当日 + 8 只历史日全部自洽）：

    - ``vol`` 单位是**手**（1 手 = 10 张），与股票逐笔一致；``trade_count`` 是**真实成交笔数**
      （``sh111000`` 3631 条切片 / 20443 笔，可转债在
      :func:`~pytdxdata.symbols.has_order_count` 里判为**可用**）。
    - ``Σvol`` 与**日线** ``SecurityBar.vol`` **1:1 相等**（比值 1.000000）——
      这点和股票相反：股票逐笔是「手」、日线是「股」，Σ比值是 **0.01**。
    - ⚠️ 但**日线** ``amount / vol`` **不等于**均价，而是均价的 **10 倍**（``vol`` 的「手」= 10 张）。
      要算可转债均价须用 ``amount / vol / 10``；股票则是 ``amount / vol`` 直接就对。
      实测：``sz127025`` 逐笔 VWAP 105.9063 vs ``amount/vol`` 1059.0957，比值 10.0003。
    """

    market: int
    code: str
    time: str           # HH:MM:SS（扩展市场为 HH:MM）
    price: float
    vol: float          # 股票/转债=手；指数=百元（成交额/100），见类注释
    trade_count: int    # 股票=笔数增量；指数/基金=占位值，见类注释
    bs_flag: int        # 1=主动买 0=主动卖 2=中性 5=盘后固定价（指数语义不成立）
    symbol: str = ""
    fields: dict[str, float] = field(default_factory=dict)  # 扩展市场额外字段（增仓等）
    trade_count_available: bool = True  # 指数/ETF/LOF 为 False（协议无笔数字段）


@dataclass(slots=True)
class MinuteBar:
    """分时（跨市场统一模型；A股 MAC 0x122D / 扩展市场 0x122D 同构）。

    ``hour``/``minute`` 是**真实时钟**（``0 <= hour < 24``），跨日的分时请用
    :attr:`datetime` / :attr:`trading_date`，不要自己拿 ``hour`` 拼日期。
    """
    price: float
    vol: float               # 手（股票/转债同）；Σ分时vol 与当日日线 vol 同口径
    avg_price: float = 0.0   # 均价(协议第2字段)
    hour: int = 0
    minute: int = 0
    symbol: str = ""
    market: int = 0
    code: str = ""
    date: int = 0            # YYYYMMDD
    fields: dict[str, float] = field(default_factory=dict)  # 扩展市场额外字段（动量等）
    day_offset: int = 0      # 相对 date 的自然日偏移：美股凌晨段为 +1

    def __post_init__(self) -> None:
        """美股（EX 通道）分时时间归一化。

        EX 分时的 ``minutes`` 字段是**自开盘起的连续分钟数**，不是「当天的分钟数」：
        实测 ``usAAPL`` 返回 ``21:30`` ~ ``27:59``（390 条里 240 条的 ``hour`` ≥ 24）。
        ``27:59`` 实际是次日 ``03:59`` 北京时间。直接 ``minutes // 60`` 会得到
        ``hour=27`` 这种**非法时钟值**，任何 ``datetime(...)`` 拼接都会抛异常，
        按小时聚合也会把凌晨段错误地归到同一天。

        这里拆成「真实小时 + 日偏移」：``hour = (minutes // 60) % 24``，
        ``day_offset = (minutes // 60) // 24``。A股/港股/期货的 ``hour`` 本就合法
        （A股合成时间 09:30~15:00、港股 09:30~16:00），**零行为变化**。
        """
        if self.hour >= 24:
            self.day_offset += self.hour // 24
            self.hour %= 24

    @property
    def datetime(self) -> datetime:
        """该分时点的时刻（北京时间，已含跨日偏移）。"""
        base = date(self.date // 10000, self.date // 100 % 100, self.date % 100)
        d = base + timedelta(days=self.day_offset)
        return datetime(d.year, d.month, d.day, self.hour, self.minute)

    @property
    def is_us(self) -> bool:
        """是否美股（EX 通道 ``US_STOCK``）。"""
        return self.market in _US_EX_MARKETS

    @property
    def trading_date(self) -> str:
        """该分时点归属的交易日，``YYYY-MM-DD``。

        美股：一个交易日横跨两个北京自然日（21:30~次日 04:00），凌晨段
        （北京 hour < 12）归属**前一天**的美东交易日，与
        :attr:`SecurityBar.trading_date` 口径一致。A股/港股/期货即 ``date`` 本身。
        按交易日聚合分时（合成日线、日内统计）**必须用它**。
        """
        base = self.datetime
        if self.is_us and base.hour < _US_DAY_SPLIT_HOUR:
            base -= timedelta(days=1)
        return base.date().isoformat()


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
    symbol: str = ""

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
    symbol: str = ""


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
    symbol: str = ""            # 规范化标的串（板块码，如 sh881001）


@dataclass(slots=True)
class MemberQuote:
    """板块成分/自定义字段报价的**协议层原始记录**。

    API 层会把它归一成 :class:`SecurityQuote`（``fields`` 原样保留、基础字段位
    填进属性），所以普通调用不会看到本类。
    """
    market: int
    code: str
    name: str
    fields: dict[str, float] = field(default_factory=dict)
    symbol: str = ""


@dataclass(slots=True)
class BelongBoard:
    """个股所属板块"""
    market: int
    board_code: str
    board_name: str
    close: float
    pre_close: float
    symbol: str = ""            # 被查询的个股（非板块）


@dataclass(slots=True)
class AuctionItem:
    """集合竞价"""
    time: str
    price: float
    matched: int
    unmatched: int
    symbol: str = ""


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
    symbol: str = ""


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
    symbol: str = ""


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
    symbol: str = ""


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


# ===== 指数成分成交汇总（TdxData.get_index_trade_stats） =====

@dataclass(slots=True)
class MinuteTradeStat:
    """指数**全部成分股**汇总后的单分钟成交**增量**（不是累计）。

    累计曲线用 :meth:`IndexTradeStats.cumulative`，不要自己再对同一字段反复累加。
    """
    time: str           # HH:MM
    volume: float       # 成交量合计（手，1 手 = 100 股）
    trade_count: int    # 成交笔数合计
    amount: float       # 成交额估算（元）= Σ(逐笔价 × 手 × 100)


@dataclass(slots=True)
class IndexTradeStats:
    """某个指数「全部成分股逐笔」汇总出的成交曲线。

    为什么需要它：**通达信协议里指数本身没有成交笔数** —— 指数逐笔
    （``sh000001``）的 ``trade_count`` 字段实际是「当时均价 × 1000」（见
    :class:`TransactionRecord`）。所以「指数笔数」唯一可行的口径是
    **把成分股的逐笔笔数累加**，本类即该口径。

    口径校验（2026-10-22 实测）：

    - ``total_amount`` 是**成分股**合计。对「成分恰好就是指数覆盖面」的指数，它与
      指数快照 ``amount`` **完全相等**：``sh000016`` 661.34 亿 = 661.34 亿、
      ``sh000300`` 2373.98 亿 = 2373.98 亿（比值 1.0000）。
    - **但深市 ``399xxx`` 的指数成交额字段是「全板块」口径**，不是样本股：
      实测 ``sz399001`` 快照 5081.40 亿 = 深市 2908 只股票合计 5081.01 亿、
      ``sz399006`` 2568.60 亿 = 创业板全部股票合计。所以拿深市指数 ``amount``
      与 ``total_amount`` 比会得到 ~0.43 这类无意义比值——不是数据错，是口径不同。
    - ``total_volume`` 单位是**手**（与指数逐笔的「百元」单位不同，勿混用）。
    - 北交所成分（``bj`` 前缀）**没有笔数字段**（实测 ``trade_count`` 恒为 0）：
      此时 ``trade_count_members = 0``、:attr:`trade_count_available` 为 ``False``，
      只有成交量可用。``sh``/``sz`` 成分均正常返回笔数。

    ``minutes`` 只包含**实际有成交**的分钟（午休 11:30~13:00 之间无桶），
    画累计曲线时请按 ``time`` 自行补齐或直接连线。
    """
    index: str                      # 规范化指数标的（如 sh000300）
    date: int | None                # None=当日；否则 YYYYMMDD
    member_count: int               # 指数成分数
    active_members: int             # 实际有逐笔成交的成分数（停牌/无成交不计）
    trade_count_members: int        # 返回了笔数的成分数（北交所为 0）
    minutes: list[MinuteTradeStat] = field(default_factory=list)
    total_volume: float = 0.0
    total_trade_count: int = 0
    total_amount: float = 0.0
    failed_members: int = 0         # 逐笔拉取失败的成分数（skip_errors=True 时）
    elapsed: float = 0.0            # 本次汇总耗时（秒）

    @property
    def trade_count_available(self) -> bool:
        """是否有笔数口径（北交所成分为 ``False``，此时 ``trade_count`` 全 0）。"""
        return self.trade_count_members > 0

    def cumulative(self) -> list[MinuteTradeStat]:
        """累计曲线：当日**截止该分钟**的累计量，时间升序。

        这是「当日截止该分钟累计成交笔数」的直接数据源。
        """
        out: list[MinuteTradeStat] = []
        vol = 0.0
        tc = 0
        amt = 0.0
        for m in self.minutes:
            vol += m.volume
            tc += m.trade_count
            amt += m.amount
            out.append(MinuteTradeStat(time=m.time, volume=vol,
                                       trade_count=tc, amount=amt))
        return out

    def cum_trade_count(self) -> list[tuple[str, int]]:
        """``[(HH:MM, 累计笔数)]``，画「截止该分钟累计笔数」曲线用。"""
        return [(m.time, m.trade_count) for m in self.cumulative()]
