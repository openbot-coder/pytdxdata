---
name: pytdxdata
description: 用 pytdxdata 拉取通达信行情数据——A股(沪/深/北)、港股、美股、期货、期权的 K线 / 报价 / 逐笔 / 分时 / 板块 / 除权 / 财报文件。触发词：通达信数据、tdx 行情、A股日线下载、分钟K线、批量拉K线、板块成分股、行业分类、港股美股行情、pytdx、easy-tdx、pytdxdata。
---

# pytdxdata 使用技能

`pytdxdata` 是通达信公开行情服务器的纯 asyncio 客户端：动态连接池 + 双层 TTL 缓存 + 多连接并发分页，**零运行时依赖**。
本技能让你一次写对——重点是那三条一眼看不出来的约定，以及一张必须背下来的枚举表。

> 需要精确签名时查 <https://openbot-coder.github.io/pytdxdata/api/tdxdata/>（由 docstring 实时生成，不可能过期）。

## 三条必守约定

**1. 唯一入口是 `TdxData`，且必须 `async with`**

```python
from pytdxdata import TdxData

async with TdxData() as td:      # 自动 start（探测服务器 + 预建连接 + 建缓存）
    bars = await td.get_bars(["sz000001"], KlinePeriod.DAY, count=240)
# 退出即 close
```

不要直接 `await TdxData().get_bars(...)`——没 `start()` 就没有连接池。

**2. 标的写法是字符串，不是元组**

这是 v0.6 的最重要变化。**不要传 `(Market.SH, "600000")` 元组**，用字符串：

```python
# 旧写法（deprecated，会发出 DeprecationWarning）：
bars = await td.get_kline(Market.SH, "600000", KlinePeriod.DAY, count=240)

# 新写法（推荐）：
bars = await td.get_bars(["sz000001"], KlinePeriod.DAY, count=240)
# 港股/美股/期货也一样：
bars = await td.get_bars(["hk00700", "usAAPL", "cffex:IFL0"], KlinePeriod.DAY, count=240)
```

**3. 返回 `list[dataclass]`，不是 DataFrame**

库刻意不依赖 pandas。要 DataFrame 自己转一行：

```python
from dataclasses import asdict
import pandas as pd
df = pd.DataFrame([asdict(b) for b in bars])
```

## 安装

```bash
pip install pytdxdata        # 或 uv add pytdxdata
pip install "pytdxdata[polars]"   # 需要 DataFrame 输出时才装（可选 extras）
```

## 可运行模板

```python
import asyncio
from pytdxdata import TdxData
from pytdxdata.models import KlinePeriod, Adjust

async def main():
    async with TdxData() as td:
        # 日K线，240 根（count 超过单页大小会自动多连接并发分页）
        bars = await td.get_bars(["sz000001"], KlinePeriod.DAY, count=240)
        print(bars[-1].close, bars[-1].datetime)

        # 前复权日线（复权自动走 MAC 通道）
        qfq = await td.get_bars(["sz300308"], KlinePeriod.DAY,
                                 count=500, adjust=Adjust.QFQ)

        # 跨市场报价：A股+港股+美股一次拿全
        quotes = await td.get_quotes(["sz000001", "hk00700", "usAAPL"])
        print(quotes[0].price, quotes[1].name, quotes[2].amount)

        # 逐笔成交（MAC 通道，支持历史日期，含 trade_count 成交笔数）
        ticks = await td.get_ticks(["sz000001"], date=20260811)
        print(sum(r.trade_count for r in ticks))

        # 分时（MAC 通道，今日或历史）
        minutes = await td.get_minutes(["sz000001"])

        # 批量 K 线：多股票自动并发，池子会分配到不同服务器
        batch = await td.get_bars(
            ["sz000001", "sh600000"],
            KlinePeriod.DAY, count=240,
        )  # 平铺返回，每条带 .symbol 字段

asyncio.run(main())
```

## 枚举取值表（别猜，对照着填）

**`Market`** — A 股市场

| 值 | 名称 | 含义 |
|---|---|---|
| 0 | `SZ` | 深市 |
| 1 | `SH` | 沪市 |
| 2 | `BJ` | 北交所 |

**`KlinePeriod`** — 周期编码，**与通达信协议字节一致，不是顺序编号**

