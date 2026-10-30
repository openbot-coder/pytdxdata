# pytdxdata 对外接口清单

> 以 `src/pytdxdata` 源码为准整理（v0.7.2）。分四类：Python API / 数据模型 / 辅助模块 / CLI。
>
> 方法与代码的一致性由 `scripts/check_docs.py` 在 CI 中校验。

## 一、包级导出

```python
from pytdxdata import (
    TdxData,          # 唯一对外入口类
    Market, ExMarket, KlinePeriod, Adjust,          # 枚举
    SecurityBar, SecurityQuote, TransactionRecord,  # 数据模型
    MinuteBar, XdxrRecord, FinanceRecord,
    __version__,
)
```

## 二、入口类 `TdxData`

构造参数（全部 keyword-only）：

| 参数 | 默认 | 说明 |
|------|------|------|
| `standard_servers` / `mac_servers` / `ex_servers` | None | 自定义服务器清单，None 用内置探测结果 |
| `port` | 7709 | 标准/MAC 端口（EX 固定 7727） |
| `timeout` | 3.0 | 连接超时（秒） |
| `cache_dir` | None | 磁盘缓存目录；None=仅内存缓存 |
| `default_adjust` | `Adjust.NONE` | 默认复权方式 |
| `max_inflight` | 64 | single-flight 并发上限 |
| `pool_min` / `pool_max` | 3 / 24 | 动态池上下限 |
| `mac_pool_cap` / `std_pool_cap` / `ex_pool_cap` | 4 / 4 / 2 | 单台服务器连接上限 |
| `df` | None | **DataFrame 后端**：`None`=返回 `list[dataclass]`（0.6.x 行为）、`"polars"`、`"pandas"`。需先装 extra：`pip install pytdxdata[polars]`，详见 [DataFrame 输出](dataframe.md) |

生命周期：

| 方法 | 说明 |
|------|------|
| `async start()` | 启动（服务器探测 + 预建连接 + 缓存） |
| `async close()` | 关闭全部池与缓存 |
| `async with TdxData() as td:` | 上下文管理器，自动 start/close |

### 2.1 统一接口（v0.6+ 推荐）{#unified}

> **DataFrame（v0.7+）**：本节的表格型方法在 `TdxData(df="polars")` / `df="pandas"` 时
> 直接返回 DataFrame；`df=None`（默认）返回下表的 `list[...]`。列口径、时间列替换规则、
> 不受影响的接口清单见 [DataFrame 输出](dataframe.md)。

以下方法在 v0.6 新增，标的一律用字符串（`sz000001` / `hk00700` / `usAAPL` / `cffex:IFL0`）。

#### 标的清单

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_universe(market=None)` | `list[SecurityInfo]` | 跨市场标的清单（A 股+港股+美股+期货/期权）；每条带协议自带的 `decimal_point`（价格小数位）与 `pre_close`。**北交所清单协议不支持，返回空列表** |
| `sync_decimal_points(markets=("sh","sz"))` | `dict` | 从证券清单同步**权威价格小数位表**（`SecurityInfo.decimal_point`），灌进 `price_decimal_digits` 的运行时表；`start()` 已自动做（当日缓存命中则零网络），此方法用于强制刷新/排障 |
| `decimal_point_status()` | `dict` | 小数位表状态：`source`（`cache`/`universe`/空）、`stock`、`mixed_prefixes`、`syncing` |

#### 报价

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_quotes(symbols, *, fields=None, concurrency=None, pipeline=True)` | `list[SecurityQuote]` | 跨市场混合报价（A 股标准五档；给了 fields 走 MAC/EX 字段位有 name）。超 80 只自动切批，`pipeline` 开启**单连接流水线**，全市场 5,200 只 ~0.33s |
| `get_snapshot(symbols)` | `list[SymbolSnapshot]` | 个股特征快照（A 股） |
| `get_auction(symbols)` | `list[AuctionItem]` | 集合竞价（A 股） |
| `get_capital_flow(symbols)` | `list[CapitalFlow]` | 个股资金流向（A 股） |

