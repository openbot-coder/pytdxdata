# 场景示例

每个片段都是完整可跑的。共同前提：

```python
import asyncio
from pathlib import Path
from pytdxdata import TdxData
from pytdxdata.models import Adjust, ExMarket, KlinePeriod, Market
```

## 全市场日线批量落库

连接池会把不同股票分配到不同服务器，这是 `*_batch` 存在的意义。

```python
async def dump_all_daily():
    async with TdxData(cache_dir=Path("./tdx_cache")) as td:
        codes = await td.get_security_list_all(Market.SZ)      # 缓存 1 天
        symbols = [(Market.SZ, s.code) for s in codes]

        bars = await td.get_kline_batch(
            symbols,
            KlinePeriod.DAY,
            count=800,
            adjust=Adjust.QFQ,
            concurrency=12,
        )
        print(f"{len(bars)} / {len(symbols)} 只拿到数据")

        for code, rows in bars.items():
            if not rows:
                continue
            # 落库 / 写 parquet 都行，这里只演示
            print(code, len(rows), rows[-1].datetime, rows[-1].close)
```

!!! tip "分页与并发的取舍"
    `concurrency` 不是越大越好。单台服务器有连接上限，`concurrency` 超过 `pool_max × 服务器数`
    之后只会排队。实践值：**8~16**。

## 分钟线增量更新

只拉「上次之后」的部分，用 `get_kline_since` 省掉全量重拉。

```python
async def update_minute(code: str, last_dt):
    async with TdxData(cache_dir=Path("./tdx_cache")) as td:
        rows = await td.get_kline_since(Market.SZ, code, KlinePeriod.MIN_1, since=last_dt)
        return [r for r in rows if r.datetime > last_dt]
```

!!! warning "1 分钟 K 线走 MAC 单连接串行分页"
    这是刻意设计：并发分页会把不同页分到不同服务器，而各服务器**历史深度不一致**，
    拼接后会出现**非确定性数据空洞**。所以 `MIN_1` 强制单连接串行（见 [通道与数据源](internals/routing.md)）。

## 报价轮询

```python
async def poll_quotes(symbols, interval=5.0):
    async with TdxData() as td:
        while True:
            quotes = await td.get_quotes(symbols)      # >80 只自动切批；缓存 TTL 5s
            for q in quotes:
                print(q.code, q.price, q.bid[0], q.ask[0])
            await asyncio.sleep(interval)
```

> 缓存 TTL 与轮询间隔对齐时，重复轮询几乎 0 网络开销。但注意通达信公开源本身是
> **15 分钟延时**数据，别拿它做实时交易决策。

## 板块轮动 / 领涨股

```python
async def sector_rotation():
    async with TdxData() as td:
        # 近 5 日概念板块涨幅排行
        top = await td.get_board_change_ranking(board_type=2, days=5, top_n=10)
        for row in top:
            print(row)

        # 拿第一名板块的成分股
        symbol = top[0]["symbol"] if "symbol" in top[0] else "881001"
        members = await td.get_board_members(symbol, count=20)
        for m in members[:5]:
            print(m.code, m.name, m.fields)

        # 板块资金汇总
        print(await td.get_board_summary(symbol))
```

## 个股所属板块（反向查询）

```python
async def belong(code: str):
    async with TdxData() as td:
        for b in await td.get_belong_board(Market.SH, code):
            print(b)
```

## 逐笔复盘

```python
async def review_transactions(code: str, ymd: int):
    async with TdxData() as td:
        rows = await td.get_transactions(Market.SZ, code, date=ymd, count=2000)
        total = sum(r.trade_count or 0 for r in rows)
        buy = sum(r.vol for r in rows if r.bs_flag == 0)     # 主动买
        sell = sum(r.vol for r in rows if r.bs_flag == 1)    # 主动卖
        print(f"{ymd} 总成交笔数={total} 主动买={buy} 主动卖={sell}")
```

## 财务因子快照

