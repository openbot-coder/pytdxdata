"""权威价格小数位表（decimal_sync）单元测试。

覆盖点：
- 权威表（来自 ``SecurityInfo.decimal_point``）**优先于**静态前缀规则
- 某前缀出现混合小数位 → 自动退化为逐代码查表，不误伤同前缀其它代码
- 清空权威表 → 回到静态规则（与 0.6.5 行为一致）
- 磁盘缓存：写入 / 读取 / 跨日失效 / 损坏容错，均不回退到网络
- ``PYPYTDX_DATA_DISABLE_DECIMAL_SYNC=1`` → 完全不动作
- 证券清单里 ``decimal_point == 0`` 的脏条目被忽略
- 后台 task 可取消，且 ``close()`` 后不再改写表
（全部注入假 ``TdxData``，不连真实网络）
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from pytdxdata import decimal_sync
from pytdxdata.protocol.commands.quotes import (
    clear_decimal_map,
    decimal_map_stats,
    install_decimal_map,
    price_decimal_digits,
)


# ---------------------------------------------------------------------- #
# 夹具
# ---------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每个用例独立缓存目录 + 干净的内存状态。"""
    monkeypatch.setenv("PYPYTDX_DATA_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("PYPYTDX_DATA_DISABLE_DECIMAL_SYNC", raising=False)
    decimal_sync.reset()
    yield
    decimal_sync.reset()


def _info(code: str, dp: int) -> SimpleNamespace:
    return SimpleNamespace(code=code, decimal_point=dp, market=1)


class FakeTd:
    """只有 ``get_universe`` 的假客户端（记录调用次数，便于断言"没走网络"）。"""

    def __init__(self, tables: dict[str, list[tuple[str, int]]] | None = None) -> None:
        self.tables = tables or {}
        self.calls: list[str] = []

    async def get_universe(self, market: str | None = None):
        self.calls.append(market or "all")
        return [_info(c, d) for c, d in self.tables.get(market, [])]


# ---------------------------------------------------------------------- #
# 权威表优先级
# ---------------------------------------------------------------------- #

def test_authoritative_map_overrides_static_rule():
    # 静态规则说 sh019742 是 4 位；权威表说 3 位 → 必须听权威表的
    assert price_decimal_digits(1, "019742") == 4
    install_decimal_map([(1, "019742", 3)], source="test")
    assert price_decimal_digits(1, "019742") == 3


def test_codes_outside_map_still_use_static_rule():
    install_decimal_map([(1, "019742", 3)], source="test")
    assert price_decimal_digits(1, "600000") == 2      # 股票
    assert price_decimal_digits(1, "510300") == 3      # ETF
    assert price_decimal_digits(0, "131810") == 4      # 深回购（表里没有）


def test_mixed_prefix_falls_back_to_per_code_lookup():
    """同一前缀出现两种小数位 → 该前缀不进前缀表，只把例外代码存进逐代码表。"""
    stats = install_decimal_map(
        [(1, "110001", 4), (1, "110002", 4), (1, "110003", 3)], source="test")
    assert stats["mixed_prefixes"] == 1
    assert stats["codes"] == 3           # 混合前缀下所有代码都进逐代码表
    assert price_decimal_digits(1, "110001") == 4
    assert price_decimal_digits(1, "110003") == 3


def test_clear_map_restores_static_rules():
    install_decimal_map([(1, "019742", 3)], source="test")
    assert price_decimal_digits(1, "019742") == 3
    clear_decimal_map()
    assert decimal_map_stats()["stock"] == 0
    assert price_decimal_digits(1, "019742") == 4


def test_reinstall_replaces_previous_map():
    install_decimal_map([(1, "019742", 3)], source="a")
    install_decimal_map([(1, "019742", 4)], source="b")
    assert price_decimal_digits(1, "019742") == 4
    assert decimal_map_stats()["prefixes"] == 1


# ---------------------------------------------------------------------- #
# 同步
# ---------------------------------------------------------------------- #

def test_refresh_installs_and_writes_cache(tmp_path):
    td = FakeTd({"sh": [("019742", 4), ("600000", 2)],
                 "sz": [("000001", 2), ("131810", 4)]})
    stats = asyncio.run(decimal_sync.refresh(td))

    assert td.calls == ["sh", "sz"]
    assert stats["stock"] == 4
    assert price_decimal_digits(1, "600000") == 2
    assert price_decimal_digits(0, "131810") == 4

    raw = json.loads((tmp_path / "decimal-points.json").read_text(encoding="utf-8"))
    assert raw["markets"]["1"]["019742"] == 4
    assert raw["markets"]["0"]["131810"] == 4


def test_refresh_skips_zero_decimal_point():
    td = FakeTd({"sh": [("019742", 0), ("600000", 2)]})
    stats = asyncio.run(decimal_sync.refresh(td, ("sh",)))
    assert stats["stock"] == 1


def test_refresh_ignores_unknown_market():
    td = FakeTd({"hk": [("00700", 3)]})
    stats = asyncio.run(decimal_sync.refresh(td, ("hk",)))
    assert stats["stock"] == 0
    assert not (decimal_sync.cache_file()).exists()


def test_load_cached_roundtrip(tmp_path):
    td = FakeTd({"sh": [("019742", 4)]})
    asyncio.run(decimal_sync.refresh(td))
    decimal_sync.reset()
    assert decimal_map_stats()["stock"] == 0

    assert decimal_sync.load_cached() == 1
    assert price_decimal_digits(1, "019742") == 4


def test_cache_expires_across_days(tmp_path):
    td = FakeTd({"sh": [("019742", 4)]})
    asyncio.run(decimal_sync.refresh(td))
    decimal_sync.reset()

    path = tmp_path / "decimal-points.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["date"] = "2000-01-01"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert decimal_sync.load_cached() == 0


def test_corrupt_cache_is_ignored(tmp_path):
    path = tmp_path / "decimal-points.json"
    path.write_text("{not json", encoding="utf-8")
    assert decimal_sync.load_cached() == 0


# ---------------------------------------------------------------------- #
# 调度
# ---------------------------------------------------------------------- #

def test_schedule_disabled_by_env(monkeypatch):
    monkeypatch.setenv("PYPYTDX_DATA_DISABLE_DECIMAL_SYNC", "1")
    td = FakeTd({"sh": [("019742", 4)]})

    async def run():
        task = decimal_sync.schedule(td)
        return task

    assert asyncio.run(run()) is None
    assert td.calls == []


def test_schedule_loads_cache_without_network(tmp_path):
    td = FakeTd({"sh": [("019742", 4)]})
    asyncio.run(decimal_sync.refresh(td))
    decimal_sync.reset()
    quiet = FakeTd({"sh": [("019742", 4)]})

    async def run():
        return decimal_sync.schedule(quiet)

    assert asyncio.run(run()) is None      # 缓存命中 → 不起 task
    assert quiet.calls == []               # 也没走网络
    assert price_decimal_digits(1, "019742") == 4


def test_schedule_runs_background_sync_without_cache():
    td = FakeTd({"sh": [("019742", 4)]})

    async def run():
        task = decimal_sync.schedule(td)
        assert task is not None
        await task
        return decimal_sync.status()

    st = asyncio.run(run())
    assert st["source"] == "universe"
    assert st["syncing"] is False
    assert td.calls == ["sh", "sz"]        # 默认两市


def test_cancel_stops_background_task():
    class SlowTd:
        def __init__(self) -> None:
            self.calls = 0

        async def get_universe(self, market=None):
            self.calls += 1
            await asyncio.sleep(5)
            return []

    td = SlowTd()

    async def run():
        task = decimal_sync.schedule(td)
        await asyncio.sleep(0.01)
        await decimal_sync.cancel(task)
        return task.done() or task.cancelled()

    assert asyncio.run(run()) is True


def test_status_shape():
    st = decimal_sync.status()
    assert set(st) >= {"loaded", "source", "syncing", "prefixes", "codes",
                       "mixed_prefixes", "stock"}