#### K线

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_bars(symbols, period=DAY, *, start=0, count=800, adjust=None, start_date=None, end_date=None, concurrency=6, pipeline=None)` | `list[SecurityBar]` | 跨市场 K 线（自动判指数、复权走 MAC、并发分页；日期区间贴最新走倒推、历史中段走二分）。**≥16 只自动走多连接流水线**：`pipeline` 为每条连接的在途请求数（默认按单请求字节自适应 8~32，传 `1` 关闭），全市场 1 分钟 K 线取当天 43.6s → 5s 级 |
| `get_indicators(symbols, indicators, *, period=DAY, count=30, adjust=None, params=None)` | `list[IndicatorSet]` | K 线+技术指标（MACD/KDJ/RSI/BOLL/MA/EMA） |

#### 逐笔与分时

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_ticks(symbols, *, date=None, start=0, count=None, concurrency=6)` | `list[TransactionRecord]` | 跨市场逐笔成交。返回按时间升序；`start` **从最新一笔往回数**；`vol`/`trade_count` 为约 3 秒切片的增量值（非累计）。**已剔除 `bs_flag=8` 竞价委托**（非成交，`vol` 为累计撮合量）。**指数**：`vol` 单位是百元（成交额/100）、`trade_count` 是均价×1000，均非股票语义；**可转债**：`vol` 是手（=10 张）、`trade_count` 是**真实笔数**，`Σvol` 与日线 `vol` **1:1** |
| `get_minutes(symbols, *, date=None, sampling=False, concurrency=6)` | `list[MinuteBar]` | 跨市场分时（sampling=True 取缩略采样：A股由 0x122D 派生，扩展市场 0x254D） |

#### 公司信息/文件/板块

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_f10(symbol, section=None, *, offset=0, length=65535)` | `list \| str` | F10 公司资料 |
| `get_file(name)` | `bytes` | 服务器文件原始字节（自动选通道） |
| `get_boards(kind="all", *, count=10000)` | `list[BoardInfo]` | 板块列表 |
| `get_board_members(board, *, fields=None, count=100000)` | `list[SecurityQuote]` | 板块成分股报价（归一模型） |
| `get_board_of(symbols)` | `list[BelongBoard]` | 个股所属板块 |
| `get_index_members(index, *, fields=None, count=100000)` | `list[SecurityQuote]` | **指数成分股**报价。裸 6 位按**指数语义**解析（`000001`=上证指数，不是平安银行）：`000/999/880`→沪、`399`→深、`899`→北交所 |
| `get_index_trade_stats(index, date=None, *, concurrency=24, batch_size=50, members=None, skip_errors=True, on_progress=None)` | `IndexTradeStats` | **指数成分逐笔汇总**成每分钟成交量/成交笔数。协议里指数本身没有笔数（`trade_count` 是均价×1000），故由成分股逐笔累加；`.cumulative()` 取「截止该分钟累计」，`.cum_trade_count()` 直接给 `[(HH:MM, 累计笔数)]`。2230 只（上证）约 36-60 s |
| `get_index_trade_stats_multi(index, dates, *, ...)` | `list[IndexTradeStats]` | 多日批量版（成分只请求一次），用于「近 N 日同时点累计均值」基准 |

### 2.2 K线（旧接口，共 6 个） {#legacy-kline}

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_kline(market, code, period, *, start=0, count=800, adjust=None, start_date=None, end_date=None)` | `list[SecurityBar]` | 主接口；count>页自动并发分页；复权走 MAC；支持日期区间（贴最新倒推 / 中段二分） |
| `get_kline_since(market, code, period, since, adjust=None)` | `list[SecurityBar]` | 增量便捷方法（≥since，含当天） |
| `get_kline_batch(stocks, period, *, start=0, count=800, adjust=None, concurrency=6)` | `dict[str, list[SecurityBar]]` | 多股票并发，返回 {code: bars} |
| `get_index_kline(market, code, period, *, start=0, count=800)` | `list[SecurityBar]` | 指数K线（含涨跌家数字段） |
| `get_stock_kline_with_indicators(market, code, indicators, *, period=DAY, count=30, adjust=None, params=None)` | `dict` | K线+技术指标（MACD/KDJ/RSI/BOLL/MA/EMA） |
| `get_kline_offset(offset=0, count=1)` | `tuple[int, int]` | K线偏移信息（MAC） |