| 值 | 名称 | | 值 | 名称 |
|---|---|---|---|---|
| 0 | `MIN_5` | | 7 | `MIN_1` |
| 1 | `MIN_15` | | 8 | `MIN_3` |
| 2 | `MIN_30` | | 9 | `YEAR` |
| 3 | `MIN_60` | | 10 | `SEASON` |
| 4 | `DAY` | | 11 | `YEAR_ALT` |
| 5 | `WEEK` | | | |
| 6 | `MONTH` | | | |

⚠️ **`DAY=4` 不是 0**，`MIN_1=7` 也不是 0。凭直觉填 `0` 会拿到 5 分钟线。

**`Adjust`** — 复权方式

| 值 | 名称 | 含义 |
|---|---|---|
| 0 | `NONE` | 不复权 |
| 1 | `QFQ` | 前复权 |
| 2 | `HFQ` | 后复权 |

**`ExMarket`** — 扩展市场（港股 / 美股 / 期货 / 期权，共 46 个成员，走独立 EX 通道）

v0.6 起统一接口用**字符串前缀**指定扩展市场，一般不再需要 `ExMarket`：

```python
await td.get_bars("hk00700", KlinePeriod.DAY, count=700)      # 港股（5 位代码）
await td.get_bars("usAAPL", KlinePeriod.DAY, count=700)       # 美股（ticker）
await td.get_bars("cffex:IFL0", KlinePeriod.DAY, count=700)   # 期货合约
```

常用前缀：`hk` / `us` / `cffex` / `shf` / `dl` / `zz` / `gz`；完整表见 <https://openbot-coder.github.io/pytdxdata/api/enums/>。

## 想做什么 → 用哪个方法

| 需求 | 方法 |
|---|---|
| 全市场标的清单（A股+港股+期货…；**北交所返回空**） | `get_universe(market=None)` |
| 日/周/月/分钟 K 线（任意市场） | `get_bars(symbols, period, *, count, adjust, start_date, end_date)` |
| K 线 + MACD/KDJ/RSI/BOLL | `get_indicators(symbols, indicators, ...)` |
| 实时五档报价（A股）/ 基础报价（港股/美股/期货） | `get_quotes(symbols, *, fields=None, pipeline=True)` |
| 逐笔成交（任意市场） | `get_ticks(symbols, *, date)` |
| 分时图 / 缩略采样 | `get_minutes(symbols, *, date, sampling=True)` |
| 全市场股票列表（缓存 1 天） | `get_universe("sz")` / `get_universe("sh")` |
| ~~北交所全量代码~~ | 拿不到：清单命令对 market=2 无响应（27 台全测）。北交所标的仍可直接查 `bj920992` |
| 除权除息 / 财务快照 | `get_xdxr(symbols)` / `get_finance(symbols)` |
| F10 公司资料 | `get_f10(symbol, section)` |
| 板块列表 / 成分股 / 涨跌排行 | `get_boards` / `get_board_members` / `get_board_ranking` |
| **指数的成交量 + 成交笔数**（每分钟曲线） | `get_index_trade_stats(index, date)` → `.cum_trade_count()` |
| **指数成分股** | `get_index_members(index)`（裸 `000001` 按指数语义解析） |
| 个股所属板块 | `get_board_of(symbols)` |
| 集合竞价 / 异动 / 资金流 | `get_auction` / `get_unusual` / `get_capital_flow` |
| 个股特征快照 | `get_snapshot(symbols)` |
| **板块成分文件**（离线快照） | `get_block_parsed("block_gn.dat")` |
| **行业分类**（通达信 + 申万） | `get_industry_map()` |
| **历史专业财报**（gpcw 全字段） | `get_financial_file_infos()` + `get_financial_records_parsed(fn)` |
| 涨跌停价 | `get_price_limits(symbol, name, pre_close, listed_days)`（本地算，无网络） |
| 全市场涨跌家数/成交额 | `get_market_stat()` |

## 通道与数据特性（影响你能拿到什么）

库内部有**标准通道**和 **MAC 通道**两条链路，会**按命令自动选路**，你不用管。但有几件事必须知道：

