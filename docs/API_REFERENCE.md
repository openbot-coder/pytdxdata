# pytdxdata 对外接口清单

> 以 `src/pytdxdata` 源码为准整理（v0.3.2）。分四类：Python API / 数据模型 / 辅助模块 / CLI。

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
| `pool_min` / `pool_max` | 3 / 12 | 动态池上下限 |
| `mac_pool_cap` | 4 | 单台 MAC 服务器连接上限 |

生命周期：

| 方法 | 说明 |
|------|------|
| `async start()` | 启动（服务器探测 + 预建连接 + 缓存） |
| `async close()` | 关闭全部池与缓存 |
| `async with TdxData() as td:` | 上下文管理器，自动 start/close |

### 2.1 K线（共 6 个）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_kline(market, code, period, *, start=0, count=800, adjust=None, start_date=None, end_date=None)` | `list[SecurityBar]` | 主接口；count>页自动并发分页；复权走 MAC；支持日期区间（二分定位） |
| `get_kline_since(market, code, period, since, adjust=None)` | `list[SecurityBar]` | 增量便捷方法（≥since，含当天） |
| `get_kline_batch(stocks, period, *, start=0, count=800, adjust=None, concurrency=6)` | `dict[str, list[SecurityBar]]` | 多股票并发，返回 {code: bars} |
| `get_index_kline(market, code, period, *, start=0, count=800)` | `list[SecurityBar]` | 指数K线（含涨跌家数字段） |
| `get_stock_kline_with_indicators(market, code, indicators, *, period=DAY, count=30, adjust=None, params=None)` | `dict` | K线+技术指标（MACD/KDJ/RSI/BOLL/MA/EMA） |
| `get_kline_offset(offset=0, count=1)` | `tuple[int, int]` | K线偏移信息（MAC） |

### 2.2 报价（共 4 个）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_quotes(stocks)` | `list[SecurityQuote]` | 五档报价；>80 只自动切批；TTL 5s |
| `get_stock_quotes(stocks, bits=None)` | `list[MemberQuote]` | MAC 自定义字段报价（≤80只/次） |
| `get_stock_quotes_list(category, *, start=0, count=80, sort_type=0)` | `list[MemberQuote]` | 市场分类报价（0沪A/2深A/6全A/8科创/14创业） |
| `get_market_stat()` | `MarketStat` | 全市场统计（涨跌家数/总额/市值/涨跌停数） |

### 2.3 逐笔成交 / 分时（共 4 个）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_transactions(market, code, *, date=None, start=0, count=2000)` | `list[TransactionRecord]` | 逐笔成交（MAC，含 trade_count），支持历史日期 |
| `get_minute(market, code, *, date=None)` | `list[MinuteBar]` | 分时（MAC 0x122D，可历史） |
| `get_minute_batch(stocks, *, date=None, concurrency=6)` | `dict[str, list[MinuteBar]]` | 批量分时 |
| `get_chart_sampling(market, code)` | `list[float]` | 分时缩略采样（A股，MAC 0x254D） |

### 2.4 证券列表（共 3 个）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_security_count(market)` | `int` | 市场证券数量 |
| `get_security_list(market, *, start=0, count=None)` | `list[SecurityInfo]` | count=None=全部（先查总数防死循环） |
| `get_security_list_all(market)` | `list[SecurityInfo]` | 全市场列表（高并发一次拉完，缓存 1 天） |

### 2.5 除权 / 财务 / F10 / 文件（共 15 个）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_xdxr(market, code)` | `list[XdxrRecord]` | 除权除息历史（标准） |
| `get_finance(market, code)` | `FinanceRecord \| None` | 财务快照（标准） |
| `get_price_limits(market, code, name, pre_close, listed_days=9999)` | `tuple[float\|None, float\|None]` | 涨跌停价（本地计算） |
| `get_company_info_category(market, code)` | `list` | F10 目录（标准） |
| `get_company_info_content(market, code, filename, offset=0, length=65535)` | `str` | F10 内容（分块） |
| `get_block_info(filename)` | `bytes` | 板块文件**原始字节**（先查元数据再分块下载） |
| `get_report_file(filename)` | `bytes` | 报告/财务文件**原始字节**（标准 0x06B9） |
| `get_financial_file_list()` | `list[str]` | 财务文件列表**原始行**（tdxfin/gpcw.txt） |
| `get_financial_records(filename)` | `bytes` | 财务文件内容（gpcw*.zip 原始字节） |
| `get_file_meta(filename)` | `FileMeta` | 远程文件元信息（MAC 0x1215） |
| `download_file(filename, filesize=0)` | `bytes` | 下载完整远程文件（MAC 0x1217，分块） |
| **`get_block_parsed(filename)`** | `list[TdxBlock]` | 板块文件**下载+解析**为板块成分（category 按文件名推断：0行业/2概念/3风格） |
| **`get_industry_map()`** | `dict[str, IndustryInfo]` | tdxhy.cfg **下载+解析**：`{代码: 通达信行业+申万行业}` |
| **`get_financial_file_infos()`** | `list[FinancialFileInfo]` | gpcw.txt **下载+解析**：文件名/MD5/大小 |
| **`get_financial_records_parsed(filename)`** | `list[FinancialRecord]` | gpcw*.zip **解压+解析**：每股原始 float 字段（报告期取自文件名，缺失回退头部） |

