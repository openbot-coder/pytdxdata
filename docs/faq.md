# 常见问题

## 数据与来源

??? question "数据是实时的吗？"
    不是。行情来自通达信**公开**行情服务器，本身是 **15 分钟延时**数据。

    需要实时数据请使用券商行情（迅投 QMT / 兴业 SMT-Q / 同花顺 等）。
    本项目定位是**历史数据与研究**——日线、分钟线、逐笔、财报这些历史数据是完整且稳定的。

??? question "需要账号吗？要付费吗？"
    不需要。通达信公开行情服务器无需注册，直接 TCP 连接即可。本项目也不收任何费用。

??? question "为什么 SHA 000001 和 SZ 000001 完全不一样？"
    同一个 6 位代码在不同市场里是**不同标的**：

    | 市场 | 代码 | 标的 |
    |---|---|---|
    | SH | `000001` | 上证指数 |
    | SZ | `000001` | 平安银行 |

    代码不唯一，**市场 + 代码**才唯一。这也是为什么所有接口的第一个参数都是 `market`。

??? question "为什么报价格式有时候是 2 位小数，有时候是 3 位？"
    价格小数位按**品种**推断：股票 2 位，指数 / ETF / 可转债 3 位。
    模型里的 `decimal_point` 字段就存了这个信息，需要自己格式化时以它为准。

## 连接与网络

??? question "连不上服务器 / 全部超时怎么办？"
    服务器可用性会随时间变化（下线、换 IP、封禁）。按顺序排查：

    1. 重新探测：`python -m pytdxdata.server_probe`
    2. 手工指定清单绕过探测结果：
       ```python
       async with TdxData(standard_servers=[("1.2.3.4", 7709)]) as td: ...
       ```
    3. 检查出口网络策略——标准 / MAC 端口 **7709**、EX 端口 **7727** 需要放通。
    4. 若在用代理软件，注意有些本地代理会劫持回环 TCP，导致 asyncio 建事件循环都卡住
       （见下方「在 Windows 上测试卡死」）。

??? question "公司网络 / 代理环境下失败"
    通达信协议是裸 TCP，不走 HTTP 代理。如果环境强制走代理，需要把 `7709` / `7727`
    配成直连（bypass）。云服务器上常见问题是安全组只放通了 80/443。

??? question "连接池要配多大？"
    默认 `pool_min=3 / pool_max=12` 覆盖绝大多数场景。经验值：

    | 场景 | 建议 |
    |---|---|
    | 偶发查询、CLI | 默认即可 |
    | 批量落库（`*_batch`） | `pool_max=16`、`concurrency=8~16` |
    | Web 服务多并发 | `pool_max=24`，并确保单实例复用 |

    再大只是排队，不会更快——瓶颈是对端服务器的连接上限。

## 数据正确性

??? question "K 线 `count` 给很小的时候返回空？"
    已知问题：标准通道部分服务器在 `count < 100` 时返回空数据。
    分页器已强制 `min_page=100` 规避，正常调用不会触发。
    若你绕过分页器直接把 `start/count` 传给底层命令，需要自己处理。

??? question "1 分钟 K 线明明能并发，为什么是串行的？"
    **刻意设计**。并发分页会把不同页分配到不同服务器，而各服务器的历史深度不一致；
    拼接深度不同的分页，`stop_when_short` 会导致**非确定性数据丢失**。
    所以 `KlinePeriod.MIN_1` 强制路由到 MAC 通道并**单连接串行分页**。

    其他周期（`MIN_5` 及以上）不受影响，仍走并发分页。

??? question "为什么日线数据和券商软件对不上？"
    按顺序检查：

    1. **复权方式**是否一致 —— 券商常默认前复权，而本包默认 `Adjust.NONE`（不复权）
    2. **是否含未收盘的那根** —— 盘中最后一根是动态的
    3. 分钟线注意**数据来源深度**不同

??? question "缓存会不会返回过期数据？"
    缓存按数据类型分级 TTL，不会返回超出 TTL 的数据：

    | 数据类型 | TTL |
    |---|---|
    | 实时报价 | 5s |
    | 分钟 K 线 | 60s |
    | 日 K 线 | 当天 |
    | 复权 K 线 | 1 天 |
    | 历史逐笔 | 永久 |

    需要绕开缓存时，用新的 `cache_dir`，或直接不传 `cache_dir`（仅内存缓存）。

## 市场覆盖

??? question "支持哪些市场？"
    **标准通道**：沪 / 深 / 北（A 股）、指数、ETF、可转债。
    **EX 通道**：港股、美股、期货、期权等 **52 个市场**，完整清单见 [枚举速查](api/enums.md#exmarket)。

??? question "美股期权 / 港股期权能拿吗？"
    不能。通达信协议内**无美股 / 港股期权数据**。
    美股期权可用 CBOE 公开接口获取。

??? question "数据能存下来离线用吗？"
    能。所有方法返回的都是纯 Python `dataclass`，序列化完全自主。
    服务器文件类的解析方法（板块 / 行业 / 财报）尤其适合做「快照落盘」——
    解析后就是普通 dataclass，落 JSON 后**完全脱网可用**。见 [场景示例](cookbook.md#offline-block-snapshot)。

## 开发与测试

??? question "本机 `uv run pytest` 报 trampoline 错误？"
    Windows 上已知问题。直接调 venv 里的解释器：

    === "Windows"

        ```bash
        .venv/Scripts/python.exe -m pytest tests/ -q
        ```

    === "Linux / macOS"

        ```bash
        .venv/bin/python -m pytest tests/ -q
        ```

??? question "本地测试卡死 / asyncio 事件循环建不起来？"
    **根因**：Windows 上 `asyncio.new_event_loop()` 内部要建一个 `socket.socketpair()`，
    走的是回环 TCP；**本地代理软件 / 安全软件会拦截回环连接**，导致建循环永久挂起。

    表现：`asyncio.new_event_loop()`、`asyncio.run()`、`pytest-asyncio` 全部挂起。

    规避办法（任选）：

    1. 退出或放行本地代理软件，让 `127.0.0.1` 回环连通；
    2. 在 WSL / Linux 环境里跑异步测试；
    3. 对不 `await` 真实 Future 的协程，可以同步驱动验证：
       ```python
       coro = some_coroutine()
       try:
           coro.send(None)
       except StopIteration as e:
           result = e.value
       ```

    > 这也是 CI 放在 Linux 上跑的原因——Windows 侧无法做首轮异步验证。

??? question "和 pytdx / easy-tdx 什么关系？"
    | 项目 | 关系 |
    |---|---|
    | [easy-tdx](https://github.com/handsomejustin/easy_tdx) | **参照实现**。协议层独立实现，不依赖其包，只参照字节级格式；解决了它无连接池、串行分页、单连接单锁的问题 |
    | [pytdx](https://github.com/rainx/pytdx) | 上游生态。本包**不依赖** pytdx，但对 EX 通道（7727）沿用 pytdx 的扩展市场协议 |

## 下一步

- [参与贡献](contributing.md) — 报 bug / 提 PR / 跑测试
- [更新日志](changelog.md) — 每个版本改了什么