- **数据有 15 分钟延时**（通达信公开服务器特性）。要实时数据请用券商行情。
- **复权 K 线与 1 分钟 K 线走 MAC 通道**。`get_bars(..., adjust=Adjust.QFQ)` 自动切换。
- **K 线时间戳统一为北京时间**（v0.6.2 起）。美股原始报文里日期是美东交易日、时分是北京时间（两个时区拼在一起），库已自动归一化，`bar.datetime` 可直接排序 / 比较 / 过滤。但**美股一个交易日横跨两个北京自然日**，按交易日聚合（分钟线合成日线、日内统计）必须用 `bar.trading_date`。
- **`count` 很大时会自动多连接并发分页**，无需手动翻页。但标准通道 K 线 `count < 100` 部分服务器会返回空——内部已强制 `min_page=100` 规避。
- **`get_quotes` 一次 >80 只自动切批**；走 MAC 字段位通道（`fields=`）时上限为 80 只/次。
- **全市场报价用默认参数即可，别自己写循环**。`get_quotes(全部A股)` 内部会切 66 片并走**单连接流水线**（一条连接连发多个分片、只付一次往返）+ 工作窃取调度，5,227 只实测 **p50 325ms**（老版本串行 3.5s）。自己 for 循环逐只/逐片调会把流水线优势全部丢掉。
  - 想更保守可 `concurrency=11`（每台 1 连接），代价 p50 457ms。
  - 个别服务器不支持流水线时自动降级逐片，结果不受影响。
- **可转债（110/111/113/118/123/127/128）三样都是真的**：价格、`vol`（手=10 张）、`trade_count`（真实笔数，3631 条切片 / 20443 笔量级）全部可用，`Σvol` 与日线 `vol` **1:1 相等**。
  ⚠️ 唯一要小心的是 **日线 `amount / vol` 不等于均价，而是均价的 10 倍**（`vol` 单位是手=10 张）。算可转债均价用 `amount / vol / 10`，或直接用 `Σ(price×vol) / Σvol`——后者跨品种都成立，最稳。
  对照：股票/ETF 的 `Σ逐笔vol / 日线vol = 0.01`（逐笔手、日线股），而 `amount/vol` 恰好就是均价。
- **指数本身没有成交笔数**。`get_ticks("sh000001")` 的 `trade_count` 是「当时均价×1000」、`vol` 单位是百元（成交额/100）。要笔数只能用 `get_index_trade_stats(index)`（成分股逐笔累加，2230 只约 36-60 s）。北交所成分无笔数字段，该方法会返回 `trade_count_available=False`。
- **ETF / LOF 也没有成交笔数**。`get_ticks("sh510310")` 的 `trade_count` 是「价格×1000」（货币 ETF 恒为 0）。判定用 `symbols.has_order_count(market, code)`，读记录时看 `TransactionRecord.trade_count_available`——为 `False` 时 `trade_count` 已被置 0，不要求和/求均值。注意**沪市 `501x/502x/506x` 实测有真实笔数**，不能按「5 开头=基金」一刀切。
- **历史逐笔有笔数**（MAC `0x122F` 支持历史日期，实测可回溯最近 5 个交易日；更早返回空）。
- **小数位表不用手动管，但可以手动刷**。`start()` 会检查当日缓存并后台同步一次（`SecurityInfo.decimal_point` 权威值）；要强制刷新用 `await td.sync_decimal_points()`，看同步状态用 `await td.decimal_point_status()`。`PYPYTDX_DATA_DISABLE_DECIMAL_SYNC=1` 可关闭后台同步。
- 板块文件（`block_*.dat`）/ 行业（`tdxhy.cfg`）/ 财报（`gpcw*.zip`）是**服务器上的静态文件**，解析后是普通 dataclass，**落 JSON 后完全脱网可用**——适合做离线快照。

## DataFrame 输出（v0.7+）

表格型方法可以直出 DataFrame，**唯一开关是构造参数 `df`**，不要在调用处传 `df=True`（没有这个参数）：

```python
async with TdxData(df="polars") as td:      # 或 df="pandas"
    df = await td.get_bars("sh600000", KlinePeriod.DAY, count=240)
```

