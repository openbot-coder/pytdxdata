# 安装与配置

## 环境要求

| 项 | 要求 |
|---|---|
| Python | **≥ 3.11**（开发与 CI 覆盖 3.11 / 3.13） |
| 运行时依赖 | **无**（纯 stdlib：`socket` / `ssl` / `zlib` / `struct` / `sqlite3` / `asyncio`） |
| 操作系统 | 任意（Windows / Linux / macOS） |
| 网络 | 需能直连通达信行情服务器（标准/MAC 端口 **7709**，EX 端口 **7727**） |

## 安装

=== "pip"

    ```bash
    pip install pytdxdata
    ```

=== "uv"

    ```bash
    uv add pytdxdata
    ```

=== "源码开发"

    ```bash
    git clone git@github.com:openbot-coder/pytdxdata.git
    cd pytdxdata
    uv sync                      # 建虚拟环境 + 装 CLI + 装测试依赖
    .venv/Scripts/python.exe -c "import pytdxdata"   # Windows
    # .venv/bin/python -c "import pytdxdata"         # Linux / macOS
    ```

安装后即可使用 `tdx` 命令行工具（见 [CLI 手册](cli.md)）。

### 可选：uvloop 加速

Linux / macOS 上可选用 `uvloop` 替换默认事件循环（Windows 不支持）：

```bash
uv add --optional uvloop uvloop
```

```python
import uvloop, asyncio
uvloop.install()          # 必须在 asyncio.run() 之前
asyncio.run(main())
```

## 构造参数

`TdxData` 全部参数为 **keyword-only**，都有合理默认值——99% 的场景直接 `TdxData()` 即可。

| 参数 | 默认 | 说明 |
|------|------|------|
| `standard_servers` | `None` | 标准通道服务器清单；`None` = 用内置探测结果 |
| `mac_servers` | `None` | MAC 通道服务器清单；`None` = 用内置探测结果 |
| `ex_servers` | `None` | EX 扩展市场服务器清单；`None` = 用内置探测结果 |
| `port` | `7709` | 标准 / MAC 端口（EX 固定 `7727`） |
| `timeout` | `3.0` | 连接超时（秒） |
| `cache_dir` | `None` | 磁盘缓存目录；**`None` = 仅内存缓存**（进程退出即失效） |
| `default_adjust` | `Adjust.NONE` | 默认复权方式，可被单次调用覆盖 |
| `max_inflight` | `64` | single-flight 并发上限（同键请求合并） |
| `pool_min` / `pool_max` | `3` / `12` | 动态连接池上下限 |
| `mac_pool_cap` | `4` | 单台 MAC 服务器连接数上限 |

### 典型配置

=== "最简（仅内存缓存）"

    ```python
    async with TdxData() as td:
        ...
    ```

=== "生产（持久化缓存）"

    ```python
    from pathlib import Path

    async with TdxData(
        cache_dir=Path.home() / ".cache" / "pytdxdata",
        pool_min=4,
        pool_max=16,
        timeout=5.0,
    ) as td:
        ...
    ```

    `cache_dir` 下会生成一个 SQLite 文件（WAL 模式）。**同一目录可被多个进程共享**，
    适合「定时任务 + 交互式查询」并存的场景。

=== "自建服务器清单"

    ```python
    async with TdxData(
        standard_servers=[("1.2.3.4", 7709), ("5.6.7.8", 7709)],
        mac_servers=[("9.10.11.12", 7709)],
    ) as td:
        ...
    ```

## 服务器清单

内置清单随包发布（`src/pytdxdata/servers.json`），由探测脚本生成：

```bash
python -m pytdxdata.server_probe          # 重新探测并覆盖 servers.json
```

探测逻辑不是简单握手——它会**真实请求一段 K 线**，把「能连上但返回空数据」的坏节点剔除。
服务器可用性会随时间变化，连不上时优先重跑探测。

`pytdxdata.config` 提供读取接口：

```python
from pytdxdata.config import get_standard_hosts, get_mac_hosts, get_ex_hosts

print(len(get_standard_hosts()), len(get_mac_hosts()), len(get_ex_hosts()))
```

## 生命周期

`TdxData` 必须在 `async with` 内使用，或手动 `await td.start()` / `await td.close()`：

```python
async with TdxData() as td:      # __aenter__ → start()
    ...
# __aexit__ → close()：关闭全部连接池、flush 缓存
```

!!! tip "长驻进程"
    池与缓存都挂在 `TdxData` 实例上。Web 服务 / 常驻任务应该**只创建一个实例并复用**，
    不要每次请求都 `TdxData()`——否则等于每请求重建连接池，缓存也失效。
    推荐在 FastAPI 的 `lifespan` 里创建并放进依赖。

## 下一步

- [场景示例](cookbook.md) — 看真实用法
- [CLI 手册](cli.md) — 不写代码直接拿数据
- [接口总览](api/index.md) — 完整方法清单
