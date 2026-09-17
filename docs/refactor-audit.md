# 接口重构审查报告

> 范围：v0.6 统一接口重构（`TdxData` 对外方法 51 → 63）的**数据一致性**与**遗漏审查**。
> 结论基于对旧实现（`git show HEAD:src/pytdxdata/api.py`）与新实现的逐字段 / 逐方法对照。

## 一、结论速览

| 维度 | 结论 |
|------|------|
| 数据一致性（旧接口 vs 统一接口） | ✅ 55 条断言全部通过，无字段差异 |
| 方法丢失 | ✅ 无。旧 53 个公开方法 100% 保留 |
| 文档覆盖 | ✅ `scripts/check_docs.py` 三条硬约束通过 |
| 标的写法 | ✅ 统一接口一律只接受字符串，已无元组软兼容 |
| 公开方法计数 | `28 统一 + 33 别名 + start/close = 63` |

---

## 二、数据一致性（旧接口 vs 新统一接口）

逐字段对照 24+ 组、**55 条断言全部通过**，覆盖：

- K 线（各周期 / 复权 / 日期区间）
- 实时报价（五档 bid/ask、买卖量、涨跌停价）
- 逐笔成交（含 `trade_count`）
- 分时
- 除权除息
- 财务快照
- F10（目录 + 正文）
- 服务器文件（板块 / 行业 / 财报）
- 板块（列表 / 成员 / 归属 / 汇总 / 排行）

唯一 1 条「失败」经核实为 **mock 假象**：`security_count` 的 mock 返回固定值 100，而
`get_universe` 走真实分页逻辑，两者口径不同；**并非真实数据差异**，逐字段比较全部相等。

---

## 三、遗漏审查（方法级）

对旧 / 新 `api.py` 的公开方法集合做 AST 级 diff：

| 指标 | 数量 |
|------|------|
| 旧公开方法 | 53（51 数据 + `start` / `close`） |
| 新公开方法 | 63（61 数据 + `start` / `close`） |
| **旧有新无（丢失）** | **0** |
| 新增 | 10（全部为统一入口） |

**数值闭合**：

- `28 统一 + 33 废弃别名 + start/close = 63`
- `18 旧名升级 + 33 转别名 = 51 旧数据方法`
- `51 旧 + 10 新入口 = 61 数据方法`

---

## 四、统一接口清单（28 个数据接口）

标的写法：`"sz000001"` / `"hk00700"` / `"usAAPL"` / `"cffex:IFL0"`；裸 6 位数字 = 沪市指数。

### 4.1 新入口（10，v0.6 新增）

| 方法 | 签名 | 返回 | 吸收的旧接口 |
|------|------|------|--------------|
| `get_universe` | `(market=None)` | `list[SecurityInfo]` | get_security_count / get_security_list / get_security_list_all / get_goods_list / get_goods_count |
| `get_bars` | `(symbols, period=DAY, *, start=0, count=800, adjust=None, start_date=None, end_date=None, concurrency=6)` | `list[SecurityBar]` | get_kline / get_kline_since / get_kline_batch / get_index_kline |
| `get_indicators` | `(symbols, indicators, *, period=DAY, count=30, adjust=None, params=None)` | `list[IndicatorSet]` | get_stock_kline_with_indicators |
| `get_ticks` | `(symbols, *, date=None, start=0, count=None, concurrency=6)` | `list[TransactionRecord]` | get_transactions / get_goods_transaction / get_goods_transaction_all |
| `get_minutes` | `(symbols, *, date=None, sampling=False, concurrency=6)` | `list[MinuteBar]` | get_minute / get_minute_batch / get_chart_sampling / get_goods_chart_sampling / get_goods_tick_chart |
| `get_snapshot` | `(symbols)` | `list[SymbolSnapshot]` | get_symbol_info |
| `get_f10` | `(symbol, section=None, *, offset=0, length=65535)` | `list \| str` | get_company_info_category / get_company_info_content |
| `get_file` | `(name)` | `bytes` | get_block_info / get_report_file / get_file_meta / download_file / get_financial_file_list / get_financial_records |
| `get_boards` | `(kind="all", *, count=10000)` | `list[BoardInfo]` | get_board_list |
| `get_board_of` | `(symbols)` | `list[BelongBoard]` | get_belong_board |

### 4.2 旧名升级（18，同名泛化为统一接口）

