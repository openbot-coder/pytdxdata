# pytdxdata

[![CI](https://img.shields.io/github/actions/workflow/status/openbot-coder/pytdxdata/ci.yml?branch=main&label=CI)](https://github.com/openbot-coder/pytdxdata/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/pytdxdata)](https://pypi.org/project/pytdxdata/)
[![Python](https://img.shields.io/pypi/pyversions/pytdxdata)](https://pypi.org/project/pytdxdata/)
[![License](https://img.shields.io/pypi/l/pytdxdata)](https://github.com/openbot-coder/pytdxdata/blob/main/LICENSE)
[![PyPI downloads](https://img.shields.io/pypi/dm/pytdxdata)](https://pypi.org/project/pytdxdata/)

通达信(TongDaXin)行情数据客户端：**动态连接池 + 双层 TTL 缓存 + 多连接并发分页**，纯 asyncio、**零运行时依赖**。

**A股(沪/深/北) + 港股 + 美股 + 期货 + 期权**，一套接口全市场。

📖 **完整文档：<https://openbot-coder.github.io/pytdxdata/>**

## 安装

```bash
pip install pytdxdata          # 或 uv add pytdxdata
```

可选异步加速：

```bash
pip install "pytdxdata[uvloop]"
```

## 30 秒上手

```python
import asyncio
from pytdxdata import TdxData
from pytdxdata.models import KlinePeriod, Adjust

async def main():
    async with TdxData() as td:                     # 自动建连接池 + 缓存
        # 日K线（count 超过单页大小会自动多连接并发分页）
        bars = await td.get_bars(["sz000001"], KlinePeriod.DAY, count=240)
        # 前复权（自动走 MAC 通道）
        qfq = await td.get_bars(["sz300308"], KlinePeriod.DAY,
                                 count=500, adjust=Adjust.QFQ)
        # 跨市场报价：A股+港股+美股一次拿全
        quotes = await td.get_quotes(["sh600000", "hk00700", "usAAPL"])
        # 逐笔成交（MAC 通道，支持历史日期）
        ticks = await td.get_ticks(["sz000001"], date=20260811)
        print(bars[-1].close, quotes[0].price, len(ticks))

asyncio.run(main())
```

默认返回的是标准库 `dataclass` 组成的 `list`（核心库不依赖 pandas）。需要 DataFrame 就换个后端构造：

```python
async with TdxData(df="polars") as td:       # 或 df="pandas"；需 pip install "pytdxdata[polars]"
    df = await td.get_bars("sh600000", KlinePeriod.DAY, count=240)
# 列：market code open high low close vol amount datetime trading_date symbol settle
```

时间字段会被**合并成一列 `datetime`**（不再有 year/month/day/hour/minute），五档报价展开成
`bid1_price`…`ask5_vol`，详见 [DataFrame 输出](https://openbot-coder.github.io/pytdxdata/dataframe/)。

## 特性

| 特性 | 说明 |
|------|------|
| 🧩 动态连接池 | 启动时对服务器**真实 K 线探测**选优（剔除握手正常但数据为空的坏节点），健康分 + EWMA 延迟选路，跨服务器故障转移 |
| 🗄️ 双层 TTL 缓存 | 内存 LRU + 磁盘 SQLite（WAL），TTL 分级；single-flight 并发同键去重；缓存**已解析记录**（重复请求 0 网络 0 解析） |
| ⚡ 并发分页 | 单请求拆 N 页（显式 start 偏移 → 无状态可并行）并行拉取、按序拼接、失败换连接重试 |
| 🔀 双通道整合 | 标准通道（经典行情）+ MAC 通道（逐笔 / 复权 / 1 分钟 K 线），按命令自动选路 |
| 📦 零重型依赖 | 纯 asyncio + stdlib（socket / zlib / struct / sqlite3），`dependencies` 为空 |
| 🐼 DataFrame 直出 | `TdxData(df="polars")` / `df="pandas"` 让 42 个表格型接口直接返回 DataFrame；时间列自动合并成 `datetime`，五档报价自动展开。后端是可选 extras，核心包仍零依赖 |
| 📊 全市场覆盖 | A股、港股、美股、期货、期权（46 个扩展市场） |
| 📄 服务器文件解析 | 板块 `block_*.dat` / 行业 `tdxhy.cfg` / 财报 `gpcw*` **下载即解析**成 dataclass（索引名与服务器实际文件名不一致时自动纠正） |
| 🤖 AI 助手友好 | 内置 [`SKILL.md`](https://github.com/openbot-coder/pytdxdata/blob/main/SKILL.md) 与 [`llms.txt`](https://openbot-coder.github.io/pytdxdata/llms.txt) |

## 文档导航

| 我想… | 去哪 |
|------|------|
| 装好跑起来 | [快速开始](https://openbot-coder.github.io/pytdxdata/getting-started/) |
| 抄场景代码（转 DataFrame / 批量下载 / 离线板块快照） | [场景示例](https://openbot-coder.github.io/pytdxdata/cookbook/) |
| 查方法签名与参数 | [TdxData 方法](https://openbot-coder.github.io/pytdxdata/api/tdxdata/) — 由 docstring 自动生成 |
| 搞清枚举该填几 | [枚举速查](https://openbot-coder.github.io/pytdxdata/api/enums/) |
| 看协议二进制格式、三池路由、缓存策略 | [深入原理](https://openbot-coder.github.io/pytdxdata/internals/architecture/) |
| 用命令行 | [命令行工具](https://openbot-coder.github.io/pytdxdata/cli/) |
| 完整接口清单（28 个统一方法 + 33 个废弃别名） | [`docs/API_REFERENCE.md`](https://openbot-coder.github.io/pytdxdata/API_REFERENCE/) |
| 重构审查（数据一致性 / 遗漏对账） | [接口重构审查](https://openbot-coder.github.io/pytdxdata/refactor-audit/) |
| 版本变更 | [`CHANGELOG.md`](https://github.com/openbot-coder/pytdxdata/blob/main/CHANGELOG.md) |

> 本 README 只做**入口层**。深度内容一律在文档站，避免两处维护造成漂移。

## 给 AI 助手用

仓库根的 [`SKILL.md`](https://github.com/openbot-coder/pytdxdata/blob/main/SKILL.md) 是一份可直接加载的技能：三条必守约定、`KlinePeriod` 等枚举取值表、
「想做什么 → 用哪个方法」决策表、以及 11 条真实陷阱。AI 编程助手读完就能一次写对，不必翻源码。

```bash
# 丢进你所用 AI 助手的 skills 目录即可
curl -o ~/.workbuddy/skills/pytdxdata/SKILL.md --create-dirs \
  https://raw.githubusercontent.com/openbot-coder/pytdxdata/main/SKILL.md
```

## 示例

- [`examples/quickstart.py`](https://github.com/openbot-coder/pytdxdata/blob/main/examples/quickstart.py) — 报价 / K线 / 分时 / 板块
- [`examples/server_files.py`](https://github.com/openbot-coder/pytdxdata/blob/main/examples/server_files.py) — 板块 / 行业 / 财报 下载 + 解析

## 开发

```bash
uv sync                        # 建虚拟环境 + 注册 tdx 命令
uv run pytest tests/           # 191 个测试用例（协议 / 池 / 缓存 / 分页 / 全接口回归 / 文件解析 / CLI / 文档漂移 / 当日测速缓存）
uv sync --group docs           # 文档站依赖（mkdocs-material + mkdocstrings）
uv run mkdocs serve            # 本地预览文档站
```

## 已知限制

- 行情来自通达信公开服务器，**15 分钟延时**；需实时数据请使用券商行情
- 标准通道 K 线 `count < 100` 部分服务器返回空 → 分页器已强制 `min_page=100` 规避
- 港股 / 美股期权在通达信协议内无数据
- **EX 通道（港股 / 美股 / 期货 / 期权）的 K 线没有统一成交额**：`0x23FF` 帧内只有一个可复用槽位，
  权益类市场（港股主板/基金/创业板、新三板、美股）该槽位是成交额，已回填；**期货 / 期权该槽位是持仓量，
  所以它们的 `SecurityBar.amount` 恒为 `0.0`**（协议限制，不是 bug）。期货/期权的结算价在 `SecurityBar.settle`。
  ⚠️ 港股主板/基金的 `vol` 单位是「手」（=100 股），`amount/vol` 是均价的 100 倍
- **K 线时间戳统一为北京时间**（v0.6.2 起）。美股原始报文把「美东交易日日期」与「北京时间时分」
  拼在一起，库已自动归一化，`bar.datetime` 可直接排序 / 过滤；但**美股一个交易日横跨两个北京
  自然日**，按交易日聚合请用 `SecurityBar.trading_date`，不要用 `datetime.date()`
- 服务器可用性随时间变化。**当日首次启动会自动在后台测速并缓存**（对调用方无感，当天不再重测）；
  也可用 `python -m pytdxdata.server_probe` 离线重生成随包清单。
  缓存路径与三个开关见 [安装与配置 → 当日自动测速](https://openbot-coder.github.io/pytdxdata/getting-started/#当日自动测速无感)

## 免责声明

本包仅用于**技术研究与教育目的**。行情数据来自通达信公开接口，不保证实时性与准确性；
作者不对任何投资决策负责。使用前请遵守通达信服务条款及相关法律法规。

## License

[MIT](https://github.com/openbot-coder/pytdxdata/blob/main/LICENSE)