### 2.3 报价（旧接口，共 4 个） {#legacy-quote}

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_quotes(symbols, *, fields=None, concurrency=None, pipeline=True)` | `list[SecurityQuote]` | 五档报价；>80 只自动切批（流水线+工作窃取，5,200 只 ~0.33s）；TTL 5s |
| `get_stock_quotes(stocks, bits=None)` | `list[MemberQuote]` | MAC 自定义字段报价（≤80只/次） |
| `get_stock_quotes_list(category, *, start=0, count=80, sort_type=0)` | `list[MemberQuote]` | 市场分类报价（0沪A/2深A/6全A/8科创/14创业） |
| `get_market_stat()` | `MarketStat` | 全市场统计（涨跌家数/总额/市值/涨跌停数） |

### 2.4 逐笔成交 / 分时（旧接口，共 4 个） {#legacy-tick}

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_transactions(market, code, *, date=None, start=0, count=2000)` | `list[TransactionRecord]` | 逐笔成交（MAC，含 trade_count），支持历史日期 |
| `get_minute(market, code, *, date=None)` | `list[MinuteBar]` | 分时（MAC 0x122D，可历史） |
| `get_minute_batch(stocks, *, date=None, concurrency=6)` | `dict[str, list[MinuteBar]]` | 批量分时 |
| `get_chart_sampling(market, code)` | `list[float]` | 分时缩略采样（A股由 MAC **0x122D** 派生；0x254D 仅 EX 通道可用） |

### 2.5 证券列表（旧接口，共 3 个） {#legacy-list}

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_security_count(market)` | `int` | 市场证券数量 |
| `get_security_list(market, *, start=0, count=None)` | `list[SecurityInfo]` | count=None=全部（先查总数防死循环） |
| `get_security_list_all(market)` | `list[SecurityInfo]` | 全市场列表（高并发一次拉完，缓存 1 天） |

### 2.6 除权 / 财务 / F10 / 文件（旧接口，共 15 个） {#file-methods}

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_xdxr(symbols)` | `list[XdxrRecord]` | 除权除息历史（标准） |
| `get_finance(symbols)` | `list[FinanceRecord]` | 财务快照（标准；无数据的标的跳过） |
| `get_price_limits(symbol, name=None, pre_close=None, listed_days=9999)` | `tuple[float\|None, float\|None]` | 涨跌停价（本地计算） |
| `get_company_info_category(market, code)` | `list` | F10 目录（标准） |
| `get_company_info_content(market, code, filename, offset=0, length=65535)` | `str` | F10 内容（分块） |
| `get_block_info(filename)` | `bytes` | 板块文件**原始字节**（先查元数据再分块下载） |
| `get_report_file(filename)` | `bytes` | 报告/财务文件**原始字节**（标准 0x06B9） |
| `get_financial_file_list()` | `list[str]` | 财务文件列表**原始行**（tdxfin/gpcw.txt） |
| `get_financial_records(filename)` | `bytes` | 财报 `.dat` 原始字节（索引写 `.zip` 但服务器只有 `.dat` 时自动回退；取不到返回 `b""`） |
| `get_file_meta(filename)` | `FileMeta` | 远程文件元信息（MAC 0x1215） |
| `download_file(filename, filesize=0)` | `bytes` | 下载完整远程文件（MAC 0x1217，分块） |
| **`get_block_parsed(filename)`** | `list[TdxBlock]` | 板块文件**下载+解析**为板块成分（category 按文件名推断：0行业/2概念/3风格） |
| **`get_industry_map()`** | `dict[str, IndustryInfo]` | tdxhy.cfg **下载+解析**：`{代码: 通达信行业+申万行业}` |
| **`get_financial_file_infos()`** | `list[FinancialFileInfo]` | gpcw.txt **下载+解析**：文件名/MD5/大小 |
| **`get_financial_records_parsed(filename)`** | `list[FinancialRecord]` | 财报**解析**：每股原始 float 字段。可直接传 `get_financial_file_infos()` 的 `filename`——自动纠正服务器实际文件名（`.zip`/`.dat` 两种形态都支持）；报告期取自文件名，缺失回退头部 |

