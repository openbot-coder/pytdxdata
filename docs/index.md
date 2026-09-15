# pytdxdata

[![CI](https://img.shields.io/github/actions/workflow/status/openbot-coder/pytdxdata/ci.yml?branch=main&label=CI)](https://github.com/openbot-coder/pytdxdata/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/pytdxdata)](https://pypi.org/project/pytdxdata/)
[![Python](https://img.shields.io/pypi/pyversions/pytdxdata)](https://pypi.org/project/pytdxdata/)
[![License](https://img.shields.io/pypi/l/pytdxdata)](https://github.com/openbot-coder/pytdxdata/blob/main/LICENSE)

通达信(TongDaXin)行情数据客户端：**动态连接池 + 双层 TTL 缓存 + 3 连接并发分页**。

纯 `asyncio` 实现，**零运行时依赖**（只用 stdlib）；**A股(沪/深/北) + 港股 + 美股 + 期货 + 期权**，一套接口全市场。

```bash
pip install pytdxdata
```

```python
import asyncio
from pytdxdata import TdxData
from pytdxdata.models import KlinePeriod

async def main():
    async with TdxData() as td:                      # 自动探测服务器 + 建池 + 建缓存
        bars = await td.get_kline(1, "600000", KlinePeriod.DAY, count=2400)
        print(bars[-1].datetime, bars[-1].close)

asyncio.run(main())
```

## 特性

| 特性 | 说明 |
|------|------|
| 🧩 **动态连接池** | 启动时对服务器真实 K 线探测选优（剔除握手正常但返回空数据的坏节点）；健康分（失败 ×0.5 / 成功 +0.2 / 冷却 120s）+ EWMA 延迟选路；按等待队列与空闲时长动态扩缩容；跨服务器故障转移 |
| 🗄️ **双层 TTL 缓存** | 内存 LRU + 磁盘 SQLite（WAL）；TTL 分级：报价 5s / 分钟K线 60s / 日线当天 / 复权K线 1天 / 历史逐笔永久；`single-flight` 同键并发去重；缓存「已解析记录」，重复请求 0 网络 0 解析 |
| ⚡ **并发分页** | 把大请求拆成显式 `start` 偏移的页，多连接并行拉取、按序拼接、失败换连接重试 |
| 🔀 **双通道整合** | 标准通道（经典行情）+ MAC 通道（逐笔 / 复权 / 1分钟K线），按命令自动选路 |
| 📄 **服务器文件解析** | 板块 `block_*.dat` / 行业 `tdxhy.cfg` / 历史财报 `gpcw*.zip` **下载即解析**成 dataclass（纯 stdlib），无需自己拆二进制 |
| 🚀 **批量下载** | `get_kline_batch` / `get_minute_batch`，多股票自动并发，连接池分配到不同服务器 |
| 📦 **零重型依赖** | 返回 `list[dataclass]`，pandas / polars 由调用方按需转换 |

## 从这里开始

<div class="grid cards" markdown>

- **装好并跑通**

    ---

    安装、构造参数、缓存目录、第一个脚本。

    [:octicons-arrow-right-24: 安装与配置](getting-started.md)

- **命令行直接拿数据**

    ---

    19 个子命令，不写代码也能拉行情、板块、财报。

    [:octicons-arrow-right-24: CLI 手册](cli.md)

- **场景示例**

    ---

    全市场批量落库、分钟线增量、报价轮询、板块轮动、逐笔复盘。

    [:octicons-arrow-right-24: Cookbook](cookbook.md)

- **查接口签名**

    ---

    51 个公开方法，从 docstring 实时生成，不会过期。

    [:octicons-arrow-right-24: API 参考](api/index.md)

</div>

## 设计取舍

参照 [easy-tdx](https://github.com/handsomejustin/easy_tdx) 重新实现，解决其三大短板（无连接池、串行分页、单连接单锁）。
协议层**独立实现**——不依赖 easy-tdx 包，仅参照其字节级格式。

| 维度 | easy-tdx | pytdxdata |
|------|----------|-----------|
| 并发分页 | ❌ 串行 while 循环 | ✅ 3 连接并行 |
| 连接池 | ❌ 单连接单锁 | ✅ 动态池 + 故障转移 |
| 缓存 | 仅证券列表（1天） | ✅ 双层 TTL + single-flight |
| 数据正确性 | 基准 | ✅ K线 6 字段 / 报价 7 字段逐字段一致（实测） |
| 2400 根日K耗时 | 0.31s | **0.16s（1.97x）** |
| 缓存命中重取 | — | 0.016s（0 网络） |
| 依赖 | pandas 等 | 零运行时依赖 |

> 实测环境：Windows 11 / Python 3.13 / 同一批通达信服务器（2026-08）。

## 数据来源与限制

- 行情来自通达信**公开**行情服务器（无需账号），**15 分钟延时**；需实时数据请使用券商行情
- 服务器清单由 `python -m pytdxdata.server_probe` 探测生成，可用性随时间变化
- 港股 / 美股 / 期货 / 期权走通达信 EX 通道（端口 7727），部分市场（美股期权、港股期权）协议内无数据

完整清单见 [常见问题](faq.md)。

!!! warning "免责声明"
    本包仅用于**技术研究与教育目的**。行情数据来自通达信公开接口，不保证实时性与准确性；
    作者不对任何投资决策负责。使用前请遵守通达信服务条款及相关法律法规。

## 目录

<div class="grid cards" markdown>

- **快速开始**
    - [安装与配置](getting-started.md)
    - [场景示例](cookbook.md)
    - [命令行工具](cli.md)

- **参考**
    - [接口总览](api/index.md)
    - [枚举速查](api/enums.md)
    - [数据模型](api/models.md)
    - [完整接口清单](API_REFERENCE.md)

- **深入原理**
    - [架构总览](internals/architecture.md)
    - [协议与命令](internals/protocol.md)
    - [通道与数据源](internals/routing.md)
    - [连接池与缓存](internals/cache-pool.md)

- **其他**
    - [常见问题](faq.md)
    - [更新日志](changelog.md)
    - [参与贡献](contributing.md)

</div>
