# pytdxdata

[![CI](https://img.shields.io/github/actions/workflow/status/openbot-coder/pytdxdata/ci.yml?branch=main&label=CI)](https://github.com/openbot-coder/pytdxdata/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/pytdxdata)](https://pypi.org/project/pytdxdata/)
[![Python](https://img.shields.io/pypi/pyversions/pytdxdata)](https://pypi.org/project/pytdxdata/)
[![License](https://img.shields.io/pypi/l/pytdxdata)](https://github.com/openbot-coder/pytdxdata/blob/main/LICENSE)
[![PyPI downloads](https://img.shields.io/pypi/dm/pytdxdata)](https://pypi.org/project/pytdxdata/)

通达信(TongDaXin)行情数据客户端：**动态连接池 + 双层TTL缓存 + 3连接并发分页**（纯 asyncio，零运行时依赖）。

**A股(沪/深/北) + 港股 + 美股 + 期货 + 期权**，一套接口全市场。

参照 [easy-tdx](https://github.com/handsomejustin/easy_tdx) 重新实现，解决其三大短板（无连接池、串行分页、单连接单锁），协议层独立实现（不依赖 easy-tdx 包，仅参照其字节级格式）。

## 特性

| 特性 | 说明 |
|------|------|
| 🧩 动态连接池 | 启动时对服务器**真实K线探测**选优（剔除握手正常但数据返回空的坏节点），按等待队列/空闲时长动态扩缩容，健康分（失败×0.5/成功+0.2/冷却120s）+EWMA延迟选路，跨服务器故障转移 |
| 🗄️ 双层TTL缓存 | 内存LRU + 磁盘SQLite（WAL），TTL分级：报价5s / 分钟K线60s / 日线当天 / 复权K线1天 / **历史逐笔永久**；single-flight 并发同键去重（只回源一次）；缓存"已解析记录"（重复请求0网络0解析） |
| ⚡ 并发分页 | 单请求拆N页（每页显式start偏移→无状态可并行），多连接并行拉取、按序拼接、失败换连接重试；1分钟K线走 MAC 单连接串行分页（避免多服务器数据空洞） |
| 🔀 双通道整合 | 标准通道（经典行情）+ MAC通道（逐笔/复权/1分钟K线），按命令自动选路 |
| 📦 零重型依赖 | 纯 asyncio + stdlib（socket/zlib/struct/sqlite3），返回 `list[dataclass]`，pandas 由调用方按需转换 |
| 🚀 批量下载 | `get_kline_batch` / `get_minute_batch` — 多股票自动并发，连接池分配不同服务器 |
| 📄 服务器文件解析 | 板块 `block_*.dat` / 行业 `tdxhy.cfg` / 历史财报 `gpcw*.zip` **下载即解析**成 dataclass（纯 stdlib），无需自己拆二进制 |

## 安装

```bash
pip install pytdxdata          # 或 uv add pytdxdata
```

从源码开发：

```bash
git clone <repo-url>
cd pytdxdata
uv sync                          # 创建虚拟环境 + 安装CLI
uv run python -c "import pytdxdata"
```

可选加速（uvloop）：

```bash
uv add --optional uvloop uvloop
```

> 注：数据来自通达信公开行情服务器（无需账号），服务器清单由 `server_probe.py` 导入时自动探测可用性与延时，存于 `servers.json`。

## CLI 命令行工具

```bash
pip install -e .   # 或 uv sync，注册 tdx 命令

tdx quotes sz000001                    # 行情报价(五档) / hk00700 / usAAPL
tdx kline sz000001 --period day --count 30 --adjust qfq
tdx kline cffex:IFL0 --count 30        # 期货K线(hk/us/zz/dl/shf/cffex/gz)
tdx transactions sz000001 --count 50   # 逐笔(含成交笔数)
tdx minute sz000001                    # 分时
tdx index 000001 --count 30            # 指数K线
tdx board list / members 881001 / ranking / change --days 5
tdx info sz000001                      # 个股快照
tdx list --market sz --count 100       # 证券列表
tdx auction sz000001 / unusual / xdxr / finance / flow
tdx block block_gn.dat --limit 10      # 板块文件下载+解析(成分股)
tdx industry 600000 --search 银行       # 行业分类下载+解析(tdxhy.cfg)
tdx financial-list                      # 历史专业财报文件索引(gpcw.txt)
tdx financial gpcw20260331.zip          # 历史专业财报记录下载+解析
tdx server-info / market-stat
```

## 快速开始

```python
import asyncio
from pytdxdata import TdxData
from pytdxdata.models import KlinePeriod, Market, Adjust

async def main():
    async with TdxData() as td:   # 自动建标准池(52台)+MAC池(3台)+缓存
        # 日K线（count>800 自动3连接并发分页）
        bars = await td.get_kline(1, "600000", KlinePeriod.DAY, count=2400)
        # 分钟K线（复权走MAC通道）
        m1 = await td.get_kline(0, "300308", KlinePeriod.MIN_1, count=240)
        # 五档报价（TTL 5s 缓存，重复请求0网络）
        q = await td.get_quotes([(1, "600000"), (0, "000001")])
        # 逐笔成交（MAC通道，含 trade_count=成交笔数，支持历史回溯）
        t = await td.get_transactions(0, "000001", date=20260811)
        total_trades = sum(r.trade_count for r in t)   # 当天总成交笔数
        # 证券列表/数量
        n = await td.get_security_count(0)
        lst = await td.get_security_list(1, start=0, count=1000)
        # 分时/除权除息/财务
        minute = await td.get_minute(1, "600000")
        xdxr = await td.get_xdxr(1, "600000")
        fin = await td.get_finance(1, "600000")

asyncio.run(main())
```

## API 参考

统一入口 `TdxData`（`async with TdxData() as td:` 自动 start/close）。

> 完整接口清单（每个方法的参数 / 返回值 / 通道）见 [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md)。

### 连接管理

| 方法 | 说明 |
|------|------|
| `start()` / `close()` | 启动（服务器探测+预建连接+缓存）/ 关闭 |
| `__aenter__` / `__aexit__` | 异步上下文管理器 |

### 行情接口

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_kline(market, code, period, *, start, count, adjust)` | `list[SecurityBar]` | K线（count>800自动并发分页；adjust≠NONE走MAC复权；88/000/399指数走指数命令） |
| `get_index_kline(market, code, period, *, start, count)` | `list[SecurityBar]` | 指数K线（多涨跌家数字段已处理） |
| `get_quotes(stocks)` | `list[SecurityQuote]` | 五档报价（>80只自动切批，缓存键=规范化股票集） |
| `get_transactions(market, code, *, date, start, count)` | `list[TransactionRecord]` | **逐笔成交（MAC，含 trade_count 成交笔数）**，支持历史回溯 |
| `get_minute(market, code, *, date)` | `list[MinuteBar]` | 分时（今日/历史，MAC通道） |
| `get_kline_batch(stocks, period, *, start, count, adjust, concurrency)` | `dict[str, list[SecurityBar]]` | **批量K线**（多股票自动并发，连接池分配不同服务器） |
| `get_minute_batch(stocks, *, date, concurrency)` | `dict[str, list[MinuteBar]]` | **批量分时**（多股票自动并发） |
| `get_security_count(market)` | `int` | 市场证券数量 |
| `get_security_list(market, *, start, count)` | `list[SecurityInfo]` | 证券列表（count=None=全部，先查总数分页） |
| `get_security_list_all(market)` | `list[SecurityInfo]` | 全市场列表（缓存1天） |
| `get_xdxr(market, code)` | `list[XdxrRecord]` | 除权除息历史（标准） |
| `get_finance(market, code)` | `FinanceRecord \| None` | 财务快照（标准） |

### 板块/竞价/异动（MAC）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_board_list(board_type, count)` | `list[BoardInfo]` | 板块列表（0=全部 1=行业 2=概念） |
| `get_board_members(board_symbol, count, sort_type)` | `list[MemberQuote]` | 板块成分股报价（自定义字段） |
| `get_board_summary(board_symbol)` | `dict` | 板块汇总（成分数/成交额/主力净流入/涨跌家数） |
| `get_board_ranking(board_type, top_n)` | `list[dict]` | 板块涨跌幅排行 |
| `get_board_change_ranking(board_type, days, top_n)` | `list[dict]` | 板块N日涨跌幅排行（板块指数K线） |
| `get_belong_board(market, code)` | `list[BelongBoard]` | 个股所属板块 |
| `get_auction(market, code)` | `list[AuctionItem]` | 集合竞价 |
| `get_unusual(market, start, count)` | `list[UnusualItem]` | 市场异动 |
| `get_capital_flow(market, code)` | `CapitalFlow` | 资金流向 |

### 快照/服务器/扩展（MAC）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_symbol_info(market, code)` | `SymbolSnapshot` | 个股特征快照 |
| `get_stock_quotes(stocks, bits)` | `list[MemberQuote]` | MAC自定义字段报价 |
| `get_stock_quotes_list(category, count)` | `list[MemberQuote]` | 市场分类报价列表（0沪A/6全A/8科创/14创业） |
| `get_stock_kline_with_indicators(market, code, indicators, ...)` | `dict` | K线+指标（MACD/KDJ/RSI/BOLL/MA/EMA） |
| `get_goods_list(market, start, count)` | `list[GoodsItem]` | 扩展市场商品（期货/期权） |
| `get_server_info()` | `ServerInfo` | 服务器交易时段 |
| `get_kline_offset(offset, count)` | `tuple[int,int]` | K线偏移 |
| `get_chart_sampling(market, code)` | `list[float]` | 分时采样（扩展市场命令，A股不适用） |

### 文件/信息（标准+MAC）

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_company_info_category/content(...)` | `list` / `str` | F10公司信息（标准） |
| `get_block_info(filename)` | `bytes` | 板块文件下载（标准） |
| `get_report_file(filename)` | `bytes` | 报告/财务文件下载（标准） |
| `get_financial_file_list()` | `list[str]` | 财务文件列表（tdxfin/gpcw.txt，原始行） |
| `get_financial_records(filename)` | `bytes` | 财务文件内容（gpcw*.zip，原始字节） |
| **`get_block_parsed(filename)`** | `list[TdxBlock]` | 板块文件**下载+解析**：板块名/类别/成分股代码 |
| **`get_industry_map()`** | `dict[str, IndustryInfo]` | tdxhy.cfg **下载+解析**：通达信行业+申万行业 |
| **`get_financial_file_infos()`** | `list[FinancialFileInfo]` | gpcw.txt **下载+解析**：文件名/MD5/大小 |
| **`get_financial_records_parsed(filename)`** | `list[FinancialRecord]` | gpcw*.zip **解压+解析**：每股原始字段 |
| `get_file_meta(filename)` / `download_file(filename)` | `FileMeta` / `bytes` | 远程文件（MAC） |
| `get_price_limits(market, code, name, pre_close, listed_days)` | `tuple` | 涨跌停价（本地计算） |
| `get_market_stat()` | `MarketStat` | 全市场统计（涨跌家数/总额/市值/涨跌停数） |

### 数据模型（dataclass）

| 模型 | 关键字段 |
|------|---------|
| `SecurityBar` | market/code/open/high/low/close/vol/amount/year/month/day/hour/minute + `datetime`属性 |
| `SecurityQuote` | price/pre_close/OHLC/vol/amount/s_vol/b_vol/`bid[5]`/`ask[5]`/server_time/trading_status |
| `TransactionRecord` | time/price/vol/`trade_count`/bs_flag |
| `SecurityInfo` | code/name(GBK)/volunit/decimal_point/pre_close |
| `XdxrRecord` | category/分红送转/缩股/行权等（除权除息） |
| `MinuteBar` | price/vol |
| `FinanceRecord` | 股本/资产/营收/利润（万元/万股，最新快照） |
| `TdxBlock` | name/category/count/`codes`（板块成分，block_*.dat） |
| `IndustryInfo` | code/tdx_industry/sw_industry（tdxhy.cfg） |
| `FinancialFileInfo` | filename/hash/filesize（gpcw.txt） |
| `FinancialRecord` | code/market/report_date/`fields`（历史专业财报） |

### 枚举

- `Market`：SZ=0 / SH=1 / **BJ=2**（北交所）
- `KlinePeriod`：MIN_5=0 / MIN_15=1 / MIN_30=2 / MIN_60=3 / **DAY=4** / WEEK=5 / MONTH=6 / MIN_1=7 / MIN_3=8 / YEAR=9 / SEASON=10（**与通达信协议字节一致**）
- `Adjust`：NONE / QFQ（前复权）/ HFQ（后复权）

## 通道路由

| 命令 | 默认通道 | 说明 |
|------|---------|------|
| K线（复权） | **MAC** | 支持QFQ/HFQ |
| K线（MIN_1不复权） | **MAC** | 0x122E，串行分页，避免多服务器数据空洞 |
| K线（其他周期不复权）/指数 | 标准 | 价格小数位规则已验证 |
| 报价 | 标准 | 股票2位/指数ETF转债3位 |
| **逐笔成交** | **MAC** | 含trade_count，支持历史日期 |
| **分时** | **MAC** | 0x122D，完整历史分时 |
| 列表/数量/除权/财务 | 标准 | 仅标准通道提供 |

## 架构

```
src/pytdxdata/
├── api.py              # 统一接口 TdxData（对外唯一入口）
├── router.py           # 标准/MAC 通道选路
├── config.py           # 服务器清单加载（servers.json + 环境变量覆盖）
├── protocol/           # 独立协议层（帧/变长整数/差分还原/自定义浮点/命令）
│   └── commands/       # kline/quotes/list/transaction/minute/xdxr/finance/mac
├── pool/               # 动态连接池（connection/health/pool）
├── cache/              # 双层TTL缓存（key/ttl/memory/disk/manager）
├── scheduler/          # 并发分页调度器（Paginator，3连接并行）
├── models/             # dataclass 模型 + 枚举
└── codec/              # 缓存载荷序列化（pickle+zlib）+ 服务器文件解析（板块/行业/财报）
```

## 扩展市场（EX通道：港股/美股/期货/期权）

`get_kline` 的 market 参数接受 `ExMarket` 枚举（港股/美股/期货/期权等52个市场）：

```python
from pytdxdata.models import ExMarket, KlinePeriod

# 港股（HK_MAIN_BOARD=31，代码5位）
bars = await td.get_kline(ExMarket.HK_MAIN_BOARD, '00700', KlinePeriod.DAY, count=700)
# 美股（US_STOCK=74）
bars = await td.get_kline(ExMarket.US_STOCK, 'AAPL', KlinePeriod.DAY, count=700)
# 期货（CFFEX=47 / 郑28 / 大29 / 上30，合约如 IFL0/rb2610）
bars = await td.get_kline(ExMarket.CFFEX_FUTURES, 'IFL0', KlinePeriod.DAY, count=700)
# 股票期权（沪8/深9，合约代码8位）
bars = await td.get_kline(ExMarket.SH_STOCK_OPTION, '10010971', KlinePeriod.DAY, count=700)
```

- 端口 7727，统一走 **pytdx EX 协议**（0x23FF K线 / 0x23FA 报价 / 0x23F4 市场列表）
- 4台EX服务器：MAC EX 2台（116.205.135.205 / 121.37.232.167，0x2454 login）+ pytdx EX 2台（通达信扩展市场 116.205.143.214 / 国泰君安 103.221.142.82，0x2454 setup）
- EX 辅助接口：get_goods_list / get_goods_count / get_goods_quotes / get_goods_tick_chart / get_goods_transaction 等
- 实测：腾讯00700=466.6 / AAPL=332.8 / IFL0=4683.2 / 沪期权10010971=0.192
- 美股/港股期权：通达信协议无数据（美股期权可用 CBOE 公开接口，见 quantdata/scripts/import/cboe_options.py）

## 服务器文件：板块 / 行业 / 财报

通达信服务器上除了行情，还有一批**静态文件**（板块成分、行业分类、历史专业财报）。
pytdxdata 提供「**下载 + 解析**」一步到位：既保留原始 `bytes` 通道，也提供解析成 dataclass 的方法。

```python
async with TdxData() as td:
    # ① 板块文件（block_zs.dat 行业 / block_gn.dat 概念 / block_fg.dat 风格）
    blocks = await td.get_block_parsed("block_gn.dat")
    for b in blocks:
        print(b.name, b.category, b.count, b.codes[:5])

    # ② 行业分类（tdxhy.cfg → 通达信行业 + 申万行业）
    hy = await td.get_industry_map()              # {6位代码: IndustryInfo}
    print(hy["600000"].tdx_industry, hy["600000"].sw_industry)

    # ③ 历史专业财报（gpcw.txt 索引 + gpcw*.zip 解压解析）
    infos = await td.get_financial_file_infos()   # 文件名 / MD5 / 大小
    records = await td.get_financial_records_parsed(infos[-1].filename)
    print(records[0].code, records[0].report_date, records[0].fields[:4])
```

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_block_parsed(filename)` | `list[TdxBlock]` | 板块成分：板块名 / 类别 / 成分股代码 |
| `get_industry_map()` | `dict[str, IndustryInfo]` | 通达信行业 + 申万行业（GBK 解析） |
| `get_financial_file_infos()` | `list[FinancialFileInfo]` | gpcw 文件索引（文件名 / MD5 / 大小） |
| `get_financial_records_parsed(filename)` | `list[FinancialRecord]` | 解压 zip + 解析每股原始字段 |

不解析的原始字节通道仍保留（向后兼容）：`get_block_info()` / `get_report_file()` /
`get_financial_file_list()` / `get_financial_records()`。

> 解析器集中在 `codec/`（`block.py` / `industry.py` / `financial.py`），纯标准库（struct / zipfile），
> 可脱网单测；也可直接 `from pytdxdata.codec import parse_block_dat` 单独复用。

## 与 easy-tdx 对比

| 维度 | easy-tdx | pytdxdata |
|------|----------|----------|
| 并发分页 | ❌ 串行 while 循环 | ✅ 3连接并行 |
| 连接池 | ❌ 单连接单锁 | ✅ 动态池+故障转移 |
| 缓存 | 仅证券列表(1天) | ✅ 双层TTL+single-flight |
| 数据正确性 | 基准 | ✅ K线6字段/报价7字段逐字段一致（实测） |
| 2400根日K耗时 | 0.31s | **0.16s（1.97x）** |
| 缓存命中重取 | — | 0.016s（0网络） |
| 接口覆盖 | 基准 | **36/36 全覆盖** |
| 依赖 | pandas等 | 零运行时依赖 |

> 实测环境：Windows 11 / Python 3.13 / 同一批通达信服务器（2026-08 实测）。

## 已知限制

- 标准通道K线 `count<100` 部分服务器返回空 → 分页器已强制 `min_page=100` 规避
- 标准当日逐笔的笔数字段协议中有但 easy-tdx 模型丢弃（pytdxdata 已显式解析）；历史逐笔无笔数字段
- 报价价格小数位按品种推断（股票2位/指数ETF转债3位），同代码不同市场含义不同（SH 000001=指数 vs SZ 000001=股票）
- 行情数据来自通达信公开服务器，**15分钟延时**；需实时数据请使用券商行情
- 港股/美股/期货/期权数据走通达信 EX 通道（7727），部分市场（美股/港股期权）协议内无数据
- 服务器可用性随时间变化，可用 `python -m pytdxdata.server_probe` 重新探测

## 免责声明

本包仅用于**技术研究与教育目的**。行情数据来自通达信公开接口，不保证实时性与准确性；作者不对任何投资决策负责。使用前请遵守通达信服务条款及相关法律法规。

## 测试

```bash
uv run pytest tests/        # 85个测试：协议/池/缓存/分页/API/文件解析/CLI
```

## 目录

- `docs/API_REFERENCE.md` — 完整对外接口清单（Python API / 数据模型 / 枚举 / CLI）
- `examples/quickstart.py` — 快速上手（报价 / K线 / 分时 / 板块）
- `examples/server_files.py` — 服务器文件下载 + 解析（板块 / 行业 / 财报）

## Changelog

### v0.4.0 (2026-09-15)

- **新增「服务器文件下载 + 解析」能力**（对齐并超越 easy-tdx 的文件解析）：
  - `get_block_parsed(filename)` — 板块 `block_zs/gn/fg.dat` → `TdxBlock` 列表（板块名 / 类别 / 成分股代码）
  - `get_industry_map()` — `tdxhy.cfg` → `{代码: IndustryInfo}`（通达信行业 + 申万行业）
  - `get_financial_file_infos()` — `tdxfin/gpcw.txt` → `FinancialFileInfo` 列表（文件名 / MD5 / 大小）
  - `get_financial_records_parsed(filename)` — `gpcw*.zip` 解压 + 解析 → `FinancialRecord` 列表（每股原始字段）
- **新增解析器** `codec/block.py`、`codec/industry.py`、`codec/financial.py`（纯标准库，脱网可单测）
- **新增模型** `TdxBlock` / `IndustryInfo` / `FinancialFileInfo` / `FinancialRecord`
- 修复财报索引市场字段解析（`<6s1c1L` → `<6sBL`，市场 0=深 / 1=沪 正确，此前恒判深市）
- 公开异步方法 47 → **51**，新增接口全部向后兼容（原 `bytes` 方法保留）
- **CLI 新增 4 个命令**：`tdx block` / `tdx industry` / `tdx financial-list` / `tdx financial`
- 新增 `tests/test_cli_files.py`（11 项 CLI 离线测试，FakeTD 桩）
- README 与 `docs/API_REFERENCE.md` 同步更新

### v0.3.2 (2026-08-17)

- **新增** `get_kline_batch(stocks, period, ...)` — 批量K线接口，多股票自动并发下载，连接池分配不同服务器
- **新增** `get_minute_batch(stocks, ...)` — 批量分时接口，多股票自动并发下载

### v0.3.1 (2026-08-17)

- **`get_kline(MIN_1)` 切换为 MAC 通道** (0x122E)，解决标准通道 1 分钟K线深度不足问题
- **修复 MAC K线并发分页数据空洞** — 并发分页器将不同页面分配到不同服务器，当服务器数据深度不一致时 `stop_when_short` 会导致非确定性数据丢失；改用**单连接串行分页**（`_fetch_mac_kline_serial`，start 动态偏移，与 easy-tdx 一致）
- 路由更新：`router.py` 中 `KlinePeriod.MIN_1` 强制路由到 MAC 通道
- 标准通道其他周期（MIN_5/15/30/60/DAY/WEEK/MONTH）不受影响

### v0.3.0 (2026-08-17)

- **`get_minute` 切换为 MAC 通道** (0x122D `MacTickChartCmd`)，历史分时数据稳定返回完整 240 条（09:30~14:59）
- 新增 `protocol/commands/mac_tick.py` — MAC 单日分时图命令
- 移除不再使用的 `GetHistoryMinuteTimeDataCmd` 导入

### v0.2.2 及之前

- 初始版本：动态连接池 + 双层TTL缓存 + 3连接并发分页 + 双通道（标准/MAC）+ 扩展市场（港股/美股/期货/期权）
