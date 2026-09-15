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
    bars = await td.get_kline(...)
# 退出即 close
```

不要直接 `await TdxData().get_kline(...)`——没 `start()` 就没有连接池。

**2. `market` 传的是整数枚举值，不是字符串**

这是最高频的错误来源。**不要传 `"sh"` / `"SH"` / `"1"`**，要用枚举（它就是 `IntEnum`，所以传 `1` 也能跑，但可读性差）：

```python
from pytdxdata.models import Market, KlinePeriod, Adjust

await td.get_kline(Market.SH, "600000", KlinePeriod.DAY, count=240)
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
```

## 可运行模板

```python
import asyncio
from pytdxdata import TdxData
from pytdxdata.models import Market, KlinePeriod, Adjust

async def main():
    async with TdxData() as td:
        # 日K线，240 根（count 超过单页大小会自动多连接并发分页）
        bars = await td.get_kline(Market.SH, "600000", KlinePeriod.DAY, count=240)
        print(bars[-1].close, bars[-1].datetime)

        # 前复权日线（复权自动走 MAC 通道）
        qfq = await td.get_kline(Market.SZ, "300308", KlinePeriod.DAY,
                                 count=500, adjust=Adjust.QFQ)

        # 五档报价：入参是 (market, code) 元组列表，一次可传多只（>80 只自动切批）
        quotes = await td.get_quotes([(Market.SH, "600000"), (Market.SZ, "000001")])
        print(quotes[0].price, quotes[0].bid[0], quotes[0].ask[0])

        # 逐笔成交（MAC 通道，支持历史日期，含 trade_count 成交笔数）
        ticks = await td.get_transactions(Market.SZ, "000001", date=20260811)
        print(sum(r.trade_count for r in ticks))     # 当日总成交笔数

        # 分时（MAC 通道，今日或历史）
        minute = await td.get_minute(Market.SH, "600000")

        # 批量 K 线：多股票自动并发，池子会分配到不同服务器
        batch = await td.get_kline_batch(
            [(Market.SH, "600000"), (Market.SZ, "000001")],
            KlinePeriod.DAY, count=240,
        )                                            # -> {"600000": [...], "000001": [...]}

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

常用几个：`HK_MAIN_BOARD=31`、`US_STOCK=74`、`CFFEX_FUTURES=47`、`SH_STOCK_OPTION=8`、`SZ_STOCK_OPTION=9`。
完整表见 <https://openbot-coder.github.io/pytdxdata/api/enums/>。

```python
from pytdxdata.models import ExMarket

await td.get_kline(ExMarket.HK_MAIN_BOARD, "00700", KlinePeriod.DAY, count=700)   # 港股，5 位代码
await td.get_kline(ExMarket.US_STOCK, "AAPL", KlinePeriod.DAY, count=700)         # 美股，ticker
await td.get_kline(ExMarket.CFFEX_FUTURES, "IFL0", KlinePeriod.DAY, count=700)    # 期货合约
```

## 想做什么 → 用哪个方法

| 需求 | 方法 |
|---|---|
| 日/周/月/分钟 K 线 | `get_kline(market, code, period, *, count, adjust)` |
| 增量更新（从某天往后） | `get_kline_since(market, code, period, since, adjust)` |
| 一次拉多只股票 K 线 | `get_kline_batch(stocks, period, *, count, adjust, concurrency)` |
| 指数 K 线 | `get_index_kline(market, code, period, *, count)` |
| K 线 + MACD/KDJ/RSI/BOLL | `get_stock_kline_with_indicators(market, code, indicators, ...)` |
| 实时五档报价 | `get_quotes(stocks)` |
| 逐笔成交 | `get_transactions(market, code, *, date)` |
| 分时图 | `get_minute(market, code, *, date)` |
| 全市场股票列表 | `get_security_list_all(market)`（缓存 1 天） |
| 除权除息 / 财务快照 | `get_xdxr(...)` / `get_finance(...)` |
| 板块列表 / 成分股 / 涨跌排行 | `get_board_list` / `get_board_members` / `get_board_ranking` |
| 个股所属板块 | `get_belong_board(market, code)` |
| 集合竞价 / 异动 / 资金流 | `get_auction` / `get_unusual` / `get_capital_flow` |
| **板块成分文件**（离线快照） | `get_block_parsed("block_gn.dat")` |
| **行业分类**（通达信 + 申万） | `get_industry_map()` |
| **历史专业财报**（gpcw 全字段） | `get_financial_file_infos()` + `get_financial_records_parsed(fn)` |
| 涨跌停价 | `get_price_limits(market, code, name, pre_close, listed_days)`（本地算，无网络） |
| 全市场涨跌家数/成交额 | `get_market_stat()` |

