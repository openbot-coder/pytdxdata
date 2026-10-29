# DataFrame 输出（pandas / polars）

0.7.0 起，`TdxData` 可以**直接把表格型接口的返回值变成 DataFrame**，不用再自己 `[asdict(x) for x in rows]`。

设计上只有一条开关，就是构造参数 `df`：

```python
from pytdxdata import TdxData, KlinePeriod

# 默认：df=None，行为和 0.6.x 完全一致，返回 list[dataclass]
async with TdxData() as td:
    bars = await td.get_bars("sh600000", KlinePeriod.DAY, count=240)   # list[SecurityBar]

# 要 DataFrame 就在构造时声明一次后端
async with TdxData(df="polars") as td:
    df = await td.get_bars("sh600000", KlinePeriod.DAY, count=240)     # polars.DataFrame

async with TdxData(df="pandas") as td:
    df = await td.get_bars("sh600000", KlinePeriod.DAY, count=240)     # pandas.DataFrame
```

## 安装

pandas / polars 都是**可选依赖**——`pytdxdata` 的 `dependencies` 仍然是空的，不想用 DataFrame 的人一点额外开销都不用付。

```bash
pip install "pytdxdata[polars]"     # 只要 polars
pip install "pytdxdata[pandas]"     # 只要 pandas
pip install "pytdxdata[dataframe]"  # 两个都要
```

没装就 `TdxData(df="polars")`，会在**构造时**直接报错，而不是等到第一次查询：

```
FrameBackendError: 未安装 polars，无法输出 DataFrame。请执行：pip install pytdxdata[polars]
```

`df` 取值非法（比如 `df="numpy"`）抛 `ValueError`，并列出可用值。

## 三个口径约定

### 1. 时间列：`datetime` 替换拆开的年月日时分

原始模型为了解析性能把时间拆成 `year/month/day/hour/minute`。DataFrame 里再这么摆，用的人第一件事就是自己拼时间戳——所以直接给一列 `datetime`：

| 模型 | 原始字段 | DataFrame 里的列 |
|---|---|---|
| `SecurityBar` / `IndexBar` | `year month day hour minute` | `datetime`（+ `trading_date`） |
| `MinuteBar` | `date hour minute day_offset` | `datetime`（+ `trading_date`） |
| `XdxrRecord` | `year month day` | `date` |

- `datetime` 的 dtype：polars 是 `Datetime('us')`，pandas 是 `datetime64[ns]`。
- `trading_date` 是 `YYYY-MM-DD` 字符串，**按交易日聚合要用它**：美股一个交易日横跨两个北京自然日（21:30→次日 04:00），凌晨段的 `datetime` 属于次日，但 `trading_date` 归属前一交易日。
- 除权记录的 `date` 是「日」不是「时刻」，值为当日 00:00，仍用 datetime 类型以便排序比较。
- `TransactionRecord.time` 只有 `HH:MM:SS`、**协议里没有日期**，所以逐笔不生成 `datetime`，`time` 原样保留。

### 2. 嵌套字段：能定型的定型，不能定型的 JSON 化

| 字段 | DataFrame 里的形态 |
|---|---|
| `SecurityQuote.bid` / `ask`（五档） | 展开成 `bid1_price` `bid1_vol` … `ask5_price` `ask5_vol` 共 **10 列**，缺档为 `None` |
| `fields`（A股字段位 / EX 额外字段） | **JSON 字符串**列；空字典给 `None`（不会看到一列 `"{}"`） |
| `TdxBlock.codes`（成分股列表） | 保持列表（polars 为 `List(Str)`，pandas 为 object），需要时 `.explode()` |

`fields` 之所以不展开：键集随市场和通道变化，展开会让列数失控。

### 3. 特殊容器会被展开成一张表

| 接口 | 原始返回 | DataFrame 形态 |
|---|---|---|
| `get_indicators` | `list[IndicatorSet]` | bars 列 + 每个指标一列，按长度对齐（不足补 `None`），`symbol` 取自 `IndicatorSet.symbol` |
| `get_index_trade_stats` | `IndexTradeStats` | `minutes` 展开成行，并补 `index` / `date` 两个常量列 |
| `get_index_trade_stats_multi` | `list[IndexTradeStats]` | 同上，多个指数纵向拼接 |
| `get_kline_batch` / `get_minute_batch` | `dict[标的, list[...]]` | 各标的分块纵向拼接成一张表（`symbol`/`code` 列本来就在行里） |
| `get_board_ranking` / `get_board_change_ranking` | `list[dict]` | 按首次出现顺序取键并集作为列名 |