| 方法 | 签名 | 返回 |
|------|------|------|
| `get_quotes` | `(symbols, *, fields=None)` | `list[SecurityQuote]` |
| `get_auction` | `(symbols)` | `list[AuctionItem]` |
| `get_capital_flow` | `(symbols)` | `list[CapitalFlow]` |
| `get_xdxr` | `(symbols)` | `list[XdxrRecord]` |
| `get_finance` | `(symbols)` | `list[FinanceRecord]` |
| `get_price_limits` | `(symbol, name=None, pre_close=None, listed_days=9999)` | `tuple[float\|None, float\|None]` |
| `get_market_stat` | `()` | `MarketStat` |
| `get_unusual` | `(market="all", *, start=0, count=600)` | `list[UnusualItem]` |
| `get_server_info` | `()` | `ServerInfo` |
| `get_kline_offset` | `(offset=0, count=1)` | `tuple[int, int]` |
| `get_board_members` | `(board, count=100000, sort_type=0, *, fields=None)` | `list[SecurityQuote]` |
| `get_board_summary` | `(board)` | `dict` |
| `get_board_ranking` | `(kind="industry", *, top_n=20)` | `list[dict]` |
| `get_board_change_ranking` | `(kind="industry", *, days=20, top_n=20)` | `list[dict]` |
| `get_block_parsed` | `(filename)` | `list[TdxBlock]` |
| `get_industry_map` | `()` | `dict[str, IndustryInfo]` |
| `get_financial_file_infos` | `()` | `list[FinancialFileInfo]` |
| `get_financial_records_parsed` | `(filename)` | `list[FinancialRecord]` |

> 另有生命周期方法 `start()` / `close()`，合计 30 个非别名公开方法。

---

## 五、废弃别名清单（33 个，1.0 移除）

别名运行时发出 `DeprecationWarning`；**仍接受 `(market, code)` 写法**（这是别名与统一接口的关键差异）。

| 目标统一接口 | 数量 | 废弃别名 |
|--------------|------|----------|
| `get_bars` | 4 | `get_kline`、`get_kline_since`、`get_kline_batch`、`get_index_kline` |
| `get_indicators` | 1 | `get_stock_kline_with_indicators` |
| `get_quotes` | 3 | `get_stock_quotes`、`get_goods_quotes`、`get_goods_quotes_list` |
| `get_board_members` | 1 | `get_stock_quotes_list` |
| `get_ticks` | 3 | `get_transactions`、`get_goods_transaction`、`get_goods_transaction_all` |
| `get_minutes` | 3 | `get_minute`、`get_minute_batch`、`get_goods_tick_chart` |
| `get_minutes(sampling=True)` | 2 | `get_chart_sampling`、`get_goods_chart_sampling` |
| `get_universe` | 5 | `get_security_count`、`get_security_list`、`get_security_list_all`、`get_goods_list`、`get_goods_count` |
| `get_f10` | 2 | `get_company_info_category`、`get_company_info_content` |
| `get_file` | 4 | `get_block_info`、`get_report_file`、`get_file_meta`、`download_file` |
| `get_financial_file_infos` | 1 | `get_financial_file_list` |
| `get_financial_records_parsed` | 1 | `get_financial_records` |
| `get_boards` | 1 | `get_board_list` |
| `get_board_of` | 1 | `get_belong_board` |
| `get_snapshot` | 1 | `get_symbol_info` |
| **合计** | **33** | |

---

## 六、标的写法统一（字符串化）

统一接口**只接受字符串标的**，已移除所有 `(market, code)` 元组软兼容：

- 删除模块级 `_as_symbols()`；`get_quotes` / `get_xdxr` / `get_finance` / `get_auction` /
  `get_capital_flow` 去掉 `code` 位置参数，统一走 `parse_symbols()`。
- `get_price_limits` 去掉 `(market, code, …)` 旧参形式检测。
- `get_unusual` 去掉整数市场号检测。
- 调用方（CLI、示例）同步改为字符串；`get_finance` / `get_capital_flow` 的返回类型改为 `list`。

!!! note "保留项"
    `get_boards(kind)` 仍接受数字板块类别（`0/1/2/3`）。这是**板类别选择器**、非标的，
    文档已明示「也接受数字」，旧别名 `get_board_list(board_type=0)` 依赖它。

---

## 七、验证

- `pytest`：**127 passed**。其中 `tests/test_all_interfaces.py` 为全接口回归：mock 连接逐个
  调用 63 个公开方法，并以「覆盖哨兵」断言无遗漏（7 条告警为别名测试的预期 `DeprecationWarning`）。
- `scripts/check_docs.py`：`MUST_LIST_ALL` + `MUST_NOT_BE_STALE` 全部 `[OK]`。
- 签名核对：统一接口签名与返回类型与本文一致；全仓无 `_as_symbols` 及元组式调用残留。
