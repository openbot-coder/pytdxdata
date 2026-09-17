# 接口总览

对外唯一入口是 **`TdxData`** 类，所有方法都可直接 `await`，返回标准库 `dataclass` 组成的 `list`。

**v0.6+** 推荐使用**统一接口**（一个数据类型 = 一个方法，跨市场自动选路）；
旧的按通道/市场分裂的接口仍可调用，但会发出 `DeprecationWarning`，**1.0 移除**。

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

---

## 统一接口（v0.6+ 推荐）{#unified}

标的一律用**字符串写法**（`sz000001` / `hk00700` / `usAAPL` / `cffex:IFL0`，
裸 6 位数字 = 沪市指数）。跨市场按通道自动分组并发，返回值统一带 `symbol` 字段。

### 标的清单

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_universe(market=None)` | `list[SecurityInfo]` | 跨市场标的清单（A 股 + 港股 + 美股 + 期货/期权），EX 通道结果一次拉全后内存过滤 |

### 报价

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_quotes(symbols, *, fields=None)` | `list[SecurityQuote]` | 跨市场混合报价（A 股标准通道有五档 bid/ask + 涨跌停价；给了 `fields` 走 MAC/EX 字段位通道有 name） |
| `get_snapshot(symbols)` | `list[SymbolSnapshot]` | 个股特征快照（当日/换手/活跃度，A 股） |
| `get_auction(symbols)` | `list[AuctionItem]` | 集合竞价（A 股） |
| `get_capital_flow(symbols)` | `list[CapitalFlow]` | 个股资金流向（A 股） |
| `get_market_stat()` | `MarketStat` | 全市场统计（涨跌家数 / 总额 / 市值 / 涨跌停数） |

### K 线

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_bars(symbols, period, *, start, count, adjust, start_date, end_date, concurrency)` | `list[SecurityBar]` | 跨市场 K 线（自动判指数、复权走 MAC、并发分页、日期区间二分定位，支持混合 A 股+港股+期货） |
| `get_indicators(symbols, indicators, *, period, count, adjust, params)` | `list[IndicatorSet]` | K 线 + 技术指标（MACD / KDJ / RSI / BOLL / MA / EMA），返回 `bars + indicators` 组合 |

### 逐笔与分时

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_ticks(symbols, *, date, start, count, concurrency)` | `list[TransactionRecord]` | 跨市场逐笔成交（A 股 MAC `0x122F` 含 `trade_count`；港股 EX `0x23FC` / `0x2406`；其他扩展市场 MAC） |
| `get_minutes(symbols, *, date, sampling, concurrency)` | `list[MinuteBar]` | 跨市场分时（A 股 MAC `0x122D` 支持历史；`sampling=True` 取缩略采样约 240 点） |

### 公司信息 / 除权 / 财务 / 文件

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_f10(symbol, section=None, *, offset, length)` | `list \| str` | F10 公司资料（`section=None` 返回目录，给了返回正文） |
| `get_xdxr(symbols)` | `list[XdxrRecord]` | 除权除息历史（A 股） |
| `get_finance(symbols)` | `list[FinanceRecord]` | 财务快照（A 股，返回列表，无数据的标的跳过） |
| `get_price_limits(symbol, name, pre_close, listed_days)` | `tuple[float\|None, float\|None]` | 涨跌停价（本地计算，无网络） |
| `get_file(name)` | `bytes` | 服务器文件原始字节（`block_*.dat` / `tdxhy.cfg` / `tdxfin/*` / MAC 远程文件） |
| `get_block_parsed(filename)` | `list[TdxBlock]` | 板块文件**下载 + 解析**为成分股 |
| `get_industry_map()` | `dict[str, IndustryInfo]` | `tdxhy.cfg` **下载 + 解析** |
| `get_financial_file_infos()` | `list[FinancialFileInfo]` | `gpcw.txt` **下载 + 解析** |
| `get_financial_records_parsed(filename)` | `list[FinancialRecord]` | `gpcw*.zip` **解压 + 解析** |

### 市场 / 板块

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_unusual(market="all", *, start, count)` | `list[UnusualItem]` | 市场异动（A 股，`"all"` = 深 + 沪） |
| `get_server_info()` | `ServerInfo` | 服务器交易时段信息 |
| `get_kline_offset(offset, count)` | `tuple[int, int]` | K 线偏移信息（MAC） |
| `get_boards(kind="all", *, count)` | `list[BoardInfo]` | 板块列表（`"all"` / `"industry"` / `"concept"` / `"style"`） |
| `get_board_members(board, *, fields, count)` | `list[SecurityQuote]` | 板块成分股报价（返回归一报价模型） |
| `get_board_of(symbols)` | `list[BelongBoard]` | 个股所属板块（A 股） |
| `get_board_summary(board)` | `dict` | 板块汇总（成分数 / 成交额 / 主力净流入 / 涨跌家数） |
| `get_board_ranking(kind="industry", *, top_n)` | `list[dict]` | 板块当日涨跌幅排行 |
| `get_board_change_ranking(kind="industry", *, days, top_n)` | `list[dict]` | 板块 N 日涨跌幅排行 |

---

## 旧接口（deprecated，1.0 移除）{#legacy}

旧接口按通道/市场分裂（33 个 deprecated 别名），保留向后兼容但仍可用。
完整旧方法列表见 `docs/API_REFERENCE.md`。

!!! warning "迁移建议"
    各通道独立方法（如 `get_kline` / `get_transactions`）在 v0.6 内仍可调用，且仍接受
    `(market, code)` 写法，但会发出 `DeprecationWarning`。
    统一接口（`get_quotes` / `get_bars` / `get_ticks` / `get_xdxr` / `get_finance` /
    `get_auction` / `get_capital_flow` 等）一律只接受字符串标的（如 `"sz000001"`）。
    迁移对照见 [更新日志](../changelog.md)。

## 下一步

- [TdxData 方法](tdxdata.md) — 完整签名（自动生成）
- [枚举速查](enums.md) — 参数到底该填几
- [数据模型](models.md) — 返回对象有哪些字段
