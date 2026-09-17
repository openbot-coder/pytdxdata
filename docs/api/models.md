# 数据模型

全部数据模型位于 `pytdxdata.models`，统一为 `@dataclass(slots=True)`。

选择 dataclass 而不是 pandas DataFrame 是刻意的：**零依赖 + 零转换开销**，
需要 DataFrame 时你自己转（一行的事，见 [场景示例 → 转成 pandas](../cookbook.md#to-pandas)）。

## 导入方式

包级只导出 4 个枚举 + 6 个核心模型：

```python
from pytdxdata import Market, ExMarket, KlinePeriod, Adjust
from pytdxdata import (
    SecurityBar, SecurityQuote, TransactionRecord,
    MinuteBar, XdxrRecord, FinanceRecord,
)
```

其余模型需要从子模块导入：

```python
from pytdxdata.models import TdxBlock, IndustryInfo, FinancialFileInfo, FinancialRecord
from pytdxdata.models import BoardInfo, MemberQuote, AuctionItem, CapitalFlow
```

## 模型地图

| 模型 | 代表什么 | 从哪来 |
|---|---|---|
| `SecurityBar` | 一根 K 线 | `get_bars` / `get_indicators` |
| `IndexBar` | 指数 K 线（继承 `SecurityBar`） | `get_bars` |
| `SecurityQuote` | 五档报价快照 | `get_quotes` / `get_board_members` |
| `SecurityInfo` | 证券列表条目（代码 / 名称 / 前收） | `get_universe` |
| `TransactionRecord` | 一笔成交 | `get_ticks` |
| `MinuteBar` | 一分钟分时点 | `get_minutes` |
| `XdxrRecord` | 一次除权除息事件 | `get_xdxr` |
| `FinanceRecord` | 财务**快照**（最新一期，36 字段） | `get_finance` |
| `FinancialRecord` | 历史专业财报（`gpcw*.zip`，多报告期） | `get_financial_records_parsed` |
| `FinancialFileInfo` | 财报文件索引条目 | `get_financial_file_infos` |
| `TdxBlock` | 一个板块及其成分股 | `get_block_parsed` |
| `IndustryInfo` | 一只股票的行业归属 | `get_industry_map` |
| `BoardInfo` | 板块列表条目（含领涨股） | `get_boards` |
| `MemberQuote` | 成分股报价（MAC 原始字段） | 旧 `get_stock_quotes` / `get_stock_quotes_list` |
| `BelongBoard` | 个股所属板块 | `get_board_of` |
| `AuctionItem` | 集合竞价撮合点 | `get_auction` |
| `UnusualItem` | 一笔市场异动 | `get_unusual` |
| `SymbolSnapshot` | 个股特征快照 | `get_snapshot` |
| `GoodsItem` | 扩展市场商品 | 旧 `get_goods_list` |
| `ServerInfo` | 服务器交易时段 | `get_server_info` |
| `CapitalFlow` | 个股资金流向 | `get_capital_flow` |
| `MarketStat` | 全市场统计 | `get_market_stat` |
| `FileMeta` | 远程文件元信息 | `get_file_meta` |

!!! warning "`FinanceRecord` vs `FinancialRecord` 只差一个字母"
    但它们完全不同：

    | | `FinanceRecord` | `FinancialRecord` |
    |---|---|---|
    | 来源 | `get_finance()` — 标准通道 | `get_financial_records_parsed()` — 服务器文件 |
    | 内容 | **最新一期**财务快照 | **历史多报告期**专业财报 |
    | 结构 | 具名属性（`zongguben`、`jingzichan` ...） | `fields: list[float]`，原始字段数组 |
    | 单位 | 万元 / 万股 | 原始 float，需自行按字段语义换算 |

## 完整字段定义

以下由 `mkdocstrings` 从源码实时渲染（**以源码为准**）：

::: pytdxdata.models
    options:
      members_order: source
      show_root_heading: false
      show_root_toc_entry: false
      filters:
        - "!^_"

## 下一步

- [枚举速查](enums.md) — `market` / `period` 参数取值
- [TdxData 方法](tdxdata.md) — 哪个方法返回哪个模型