| 事项 | 说明 |
|---|---|
| 安装 | `pip install "pytdxdata[polars]"` / `[pandas]` / `[dataframe]`；核心包仍零依赖 |
| `df=None`（默认） | 行为与 0.6.x 完全一致，返回 `list[dataclass]` |
| 报错 | `df` 取值非法 → `ValueError`；包没装 → `FrameBackendError`（启动即失败，不拖到第一次查询） |
| 时间列 | `year/month/day/hour/minute` **被 `datetime` 替换**，另带 `trading_date`；`XdxrRecord` 给 `date` |
| 嵌套 | `bid`/`ask` 展开成 `bid1_price`…`ask5_vol` 共 10 列；`fields` 字典 JSON 化成字符串列；`codes` 保持列表 |
| 特殊容器 | `get_indicators` → bars+指标合并；`get_index_trade_stats(_multi)` → `minutes` 展开 + `index`/`date` 列；`get_kline_batch` / `get_minute_batch` → 纵向拼接成一张表 |
| 不受影响 | `bytes` / `int` / `dict` / 单对象 / `tuple` / `list[float]` 类返回原样（`get_file`、`get_security_count`、`get_server_info`、`get_price_limits` 等） |
| 空结果 | 列名齐全的空表；dtype 只由后端推断 |

完整口径见 [DataFrame 输出](https://openbot-coder.github.io/pytdxdata/dataframe/)。

## 陷阱清单

| 坑 | 后果 | 正确做法 |
|---|---|---|
| 统一方法（`get_quotes`/`get_bars`…）传 `(Market.SH, "600000")` 元组或市场号 | v0.6 起直接报错 | 用字符串 `"sh600000"` |
| 旧别名（`get_kline`/`get_transactions`…）传 `(market, code)` | 能跑，但发出 `DeprecationWarning` | 迁移到统一方法 + 字符串 |
| 忘了 `async with` / `start()` | 无连接池，运行时报错 | 一律 `async with TdxData() as td:` |
| `KlinePeriod.DAY == 0` | 拿到的是 5 分钟线 | `DAY=4`，对照枚举表 |
| 等在返回值上找 `.to_dataframe()` | 不存在 | `pd.DataFrame([asdict(x) for x in bars])` |
| 把 `close` 当成分 | 数值差 100 倍 | 价格单位是**元**（float） |
| `sh000001` 当成平安银行 | 拿到的是上证指数 | **代码不唯一**：`sh000001`=上证指数，`sz000001`=平安银行 |
| 用 `get_block_info` 又自己拆二进制 | 白写解析器 | 用 `get_block_parsed`，已经解析好了 |
| 循环里逐个 `await get_bars` | 慢 | 用列表传多个标的，自动并发 |
| 港股/美股五档 bid/ask | 没有（协议不支持） | `fields` 字段位通道有 name 但无五档 |
| 美股分钟线用 `datetime.date()` 分组做日聚合 | 一个交易日被拆成两天（美股跨北京零点） | 用 `bar.trading_date`；`bar.datetime` 是归一化后的北京时间 |
| 拿指数逐笔的 `trade_count` 当笔数 | 拿到的是「均价×1000」，数值荒谬 | 用 `get_index_trade_stats(index)` 由成分累加 |
| 拿 ETF/LOF 逐笔的 `trade_count` 当笔数 | 拿到的是「价格×1000」（`sh510310` 显示 3994 笔） | 看 `trade_count_available`，或 `symbols.has_order_count()` |
| 拿深市 `399xxx` 的 `amount` 校验成分合计 | 比值 0.43，误以为数据错 | 深市指数 amount 是**全板块**口径（`sz399001`=深市全部 5081 亿），不是样本股 |
| 依赖实时性做信号 | 数据是 15 分钟前的 | 换券商实时源 |
| 手动改包内 `servers.json` / 写死 IP | 容器只读、升级被覆盖、快节点捞不回来 | 日常靠内置的**当日测速缓存**自动选路；要强制指定就用 `PYPYTDX_DATA_{STANDARD,MAC,EX}_HOSTS` 环境变量 |

## 深入参考

- 文档站：<https://openbot-coder.github.io/pytdxdata/>
- 完整方法签名（自动生成）：<https://openbot-coder.github.io/pytdxdata/api/tdxdata/>
- 场景示例 / 离线解析配方：<https://openbot-coder.github.io/pytdxdata/cookbook/>
- 给 LLM 的速查摘要：<https://openbot-coder.github.io/pytdxdata/llms.txt>
- 命令行等价物：`tdx quotes sz000001`、`tdx kline sz000001 --period day --count 30`、`tdx block block_gn.dat`（`tdx --help` 看全部）

> 数据来自通达信公开接口，仅供技术研究与教育用途，不构成投资建议。