> 解析层独立于网络层：解析器位于 `pytdxdata.codec`（纯标准库 `struct`/`zipfile`），可脱网单测；
> 原 `get_*` 返回 `bytes`/`list[str]` 的方法保持**向后兼容**，解析版为**新增**方法。

### 2.7 板块（旧接口，共 6 个） {#legacy-board}

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_board_list(board_type=0, count=10000)` | `list[BoardInfo]` | 板块列表（0全部/1行业/2概念） |
| `get_board_members(board_symbol, count=100000, sort_type=0)` | `list[MemberQuote]` | 板块成分股报价（如 881001） |
| `get_belong_board(market, code)` | `list[BelongBoard]` | 个股所属板块 |
| `get_board_summary(board_symbol)` | `dict` | 汇总：成分数/成交额/主力净流入/涨跌家数 |
| `get_board_ranking(board_type=1, top_n=20)` | `list[dict]` | 板块涨跌幅排行 |
| `get_board_change_ranking(board_type=1, days=20, top_n=20)` | `list[dict]` | 板块 N 日涨跌幅排行（板块指数K线） |

### 2.8 MAC 扩展：竞价 / 异动 / 快照 / 资金流 / 服务器（旧接口，共 5 个） {#legacy-mac}

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_auction(symbols)` | `list[AuctionItem]` | 集合竞价 |
| `get_unusual(market="all", *, start=0, count=600)` | `list[UnusualItem]` | 市场异动 |
| `get_symbol_info(market, code)` | `SymbolSnapshot` | 个股特征快照 |
| `get_capital_flow(symbols)` | `list[CapitalFlow]` | 个股资金流向 |
| `get_server_info()` | `ServerInfo` | 服务器交易时段信息 |

