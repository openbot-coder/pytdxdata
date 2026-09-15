# 接口总览

对外唯一入口是 **`TdxData`** 类，共 **51 个数据方法**
（另有 `start()` / `close()` 两个生命周期方法，以及 `__aenter__` / `__aexit__`）。
所有方法都可以直接 `await`，返回的是标准库 `dataclass` 组成的 `list`，没有自定义容器类型。

```python
from pytdxdata import TdxData
```

!!! tip "签名不用背"
    每个方法的完整签名、参数说明、返回值类型都在 **[TdxData 方法](tdxdata.md)** ，
    由 `mkdocstrings` **从源码 docstring 实时渲染**——不可能过期。
    本页只做「有哪些能力」的地图。

## 生命周期

| 方法 | 说明 |
|------|------|
| `async start()` | 启动：服务器探测 + 预建连接 + 初始化缓存 |
| `async close()` | 关闭全部连接池与缓存 |
| `async with TdxData() as td:` | 上下文管理器，自动 start / close |

## K 线（6）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_kline(market, code, period, *, start, count, adjust, start_date, end_date)` | `list[SecurityBar]` | 主接口。`count > 页大小` 自动并发分页；复权走 MAC；支持日期区间（内部二分定位） |
| `get_kline_since(market, code, period, since, adjust)` | `list[SecurityBar]` | 增量便捷方法（≥`since`，含当天） |
| `get_kline_batch(stocks, period, *, start, count, adjust, concurrency)` | `dict[str, list[SecurityBar]]` | 多股票并发，返回 `{code: bars}` |
| `get_index_kline(market, code, period, *, start, count)` | `list[SecurityBar]` | 指数 K 线（多涨跌家数字段已处理） |
| `get_stock_kline_with_indicators(market, code, indicators, ...)` | `dict` | K 线 + 技术指标（MACD / KDJ / RSI / BOLL / MA / EMA） |
| `get_kline_offset(offset, count)` | `tuple[int, int]` | K 线偏移信息（MAC） |

## 报价（4）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_quotes(stocks)` | `list[SecurityQuote]` | 五档报价；`>80` 只自动切批；TTL 5s |
| `get_stock_quotes(stocks, bits)` | `list[MemberQuote]` | MAC 自定义字段报价（≤80 只/次） |
| `get_stock_quotes_list(category, *, start, count, sort_type)` | `list[MemberQuote]` | 市场分类报价列表（0沪A / 6全A / 8科创 / 14创业） |
| `get_market_stat()` | `MarketStat` | 全市场统计（涨跌家数 / 总额 / 市值 / 涨跌停数） |

## 逐笔与分时（4）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_transactions(market, code, *, date, start, count)` | `list[TransactionRecord]` | 逐笔成交（MAC，**含 `trade_count` 成交笔数**），支持历史回溯 |
| `get_minute(market, code, *, date)` | `list[MinuteBar]` | 分时（今日 / 历史，MAC `0x122D`） |
| `get_minute_batch(stocks, *, date, concurrency)` | `dict[str, list[MinuteBar]]` | 批量分时 |
| `get_chart_sampling(market, code)` | `list[float]` | 分时缩略采样（A 股走 MAC `0x254D`） |

## 证券列表（3）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_security_count(market)` | `int` | 市场证券数量 |
| `get_security_list(market, *, start, count)` | `list[SecurityInfo]` | `count=None` = 全部（先查总数防死循环） |
| `get_security_list_all(market)` | `list[SecurityInfo]` | 全市场列表（高并发一次拉完，缓存 1 天） |

