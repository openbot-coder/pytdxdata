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

返回的是标准库 `dataclass` 组成的 `list`（库本身不依赖 pandas）。要 DataFrame 一行转：

```python
from dataclasses import asdict
import pandas as pd
df = pd.DataFrame([asdict(b) for b in bars])
```

## 特性

| 特性 | 说明 |
|------|------|
| 🧩 动态连接池 | 启动时对服务器**真实 K 线探测**选优（剔除握手正常但数据为空的坏节点），健康分 + EWMA 延迟选路，跨服务器故障转移 |
| 🗄️ 双层 TTL 缓存 | 内存 LRU + 磁盘 SQLite（WAL），TTL 分级；single-flight 并发同键去重；缓存**已解析记录**（重复请求 0 网络 0 解析） |
| ⚡ 并发分页 | 单请求拆 N 页（显式 start 偏移 → 无状态可并行）并行拉取、按序拼接、失败换连接重试 |
| 🔀 双通道整合 | 标准通道（经典行情）+ MAC 通道（逐笔 / 复权 / 1 分钟 K 线），按命令自动选路 |
| 📦 零重型依赖 | 纯 asyncio + stdlib（socket / zlib / struct / sqlite3） |
| 📊 全市场覆盖 | A股、港股、美股、期货、期权（46 个扩展市场） |
| 📄 服务器文件解析 | 板块 `block_*.dat` / 行业 `tdxhy.cfg` / 财报 `gpcw*.zip` **下载即解析**成 dataclass |
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
uv run pytest tests/           # 127 个测试用例（协议 / 池 / 缓存 / 分页 / 全接口回归 / 文件解析 / CLI / 文档漂移）
uv sync --group docs           # 文档站依赖（mkdocs-material + mkdocstrings）
uv run mkdocs serve            # 本地预览文档站
```

## 已知限制

- 行情来自通达信公开服务器，**15 分钟延时**；需实时数据请使用券商行情
- 标准通道 K 线 `count < 100` 部分服务器返回空 → 分页器已强制 `min_page=100` 规避
- 港股 / 美股期权在通达信协议内无数据
- 服务器可用性随时间变化，可用 `python -m pytdxdata.server_probe` 重新探测

## 免责声明

本包仅用于**技术研究与教育目的**。行情数据来自通达信公开接口，不保证实时性与准确性；
作者不对任何投资决策负责。使用前请遵守通达信服务条款及相关法律法规。

## License

[MIT](https://github.com/openbot-coder/pytdxdata/blob/main/LICENSE)