### 2.9 EX 扩展市场（旧接口，港股/美股/期货/期权，共 8 个） {#legacy-ex}

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_goods_list(market, start=0, count=100)` | `list[dict]` | 扩展市场商品列表（0x23F5，二分定位市场） |
| `get_goods_count(market=None)` | `int` | 商品数量（None=全市场） |
| `get_goods_quotes(stocks, bits=None)` | `list[MemberQuote]` | 扩展市场报价（0x122B） |
| `get_goods_quotes_list(market, count=80)` | `list[MemberQuote]` | 商品列表+报价组合 |
| `get_goods_chart_sampling(market, code)` | `list[float]` | 扩展市场分时采样（0x254D） |
| `get_goods_tick_chart(market, code, ymd=0)` | `list[dict]` | 扩展市场分时 tick（0x122D） |
| `get_goods_transaction(market, code, *, ymd=0, start=0, count=1800)` | `list[dict]` | 扩展市场逐笔（港股 0x23FC/0x2406） |
| `get_goods_transaction_all(market, code, ymd=0)` | `list[dict]` | 港股全量逐笔（自动翻页，≤50页/9万条） |

**合计：`TdxData` 公开异步方法 63 个** = 28 个统一接口 + 33 个 deprecated 别名 + `start`/`close`
（另有 `_execute_standard` / `_execute_mac` / `_execute_ex` 等内部执行方法）。

## 三、数据模型（`pytdxdata.models`）

**枚举**

| 名称 | 取值 |
|------|------|
| `Market` | SZ=0 / SH=1 / BJ=2 |
| `KlinePeriod` | MIN_5=0 / MIN_15=1 / MIN_30=2 / MIN_60=3 / DAY=4 / WEEK=5 / MONTH=6 / MIN_1=7 / MIN_3=8 / YEAR=9 / SEASON=10 / YEAR_ALT=11（`is_minute` 属性） |
| `Adjust` | NONE=0 / QFQ=1 / HFQ=2 |
| `ExMarket` | 46 个扩展市场：HK_MAIN_BOARD=31 / US_STOCK=74 / CFFEX_FUTURES=47 / ZZ_FUTURES=28 / DL_FUTURES=29 / SH_FUTURES=30 / GZ_FUTURES=66 / SH_STOCK_OPTION=8 / SZ_STOCK_OPTION=9 等 |

**dataclass（均为 `@dataclass(slots=True)`）**

| 模型 | 关键字段 |
|------|---------|
| `SecurityBar` | market/code/open/high/low/close/vol/amount/year/month/day/hour/minute/symbol/**settle** + `datetime` 属性（`settle` 结算价仅 EX 期货/期权日线有值，其余为 `None`） |
| `IndexBar` | 继承 `SecurityBar`（指数K线） |
| `SecurityQuote` | price/pre_close/OHLC/vol/amount/s_vol/b_vol/`bid[5]`/`ask[5]`/server_time/trading_status/cur_vol/rise_speed/limit_up/limit_down/decimal_point/open_amount |
| `SecurityInfo` | market/code/name/volunit/decimal_point/pre_close |
| `TransactionRecord` | market/code/time/price/vol/`trade_count`/bs_flag |
| `MinuteBar` | price/vol/avg_price/hour/minute/day_offset + `datetime`/`trading_date` 只读属性（hour 恒为合法时钟值 0~23；美股一个交易日跨两个北京自然日，按交易日聚合用 `trading_date`） |
| `XdxrRecord` | 分红送转/配股/缩股/行权 + 股本变动 + `date` 属性 |
| `FinanceRecord` | 股本/资产/负债/营收/利润等 36 个字段（万元/万股） |
| `BoardInfo` | 板块 + 领涨股信息 |
| `MemberQuote` | market/code/name/`fields: dict[str, float]` |
| `BelongBoard` | 个股所属板块 |
| `AuctionItem` | time/price/matched/unmatched |
| `UnusualItem` | 市场异动 |
| `SymbolSnapshot` | 个股特征快照 |
| `GoodsItem` | 扩展市场商品 |
| `ServerInfo` | date/last_trade_date/sessions |
| `CapitalFlow` | 主力/散户买卖 + 5档资金 |
| `TdxBlock` | name/category(0行业1地域2概念3风格)/count/`codes: list[str]`（板块成分，来自 block_*.dat） |
| `IndustryInfo` | code/tdx_industry/sw_industry（来自 tdxhy.cfg） |
| `FinancialFileInfo` | filename/hash/filesize（来自 gpcw.txt） |
| `FinancialRecord` | code/market/report_date/`fields: list[float]`（历史专业财报，注意与 `FinanceRecord` 区分） |

> 注：`__init__` 仅导出 `Market/ExMarket/KlinePeriod/Adjust` 与 6 个核心模型；`BoardInfo/MemberQuote/AuctionItem` 等需 `from pytdxdata.models import ...`。

**`SecurityQuote.server_time`（行情快照时间，0.7.2 修正）**

- 格式 `HH:MM:SS.ff`，小数位数 = 该品种**价格小数位**：股票 2、ETF/LOF/B股 3、国债·回购 4。
  例 `13:22:40.02` / `13:22:45.903` / `13:10:22.8050`。
- 它比本地时钟**慢 5~15 秒**（服务器快照滞后），超低流动性品种（货币 ETF、国债）可能差几分钟。
  判断「行情有没有更新」请看这个值有没有变，别看调用时刻 —— 盘口本身约 3 秒刷新一次。
- **指数（`sh000001` / `sz399001` / `sh000300` …）恒为空串**：该字段对指数不是时钟
  （同一时刻 `000001` 解出 `12:89`、`399001` 解出 `12:15`，协议内没有可用信息）。
  要指数时间请用 `get_minutes` / K 线。
- ⚠️ 0.7.1 及之前按「小时 + 百万分之一小时」解码是**错的**：股票 `11:29:59` 会显示成
  `11:17:57`，ETF/债券会显示成 `113:00:21` 这种非法小时。

**量纲对照（跨品种，2026-10-22 实测）**

同名字段在不同品种上单位不同。最典型的坑是用 `amount / vol` 算均价——它在可转债上会**错 10 倍**：

| 品种 | 分时 / 逐笔 `vol` | 日线 `SecurityBar.vol` | `Σ分时vol / 日线vol` | 日线 `amount / vol` |
|---|---|---|---|---|
| 股票 / ETF / LOF | 手（= 100 股） | 股 | **0.01** | 均价 ✓ |
| 可转债（110/111/113/118/123/127/128） | 手（= 10 张） | **手** | **1.0** | 均价 **×10** ⚠️ |
| 指数 | 百元（成交额/100） | 手 | — | 无意义（`amount` 是全板块口径） |
| 港股主板 31 / 港股基金 49 | — | **手（= 100 股）** | — | 均价 **×100** ⚠️ |
| 港股创业板 48 / 新三板 44 / 美股 74 | — | 股 | — | 均价 ✓ |
| 期货 / 期权（EX 全市场） | — | 手/张 | — | **`amount` 恒 0.0**（帧内无成交额，见下） |

算均价：股票 `amount / vol`；可转债 `amount / vol / 10`；港股主板/基金 `amount / vol / 100`；跨品种统一用 `Σ(price×vol) / Σvol`。

> ⚠️ **EX 通道（0x23FF）的成交额不是全市场都有的**。一条记录 32B = 4B 时间 + 28B 行情，
> 28B 里只有**一个** 4B 槽位可复用，语义按市场分两类：
> 权益类（港股主板/创业板/基金、新三板、美股）该槽位是**成交额**，`amount` 正常；
> 期货期权类该槽位是**结算价**（体现在 `SecurityBar.settle`，日线才有值），
> `amount` 恒 **0.0** —— 这是协议限制，不是解析漏读，要成交额请改用 `get_quotes` 或其它源。
> 港股指数（27）/ 中证指数（62）该槽位语义未明，同样保守返回 `0.0`。

> 可转债的价格标度、分时、逐笔已全量校验（16 只当日 + 8 只历史日）：分时末价 / 快照现价、`Σvol / 日线vol`、逐笔 VWAP vs `amount/(vol×10)` 比值均为 **1.000000**。


## 四、辅助模块

**`pytdxdata.indicator`**（技术指标，纯函数）

| 函数 | 说明 |
|------|------|
| `ma(values, period)` | 移动平均 |
| `ema(values, period)` | 指数移动平均 |
| `macd(values, fast=12, slow=26, signal=9)` | MACD |
| `rsi(values, period=14)` | RSI |
| `kdj(high, low, close, ...)` | KDJ |
| `boll(values, period=20, mult=2.0)` | 布林带 |
| `compute_indicators(high, low, close, indicators, params=None, tail=None)` | 统一入口（被 `get_stock_kline_with_indicators` 调用） |

**`pytdxdata.config`**（服务器清单与选路）

主机来源优先级链（各协议独立）：`PYPYTDX_DATA_*_HOSTS` 环境变量 → **当日测速缓存** →
包内清单 `fast_pool` → 包内清单 → 代码内置常量。

| 函数 / 常量 | 说明 |
|------|------|
| `get_standard_hosts()` | 标准通道服务器列表（按上述优先级链解析） |
| `get_mac_hosts()` | MAC 通道服务器列表 |
| `get_ex_hosts()` | EX 扩展市场服务器列表 |
| `get_ex_handshakes()` | EX 握手方式映射 |
| `all_probe_candidates()` | 全量候选主机 `{standard, mac, ex}`（清单 ∪ 常量 ∪ env，**不按状态过滤**） |
| `is_valid_host(host)` | 主机名合法性校验（挡 mock 脏数据 / 非法条目） |
| `probe_cache_dir()` / `probe_cache_file()` | 用户级当日测速缓存目录 / 文件路径 |
| `load_probe_cache(today=None, path=None)` | 读当日测速缓存；缺失 / 损坏 / 非今日 / 版本不符 → `None` |
| `probe_disabled()` | 是否已用 `PYPYTDX_DATA_DISABLE_PROBE` 关闭测速 |
| `PROBE_CACHE_VERSION` | 测速缓存 payload 版本号 |

**`pytdxdata.pool.probe_cache`**（当日测速：后台执行、对用户无感）

| 符号 | 说明 |
|------|------|
| `ProbeCache` | 进程内协调器；`ensure_probe()` 为幂等入口（单飞 + 原子写） |
| `schedule_probe(hosts=None)` | 连接池用：挂后台 task 并返回该 task；无事件循环时返回 `None` |
| `get_probe_cache()` | 取进程级单例 |

**`pytdxdata.server_probe`**（离线服务器探测，`python -m pytdxdata.server_probe`）
`load_servers()` / `save_servers(cfg)` / `SERVERS_FILE` / `probe_one(server, timeout, discover)`
CLI 开关：`--discover`（三种协议全测并据实修正 `protocols`，同时剔除脏记录）/
`--group standard|mac|ex` / `--refresh`（只重测 `status=ok` 的）/ `--timeout`

## 五、CLI（`tdx` 命令，来自 `python -m pytdxdata.cli`）

| 命令 | 说明 |
|------|------|
| `tdx quotes <sym...>` | 行情报价（五档） |
| `tdx kline <sym> [--period day] [--count 30] [--start 0] [--adjust none]` | K线 |
| `tdx index <code> [--count 30]` | 指数K线 |
| `tdx transactions <sym> [--count 30] [--date YYYYMMDD]` | 逐笔成交 |
| `tdx minute <sym> [--date YYYYMMDD]` | 分时 |
| `tdx board list\|members\|ranking\|change [--type 1] [--count 30] [--top 10] [--days 20]` | 板块 |
| `tdx info <sym>` | 个股特征快照 |
| `tdx list [--market sz] [--count 30]` | 证券列表 |
| `tdx auction <sym>` | 集合竞价 |
| `tdx unusual [--market sh] [--count 30]` | 市场异动 |
| `tdx xdxr <sym>` | 除权除息 |
| `tdx finance <sym>` | 财务快照 |
| `tdx flow <sym>` | 资金流向 |
| `tdx block [filename] [--limit 30] [--codes]` | 板块文件 下载+解析（block_*.dat → 成分股） |
| `tdx industry [codes...] [--limit 30] [--search 词]` | 行业分类 下载+解析（tdxhy.cfg） |
| `tdx financial-list [--limit 30]` | 历史专业财报文件索引（gpcw.txt） |
| `tdx financial <filename> [--code X] [--limit 5] [--fields 4]` | 历史专业财报记录 下载+解析（文件名自动纠正，见 `get_financial_records_parsed`） |
| `tdx server-info` | 服务器交易时段 |
| `tdx market-stat` | 市场统计 |

**标的写法**：`sz000001` / `sh600000` / `bj920992` / `hk00700` / `usAAPL` / `cffex:IFL0` / `000001`（6位数字=指数）

## 六、通道路由（内部自动）

| 命令 | 通道 |
|------|------|
| K线（复权 QFQ/HFQ） | MAC |
| K线（MIN_1 不复权） | MAC（串行分页） |
| K线（其他周期）/ 指数 | 标准 |
| 报价 | 标准 |
| 逐笔成交 / 分时 | MAC |
| 列表 / 数量 / 除权 / 财务 | 标准 |
| 板块 / 竞价 / 异动 / 快照 / 资金流 / 服务器 / 分类报价 | MAC |
| 港股 / 美股 / 期货 / 期权 | EX（端口 7727，`0x23FF`） |

> EX 通道的 K 线（`0x23FF`）字段口径与 A 股不同，见「量纲对照」下的注：
> 权益类市场（港股/美股/新三板）有 `amount`；期货期权有 `settle`（结算价）但**没有** `amount`。

**多标的 K 线（`get_bars` 传 ≥16 只）额外走「批量流水线」**：按上表选池后，
多条连接并行、每条连接一次下发 `pipeline` 条请求（默认自适应 8~32），把 N 次往返
折叠成 `N/pipeline` 次。只用实测最快的 12 台服务器（池里 24 台全用会被慢节点拖长尾）。
`start_date=今天` 时只拉已走过的根数（10:00 → 30 根），盘中实时刷新因此只要 1~2 秒。
流水线失败时该批自动交回逐只重试，不会丢数据。