```python
async def finance_snapshot(codes):
    async with TdxData() as td:
        for mkt, code in codes:
            fin = await td.get_finance(mkt, code)      # 标准通道，最新快照
            if fin:
                print(code, fin)
```

历史专业财报（`gpcw*.zip`，含多报告期）走文件接口：

```python
async def history_financials(code: str):
    async with TdxData(cache_dir=Path("./tdx_cache")) as td:
        infos = await td.get_financial_file_infos()             # 索引
        records = await td.get_financial_records_parsed(infos[-1].filename)
        row = next((r for r in records if r.code == code), None)
        if row:
            print(row.report_date, row.fields[:8])
```

## 行业分类映射

```python
async def industry_lookup(codes):
    async with TdxData(cache_dir=Path("./tdx_cache")) as td:
        hy = await td.get_industry_map()          # {6位代码: IndustryInfo}
        for c in codes:
            info = hy.get(c)
            if info:
                print(c, info.tdx_industry, info.sw_industry)
```

## 板块成分（离线解析） {#offline-block-snapshot}

`get_block_parsed` 拿到的是纯 dataclass，落成本地 JSON 后**完全脱网可用**——
适合做「板块成分快照」的定时任务。

```python
import json

async def snapshot_blocks():
    async with TdxData(cache_dir=Path("./tdx_cache")) as td:
        all_blocks = []
        for fn in ("block_zs.dat", "block_gn.dat", "block_fg.dat"):
            for b in await td.get_block_parsed(fn):
                all_blocks.append({
                    "name": b.name,
                    "category": b.category,
                    "codes": b.codes,
                })
        Path("blocks.json").write_text(
            json.dumps(all_blocks, ensure_ascii=False), encoding="utf-8"
        )
```

## 港股 / 美股 / 期货 / 期权

```python
async def ex_markets():
    async with TdxData() as td:
        hk = await td.get_kline(ExMarket.HK_MAIN_BOARD, "00700", KlinePeriod.DAY, count=700)
        us = await td.get_kline(ExMarket.US_STOCK, "AAPL", KlinePeriod.DAY, count=700)
        fut = await td.get_kline(ExMarket.CFFEX_FUTURES, "IFL0", KlinePeriod.DAY, count=700)
        opt = await td.get_kline(ExMarket.SH_STOCK_OPTION, "10010971", KlinePeriod.DAY, count=700)
        print(len(hk), len(us), len(fut), len(opt))

        # 港股全量逐笔（自动翻页，≤50 页 / 9 万条）
        ticks = await td.get_goods_transaction_all(ExMarket.HK_MAIN_BOARD, "00700")
        print(len(ticks))
```

## 转成 pandas {#to-pandas}

包本身不带 pandas 依赖，但转换是一行的事：

```python
import pandas as pd

async def to_dataframe(code: str):
    async with TdxData() as td:
        bars = await td.get_kline(Market.SH, code, KlinePeriod.DAY, count=800)
    df = pd.DataFrame([{
        "dt": b.datetime, "open": b.open, "high": b.high,
        "low": b.low, "close": b.close, "vol": b.vol, "amount": b.amount,
    } for b in bars])
    return df.set_index("dt")
```

## 长驻服务（FastAPI）

!!! danger "不要每次请求都新建 TdxData"
    池与缓存都挂在实例上，每请求新建 = 每请求重建连接池 + 缓存全部失效。

```python
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request

@asynccontextmanager
async def lifespan(app: FastAPI):
    async with TdxData(cache_dir=Path("./tdx_cache"), pool_min=4, pool_max=16) as td:
        app.state.td = td
        yield

app = FastAPI(lifespan=lifespan)

@app.get("/kline/{code}")
async def kline(code: str, request: Request):
    td = request.app.state.td
    bars = await td.get_kline(Market.SH, code, KlinePeriod.DAY, count=30)
    return [{"dt": str(b.datetime), "close": b.close} for b in bars]
```

## 下一步

- [接口总览](api/index.md) — 全部方法
- [连接池与缓存](internals/cache-pool.md) — 调优 `pool_min` / `pool_max` / TTL