## 哪些接口不受影响

`df` 只作用于**表格型返回**。下面这些原样返回，不转 DataFrame：

| 返回类型 | 接口 |
|---|---|
| `bytes` | `get_file` `get_block_info` `get_report_file` `download_file` `get_financial_records` |
| `int` | `get_security_count` `get_goods_count` |
| `dict` | `decimal_point_status` `get_board_summary` `get_industry_map` |
| 单个对象 | `get_server_info` `get_market_stat` `get_symbol_info` |
| `tuple` | `get_price_limits` `get_kline_offset` |
| `list[float]` / `list[str]` | `get_chart_sampling` `get_goods_chart_sampling` `get_financial_file_list` |

理由是单行表没有意义，而 `bytes`/`int` 根本不是表格数据。

## 空结果

查不到数据时返回的是**列名齐全的空 DataFrame**（列由模型定义推出，不依赖是否拿到行）：

```python
async with TdxData(df="polars") as td:
    df = await td.get_xdxr("sz000001")   # 假设当天无除权
    df.height, df.columns                # 0, ['market', 'code', 'date', ...]
```

⚠️ 空表只保证**列名**正确，dtype 由后端自行推断（polars 可能全为 `Null`，pandas 为 object）。

## 语义提醒

DataFrame 只是换了个容器，**下面这些口径不会因为转成 DataFrame 而变化**：

- 股票/ETF 的 `vol` 是「手」，日线是「股」；可转债 `vol` 手=10 张，`amount/vol` 是均价 **×10**；指数逐笔 `vol` 是百元。
- 指数和 ETF/LOF 的 `trade_count` 不是笔数（见 `TransactionRecord.trade_count_available`）。
- 深市 `399xxx` 的 `amount` 是全板块口径，不要拿去校验成分合计。
- K 线的 `settle`（结算价）列只有 **EX 通道的期货/期权日线**有值，其余行全为 `None`；
  同一批 EX 标的中，期货/期权的 `amount` 恒 `0.0`（`0x23FF` 帧内没有成交额），
  港股主板/基金的 `vol` 单位是「手」（`amount/vol` 是均价的 100 倍）。

细节见 [数据模型](api/models.md) 与 [常见问题](faq.md)。

## 性能

先说结论：**`df=` 只影响出参那一层，取数、连接池、缓存完全不变**，但转换本身不是免费的——
结果集越大越明显。实测（缓存已热、同一台机器、Windows / Python 3.13）：

| 场景 | `df=None` | `df="polars"` | `df="pandas"` |
|---|---|---|---|
| 全市场报价 5,225 只 | 66 ms | 117 ms（+51） | 124 ms（+58） |
| 1,000 只 × 240 根 1 分钟（24 万行） | 1,386 ms | 1,768 ms（+383） | 1,976 ms（+590） |
| 单只 × 240 根 1 分钟 | 1 ms | 2 ms | 2 ms |

只看转换层本身（不碰网络）：5,225 行报价 → polars **51 ms** / pandas **62 ms**；
24 万行 K 线 → polars **444 ms** / pandas **585 ms**。

几个要点：

- **小结果无感**：单只 K 线、单只报价这类常见调用只多 1 ms 左右。
- **大结果按行数线性增长**：每行约 2~3 µs（12 列）。全市场 1 分钟线（约 125 万行）
  转换约 2.3 s（polars）/ 3.1 s（pandas）——如果你的链路是「全市场分钟线直落库」，
  这笔开销通常远小于下游写入，但如果只是做筛选/聚合，建议先拿 `df=None` 的 list 算完再转。
- **polars 比 pandas 快 25~30%**（同数据、同机器），大结果更明显。
- **转换是列式采集**：列计划按模型缓存（含交易日列的按时间字段缓存），
  普通列走 C 层取值，不做逐行 `asdict()`。
- **`df=None` 时完全不经过这一层**：装饰器直接透传，零额外开销。
- **缓存命中也要转**:磁盘/内存缓存里存的是 dataclass 记录，DataFrame 是每次调用现建的
  ——高频轮询同一批数据时，这笔转换开销每次都会付。
