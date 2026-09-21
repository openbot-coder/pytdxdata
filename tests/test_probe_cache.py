"""当日测速缓存单元测试（注入假探针与假日期，不连真实网络）

覆盖点：
- 当日首次启动测一次；当天已有缓存则**不刷新**（探针一次都不发起）
- 跨日 / 缓存损坏 / 版本不符 → 重新测速
- 结果按实测延迟升序被 config 采用
- **绝不改写包内 servers.json**，缓存落在用户级缓存目录
- 全部不可达 → 不写缓存（下次启动重试）
- 并发调用只真正测一次（单飞）
- 非法主机名（测试 mock 混入的 "s1" 等）被挡在测速之外
- 连接池 start() 不阻塞、close() 会取消后台测速
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from pytdxdata import config
from pytdxdata.pool.probe_cache import ProbeCache

TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
PROTOS = ("standard", "mac", "ex")


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """把用户级缓存目录指到临时目录，并复位所有相关环境变量。"""
    monkeypatch.setenv("PYPYTDX_DATA_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("PYPYTDX_DATA_DISABLE_PROBE", raising=False)
    for var in ("PYPYTDX_DATA_STANDARD_HOSTS", "PYPYTDX_DATA_MAC_HOSTS",
                "PYPYTDX_DATA_EX_HOSTS"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def fake_probers(latencies: dict[str, dict[str, float | None]], calls: list):
    """构造假探针；latencies = {协议: {主机: 延迟或 None}}。"""
    def _make(protocol: str):
        async def _probe(host: str, timeout: float) -> float | None:
            calls.append((protocol, host))
            return latencies.get(protocol, {}).get(host)
        return _probe
    return {p: _make(p) for p in PROTOS}


def patch_candidates(monkeypatch, mapping: dict[str, list[str]]) -> None:
    """把"全量候选"换成受控清单，避免依赖真实服务器清单。"""
    monkeypatch.setattr("pytdxdata.pool.probe_cache.all_probe_candidates",
                        lambda: mapping)


def write_cache(path: Path, day: str, results: dict, version: int = 1) -> None:
    path.write_text(json.dumps({"version": version, "date": day, "results": results}),
                    encoding="utf-8")


# ---------- 是否该测速 ----------

def test_needs_probe_when_no_cache(tmp_path):
    pc = ProbeCache(tmp_path / "c.json", today_fn=lambda: TODAY)
    assert pc.needs_probe_today() is True
    assert pc.read_cache() is None


def test_needs_probe_false_for_today_cache(tmp_path):
    cache = tmp_path / "c.json"
    write_cache(cache, TODAY, {"1.1.1.1": {"standard": 0.05}})
    pc = ProbeCache(cache, today_fn=lambda: TODAY)
    assert pc.needs_probe_today() is False


def test_stale_cache_needs_probe(tmp_path):
    cache = tmp_path / "c.json"
    write_cache(cache, YESTERDAY, {"1.1.1.1": {"standard": 0.05}})
    pc = ProbeCache(cache, today_fn=lambda: TODAY)
    assert pc.needs_probe_today() is True


def test_version_mismatch_needs_probe(tmp_path):
    cache = tmp_path / "c.json"
    write_cache(cache, TODAY, {"1.1.1.1": {"standard": 0.05}}, version=999)
    pc = ProbeCache(cache, today_fn=lambda: TODAY)
    assert pc.needs_probe_today() is True


@pytest.mark.parametrize("content", ["{not json", "[]", '{"version":1}', ""])
def test_broken_cache_is_ignored_not_crash(tmp_path, content):
    cache = tmp_path / "c.json"
    cache.write_text(content, encoding="utf-8")
    pc = ProbeCache(cache, today_fn=lambda: TODAY)
    assert pc.read_cache() is None
    assert pc.needs_probe_today() is True


# ---------- 核心行为：当天已有缓存就不刷新 ----------

async def test_today_cache_is_not_refreshed(tmp_path, monkeypatch):
    cache = tmp_path / "c.json"
    write_cache(cache, TODAY, {"1.1.1.1": {"standard": 0.05}})
    before = cache.read_text(encoding="utf-8")
    calls: list = []
    patch_candidates(monkeypatch, {"standard": ["1.1.1.1"], "mac": [], "ex": []})

    pc = ProbeCache(cache, probers=fake_probers({}, calls), today_fn=lambda: TODAY)
    await pc.ensure_probe()

    assert calls == []                                        # 未发起任何测速
    assert cache.read_text(encoding="utf-8") == before         # 缓存内容原封不动


async def test_stale_cache_is_refreshed(tmp_path, monkeypatch):
    cache = tmp_path / "c.json"
    write_cache(cache, YESTERDAY, {"9.9.9.9": {"standard": 9.0}})
    calls: list = []
    patch_candidates(monkeypatch, {"standard": ["1.1.1.1"], "mac": [], "ex": []})

    pc = ProbeCache(cache,
                    probers=fake_probers({"standard": {"1.1.1.1": 0.02}}, calls),
                    today_fn=lambda: TODAY)
    await pc.ensure_probe()

    assert calls == [("standard", "1.1.1.1")]
    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["date"] == TODAY and data["version"] == config.PROBE_CACHE_VERSION
    assert data["results"] == {"1.1.1.1": {"standard": 0.02}}
    assert data["hosts"]["standard"] == ["1.1.1.1"]
    assert "9.9.9.9" not in data["results"]     # 旧结果被整体替换，不残留


# ---------- 缓存写入 ----------

async def test_write_is_atomic_and_leaves_no_tmp(tmp_path, monkeypatch):
    cache = tmp_path / "sub" / "c.json"          # 目录不存在也要能建
    patch_candidates(monkeypatch, {"standard": ["1.1.1.1"], "mac": [], "ex": []})
    pc = ProbeCache(cache,
                    probers=fake_probers({"standard": {"1.1.1.1": 0.03}}, []),
                    today_fn=lambda: TODAY)
    await pc.ensure_probe()

    assert cache.exists()
    assert list(cache.parent.glob("*.tmp*")) == []
    assert json.loads(cache.read_text(encoding="utf-8"))["probed_at"]


async def test_all_unreachable_writes_nothing(tmp_path, monkeypatch):
    cache = tmp_path / "c.json"
    patch_candidates(monkeypatch, {"standard": ["1.1.1.1"], "mac": [], "ex": []})
    pc = ProbeCache(cache, probers=fake_probers({}, []), today_fn=lambda: TODAY)
    await pc.ensure_probe()

    assert not cache.exists()          # 不把"全挂"缓存一整天
    assert pc.needs_probe_today() is True


async def test_never_touches_package_servers_json(tmp_path, monkeypatch):
    """最关键的一条：测速**绝不能**改写随包分发的 servers.json。"""
    servers_json = config._SERVERS_FILE
    before_bytes = servers_json.read_bytes()
    before_mtime = servers_json.stat().st_mtime_ns

    patch_candidates(monkeypatch, {"standard": ["1.1.1.1"], "mac": [], "ex": []})
    pc = ProbeCache(probers=fake_probers({"standard": {"1.1.1.1": 0.04}}, []),
                    today_fn=lambda: TODAY)
    await pc.ensure_probe()

    assert servers_json.read_bytes() == before_bytes
    assert servers_json.stat().st_mtime_ns == before_mtime
    assert pc.cache_file() == tmp_path / "hosts-today.json"    # 落在用户缓存目录
    assert Path(config.probe_cache_file()).parent == tmp_path


async def test_results_are_recorded_per_protocol(tmp_path, monkeypatch):
    cache = tmp_path / "c.json"
    patch_candidates(monkeypatch, {
        "standard": ["1.1.1.1", "2.2.2.2"],
        "mac": ["1.1.1.1", "2.2.2.2"],
        "ex": ["3.3.3.3"],
    })
    lats = {"standard": {"1.1.1.1": 0.30, "2.2.2.2": 0.10},
            "mac": {"1.1.1.1": 0.20},   # 2.2.2.2 的 mac 故意不给 → 应记为 None
            "ex": {"3.3.3.3": 0.05}}
    pc = ProbeCache(cache, probers=fake_probers(lats, []), today_fn=lambda: TODAY)
    await pc.ensure_probe()

    data = json.loads(cache.read_text(encoding="utf-8"))
    assert data["hosts"]["standard"] == ["2.2.2.2", "1.1.1.1"]   # 按延迟升序
    assert data["hosts"]["mac"] == ["1.1.1.1"]                   # 不可达的 2.2.2.2 被排除
    assert data["hosts"]["ex"] == ["3.3.3.3"]
    # 2.2.2.2 的 mac 探针返回 None，也要如实记录（便于事后诊断）
    assert data["results"]["2.2.2.2"]["mac"] is None


# ---------- 单飞 / 开关 / 脏数据 ----------

async def test_concurrent_ensure_probe_runs_once(tmp_path, monkeypatch):
    patch_candidates(monkeypatch, {"standard": ["1.1.1.1", "2.2.2.2"], "mac": [], "ex": []})
    calls: list = []
    pc = ProbeCache(tmp_path / "c.json",
                    probers=fake_probers({"standard": {"1.1.1.1": 0.01,
                                                       "2.2.2.2": 0.02}}, calls),
                    today_fn=lambda: TODAY)
    await asyncio.gather(pc.ensure_probe(), pc.ensure_probe(), pc.ensure_probe())

    assert sorted(calls) == [("standard", "1.1.1.1"), ("standard", "2.2.2.2")]


async def test_disabled_env_skips_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("PYPYTDX_DATA_DISABLE_PROBE", "1")
    calls: list = []
    patch_candidates(monkeypatch, {"standard": ["1.1.1.1"], "mac": [], "ex": []})
    pc = ProbeCache(tmp_path / "c.json",
                    probers=fake_probers({"standard": {"1.1.1.1": 0.01}}, calls),
                    today_fn=lambda: TODAY)

    assert pc.needs_probe_today() is False
    await pc.ensure_probe()
    assert calls == []
    assert not (tmp_path / "c.json").exists()


async def test_illegal_hosts_are_filtered_out(tmp_path, monkeypatch):
    """测试 mock 常传入 "s1"/"m1" 这类伪主机名，必须挡在缓存之外。"""
    patch_candidates(monkeypatch, {"standard": [], "mac": [], "ex": []})
    calls: list = []
    pc = ProbeCache(tmp_path / "c.json",
                    probers=fake_probers({"standard": {"1.1.1.1": 0.01}}, calls),
                    today_fn=lambda: TODAY)
    await pc.ensure_probe(["s1", "m1", "1.1.1.1", ""])

    assert calls == [("standard", "1.1.1.1")]
    data = json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert "s1" not in data["results"] and "m1" not in data["results"]


async def test_reset_allows_recheck(tmp_path, monkeypatch):
    patch_candidates(monkeypatch, {"standard": ["1.1.1.1"], "mac": [], "ex": []})
    calls: list = []
    pc = ProbeCache(tmp_path / "c.json",
                    probers=fake_probers({"standard": {"1.1.1.1": 0.01}}, calls),
                    today_fn=lambda: TODAY)
    await pc.ensure_probe()
    await pc.ensure_probe()
    assert len(calls) == 1          # 第二次被单飞挡掉

    pc.reset()
    await pc.ensure_probe()
    assert len(calls) == 1          # 重置后当天已有缓存，仍不重测


# ---------- config 侧：采用顺序与兜底 ----------

def test_config_getters_prefer_cache_and_order_by_latency(tmp_path, monkeypatch):
    monkeypatch.setenv("PYPYTDX_DATA_CACHE_DIR", str(tmp_path))
    (tmp_path / "hosts-today.json").write_text(json.dumps({
        "version": config.PROBE_CACHE_VERSION, "date": TODAY,
        "results": {
            "3.3.3.3": {"standard": 0.9},
            "1.1.1.1": {"standard": 0.05},
            "2.2.2.2": {"standard": 0.4, "mac": 0.1},
            "9.9.9.9": {"ex": 0.2},
        },
    }), encoding="utf-8")

    assert config.get_standard_hosts() == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]
    assert config.get_mac_hosts() == ["2.2.2.2"]
    assert config.get_ex_hosts() == ["9.9.9.9"]


def test_config_getters_fall_back_without_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PYPYTDX_DATA_CACHE_DIR", str(tmp_path))
    assert config.get_standard_hosts() == config._load_ok_hosts("standard", fast_only=True)
    assert config.get_mac_hosts() == config._load_ok_hosts("mac", fast_only=True)
    assert config.get_ex_hosts() == config._load_ok_hosts("ex")


def test_env_override_beats_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PYPYTDX_DATA_CACHE_DIR", str(tmp_path))
    (tmp_path / "hosts-today.json").write_text(json.dumps({
        "version": config.PROBE_CACHE_VERSION, "date": TODAY,
        "results": {"1.1.1.1": {"standard": 0.05}},
    }), encoding="utf-8")
    monkeypatch.setenv("PYPYTDX_DATA_STANDARD_HOSTS", "8.8.8.8")
    assert config.get_standard_hosts() == ["8.8.8.8"]


def test_all_probe_candidates_covers_full_set_not_filtered():
    cand = config.all_probe_candidates()
    assert set(config.STANDARD_HOSTS) <= set(cand["standard"])
    assert set(config.MAC_HOSTS) <= set(cand["mac"])
    assert set(config.EX_HOSTS) <= set(cand["ex"])
    assert "117.34.114.17" in cand["standard"]      # 未申报 MAC 的节点也要被测到
    assert "s1" not in cand["standard"]            # 包内清单里的脏数据被挡


@pytest.mark.parametrize(("host", "expected"), [
    ("1.2.3.4", True),
    ("117.34.114.17", True),
    ("a.b", True),
    ("good-host.com", True),
    ("s1", False),
    ("m1", False),
    ("", False),
    ("   ", False),
    ("-bad.com", False),
    ("bad-.com", False),
    ("has space.com", False),
    (123, False),
    (None, False),
])
def test_is_valid_host(host, expected):
    assert config.is_valid_host(host) is expected


# ---------- 连接池集成 ----------

async def test_pool_start_does_not_block_and_close_cancels_probe(monkeypatch):
    from pytdxdata.pool import pool as pool_mod

    cancel_seen = asyncio.Event()
    created: list[asyncio.Task] = []

    async def _hang(hosts=None):
        try:
            await asyncio.sleep(30)          # 模拟耗时测速
        except asyncio.CancelledError:
            cancel_seen.set()
            raise

    def _fake_schedule(hosts=None):
        task = asyncio.create_task(_hang())
        created.append(task)
        return task

    monkeypatch.setattr(pool_mod, "schedule_probe", _fake_schedule)

    p = pool_mod.DynamicPool([], min_size=1, max_size=1)
    t0 = time.monotonic()
    await p.start()
    assert time.monotonic() - t0 < 1.0        # start() 不等测速
    assert p._probe_task is created[0]

    await asyncio.sleep(0)                    # 让测速任务真正跑起来再关闭
    await p.close()
    assert p._probe_task is None
    assert created[0].cancelled()
    assert cancel_seen.is_set()


async def test_pool_auto_probe_false_schedules_nothing(monkeypatch):
    from pytdxdata.pool import pool as pool_mod

    def _boom(hosts=None):  # pragma: no cover - 不应被调用
        raise AssertionError("auto_probe=False 时不应调度测速")

    monkeypatch.setattr(pool_mod, "schedule_probe", _boom)
    p = pool_mod.DynamicPool([], min_size=1, max_size=1, auto_probe=False)
    await p.start()
    assert p._probe_task is None
    await p.close()