## 通道与数据特性（影响你能拿到什么）

库内部有**标准通道**和 **MAC 通道**两条链路，会**按命令自动选路**，你不用管。但有几件事必须知道：

- **数据有 15 分钟延时**（通达信公开服务器特性）。要实时数据请用券商行情。
- **复权 K 线与 1 分钟 K 线走 MAC 通道**。`get_kline(..., adjust=Adjust.QFQ)` 自动切换。
- **`count` 很大时会自动多连接并发分页**，无需手动翻页。但标准通道 K 线 `count < 100` 部分服务器会返回空——内部已强制 `min_page=100` 规避。
- **`get_quotes` 一次 >80 只自动切批**；而 MAC 的 `get_stock_quotes` 上限就是 80 只/次。
- **历史逐笔没有笔数字段**，只有当日逐笔的 `trade_count` 有效。
- 板块文件（`block_*.dat`）/ 行业（`tdxhy.cfg`）/ 财报（`gpcw*.zip`）是**服务器上的静态文件**，解析后是普通 dataclass，**落 JSON 后完全脱网可用**——适合做离线快照。

## 陷阱清单

| 坑 | 后果 | 正确做法 |
|---|---|---|
| `market` 传 `"sh"` 字符串 | 直接报错或路由到错误市场 | 用 `Market.SH` 枚举 |
| 以为 `KlinePeriod.DAY == 0` | 拿到的是 5 分钟线 | `DAY=4`，对照上面的表 |
| 忘了 `async with` / `start()` | 无连接池，运行时报错 | 一律 `async with TdxData() as td:` |
| 等在返回值上找 `.to_dataframe()` | 不存在 | `pd.DataFrame([asdict(x) for x in bars])` |
| 把 `close` 当成分 | 数值差 100 倍 | 价格单位是**元**（float） |
| `SH 000001` 当成平安银行 | 拿到的是上证指数 | **代码不唯一**：`(Market.SH, "000001")`=上证指数，`(Market.SZ, "000001")`=平安银行 |
| 用 `get_block_info` 又自己拆二进制 | 白写解析器 | 用 `get_block_parsed`，已经解析好了 |
| 循环里逐个 `await get_kline` | 慢 | 用 `get_kline_batch` 让它并发 |
| 依赖实时性做信号 | 数据是 15 分钟前的 | 换券商实时源 |

## 深入参考

- 文档站：<https://openbot-coder.github.io/pytdxdata/>
- 完整方法签名（自动生成）：<https://openbot-coder.github.io/pytdxdata/api/tdxdata/>
- 场景示例 / 离线解析配方：<https://openbot-coder.github.io/pytdxdata/cookbook/>
- 给 LLM 的速查摘要：<https://openbot-coder.github.io/pytdxdata/llms.txt>
- 命令行等价物：`tdx quotes sz000001`、`tdx kline sz000001 --period day --count 30`、`tdx block block_gn.dat`（`tdx --help` 看全部）

> 数据来自通达信公开接口，仅供技术研究与教育用途，不构成投资建议。