## 除权 / 财务 / F10 / 文件（15） {#file-methods}

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_xdxr(market, code)` | `list[XdxrRecord]` | 除权除息历史（标准） |
| `get_finance(market, code)` | `FinanceRecord \| None` | 财务快照（标准） |
| `get_price_limits(market, code, name, pre_close, listed_days)` | `tuple[float \| None, float \| None]` | 涨跌停价（**本地计算**，无网络） |
| `get_company_info_category(market, code)` | `list` | F10 目录 |
| `get_company_info_content(market, code, filename, offset, length)` | `str` | F10 内容（分块） |
| `get_block_info(filename)` | `bytes` | 板块文件**原始字节** |
| `get_report_file(filename)` | `bytes` | 报告 / 财务文件**原始字节** |
| `get_financial_file_list()` | `list[str]` | 财务文件列表**原始行** |
| `get_financial_records(filename)` | `bytes` | 财务文件内容**原始字节** |
| `get_file_meta(filename)` | `FileMeta` | 远程文件元信息（MAC `0x1215`） |
| `download_file(filename, filesize)` | `bytes` | 下载完整远程文件（MAC `0x1217`，分块） |
| **`get_block_parsed(filename)`** | `list[TdxBlock]` | 板块文件**下载 + 解析**为成分股 |
| **`get_industry_map()`** | `dict[str, IndustryInfo]` | `tdxhy.cfg` **下载 + 解析**：通达信 + 申万行业 |
| **`get_financial_file_infos()`** | `list[FinancialFileInfo]` | `gpcw.txt` **下载 + 解析**：文件名 / MD5 / 大小 |
| **`get_financial_records_parsed(filename)`** | `list[FinancialRecord]` | `gpcw*.zip` **解压 + 解析**：每股原始字段 |

!!! note "原始字节通道保留"
    返回 `bytes` 的那 4 个方法**保持向后兼容**，解析版是**新增**方法而不是替换。
    解析器在 `pytdxdata.codec`，纯标准库，可以脱网单独复用（见 [文件解析器](codec.md)）。

## 板块（6）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_board_list(board_type, count)` | `list[BoardInfo]` | 板块列表（0全部 / 1行业 / 2概念） |
| `get_board_members(board_symbol, count, sort_type)` | `list[MemberQuote]` | 板块成分股报价（如 `881001`） |
| `get_belong_board(market, code)` | `list[BelongBoard]` | 个股所属板块 |
| `get_board_summary(board_symbol)` | `dict` | 成分数 / 成交额 / 主力净流入 / 涨跌家数 |
| `get_board_ranking(board_type, top_n)` | `list[dict]` | 板块涨跌幅排行 |
| `get_board_change_ranking(board_type, days, top_n)` | `list[dict]` | 板块 N 日涨跌幅排行 |

## 竞价 / 异动 / 快照 / 资金流 / 服务器（5）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_auction(market, code)` | `list[AuctionItem]` | 集合竞价 |
| `get_unusual(market, start, count)` | `list[UnusualItem]` | 市场异动 |
| `get_symbol_info(market, code)` | `SymbolSnapshot` | 个股特征快照 |
| `get_capital_flow(market, code)` | `CapitalFlow` | 个股资金流向（主力 / 散户 + 5 档） |
| `get_server_info()` | `ServerInfo` | 服务器交易时段 |

## 扩展市场 EX：港股 / 美股 / 期货 / 期权（8）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_goods_list(market, start, count)` | `list[dict]` | 扩展市场商品列表（`0x23F5`，二分定位市场） |
| `get_goods_count(market)` | `int` | 商品数量（`None` = 全市场） |
| `get_goods_quotes(stocks, bits)` | `list[MemberQuote]` | 扩展市场报价（`0x122B`） |
| `get_goods_quotes_list(market, count)` | `list[MemberQuote]` | 商品列表 + 报价组合 |
| `get_goods_chart_sampling(market, code)` | `list[float]` | 扩展市场分时采样（`0x254D`） |
| `get_goods_tick_chart(market, code, ymd)` | `list[dict]` | 扩展市场分时 tick |
| `get_goods_transaction(market, code, *, ymd, start, count)` | `list[dict]` | 扩展市场逐笔（港股 `0x23FC` / `0x2406`） |
| `get_goods_transaction_all(market, code, ymd)` | `list[dict]` | 港股全量逐笔（自动翻页，≤50 页 / 9 万条） |

## 下一步

- [TdxData 方法](tdxdata.md) — 完整签名（自动生成）
- [枚举速查](enums.md) — 参数到底该填几
- [数据模型](models.md) — 返回对象有哪些字段