> 解析层独立于网络层：解析器位于 `pytdxdata.codec`（纯标准库 `struct`/`zipfile`），可脱网单测；
> 原 `get_*` 返回 `bytes`/`list[str]` 的方法保持**向后兼容**，解析版为**新增**方法。

### 2.6 板块（共 6 个）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_board_list(board_type=0, count=10000)` | `list[BoardInfo]` | 板块列表（0全部/1行业/2概念） |
| `get_board_members(board_symbol, count=100000, sort_type=0)` | `list[MemberQuote]` | 板块成分股报价（如 881001） |
| `get_belong_board(market, code)` | `list[BelongBoard]` | 个股所属板块 |
| `get_board_summary(board_symbol)` | `dict` | 汇总：成分数/成交额/主力净流入/涨跌家数 |
| `get_board_ranking(board_type=1, top_n=20)` | `list[dict]` | 板块涨跌幅排行 |
| `get_board_change_ranking(board_type=1, days=20, top_n=20)` | `list[dict]` | 板块 N 日涨跌幅排行（板块指数K线） |

### 2.7 MAC 扩展：竞价 / 异动 / 快照 / 资金流 / 服务器（共 5 个）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_auction(market, code)` | `list[AuctionItem]` | 集合竞价 |
| `get_unusual(market, start=0, count=600)` | `list[UnusualItem]` | 市场异动 |
| `get_symbol_info(market, code)` | `SymbolSnapshot` | 个股特征快照 |
| `get_capital_flow(market, code)` | `CapitalFlow` | 个股资金流向 |
| `get_server_info()` | `ServerInfo` | 服务器交易时段信息 |

### 2.8 EX 扩展市场（港股/美股/期货/期权，共 8 个）

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

**合计：`TdxData` 公开异步方法 51 个**（另有 `_execute_standard/_execute_mac/_execute_ex` 等内部执行方法）。

## 三、数据模型（`pytdxdata.models`）

**枚举**

| 名称 | 取值 |
|------|------|
| `Market` | SZ=0 / SH=1 / BJ=2 |
| `KlinePeriod` | MIN_5=0 / MIN_15=1 / MIN_30=2 / MIN_60=3 / DAY=4 / WEEK=5 / MONTH=6 / MIN_1=7 / MIN_3=8 / YEAR=9 / SEASON=10 / YEAR_ALT=11（`is_minute` 属性） |
| `Adjust` | NONE=0 / QFQ=1 / HFQ=2 |
| `ExMarket` | 52 个扩展市场：HK_MAIN_BOARD=31 / US_STOCK=74 / CFFEX_FUTURES=47 / ZZ_FUTURES=28 / DL_FUTURES=29 / SH_FUTURES=30 / GZ_FUTURES=66 / SH_STOCK_OPTION=8 / SZ_STOCK_OPTION=9 等 |

**dataclass（均为 `@dataclass(slots=True)`）**

| 模型 | 关键字段 |
|------|---------|
| `SecurityBar` | market/code/open/high/low/close/vol/amount/year/month/day/hour/minute + `datetime` 属性 |
| `IndexBar` | 继承 `SecurityBar`（指数K线） |
| `SecurityQuote` | price/pre_close/OHLC/vol/amount/s_vol/b_vol/`bid[5]`/`ask[5]`/server_time/trading_status/cur_vol/rise_speed/limit_up/limit_down/decimal_point/open_amount |
| `SecurityInfo` | market/code/name/volunit/decimal_point/pre_close |
| `TransactionRecord` | market/code/time/price/vol/`trade_count`/bs_flag |
| `MinuteBar` | price/vol/avg_price/hour/minute |
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

**`pytdxdata.config`**（服务器清单）

| 函数 | 说明 |
|------|------|
| `get_standard_hosts()` | 标准通道服务器列表 |
| `get_mac_hosts()` | MAC 通道服务器列表 |
| `get_ex_hosts()` | EX 扩展市场服务器列表 |
| `get_ex_handshakes()` | EX 握手方式映射 |

**`pytdxdata.server_probe`**（服务器探测，可 `python -m pytdxdata.server_probe` 运行）
`load_servers()` / `save_servers(cfg)` / `SERVERS_FILE`

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
| `tdx financial <filename> [--code X] [--limit 5] [--fields 4]` | 历史专业财报记录 下载+解析（gpcw*.zip） |
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
| 港股 / 美股 / 期货 / 期权 | EX（端口 7727） |
