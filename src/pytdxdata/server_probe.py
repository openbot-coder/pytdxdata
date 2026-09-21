"""服务器清单导入+自动探测工具

读 servers.json → 并发探测每台可用性与延时（标准/MAC用7709、EX用7727）→ 写回状态。

用法:
  python -m pytdxdata.server_probe              # 按申报协议全量探测
  python -m pytdxdata.server_probe --discover   # 额外测【每种协议】并据实修正 protocols
  python -m pytdxdata.server_probe --group ex   # 只测EX
  python -m pytdxdata.server_probe --refresh    # 只重测status=ok的

写回的字段
----------
``status`` / ``latency`` / ``checked_at`` / ``checked_date`` / ``fast_pool``
以及顶层 ``checked_date`` / ``note``。其中 ``fast_pool`` 与运行时缓存里的 ``fast``
用同一套规则（整体延迟前 10 名，或延迟 < 200ms），供"还没有当日缓存时"的冷启动排序。

``--discover`` 会顺带剔除 ip 非法的脏记录（如历史遗留的 ``"s1"``）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .config import is_valid_host
from .models import Adjust, KlinePeriod
from .pool.connection import TdxConnection
from .pool.ex_connection import ExTdxConnection
from .protocol.commands.ex_proto import GetExInstrumentBarsCmd
from .protocol.commands.kline import GetSecurityBarsCmd
from .protocol.commands.mac import MacKlineCmd

SERVERS_FILE = Path(__file__).resolve().parent / "servers.json"

# 与 pool/probe_cache.py 保持同一套入池规则
FAST_POOL_TOP_N = 10
FAST_POOL_MAX_LATENCY = 0.2

ALL_PROTOCOLS = ("standard", "mac", "ex")


def load_servers() -> dict:
    if not SERVERS_FILE.exists():
        raise FileNotFoundError(f"配置文件不存在: {SERVERS_FILE}")
    return json.loads(SERVERS_FILE.read_text(encoding="utf-8"))


def save_servers(cfg: dict) -> None:
    SERVERS_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


async def probe_standard(ip: str, timeout: float = 5.0) -> tuple[bool, float]:
    conn = TdxConnection(ip, 7709, timeout)
    t0 = time.monotonic()
    try:
        await conn.connect()
        bars = await conn.execute(GetSecurityBarsCmd(1, "600000", KlinePeriod.DAY, 0, 100))
        return (bool(bars), time.monotonic() - t0)
    except Exception:
        return (False, time.monotonic() - t0)
    finally:
        await conn.close()


async def probe_mac(ip: str, timeout: float = 5.0) -> tuple[bool, float]:
    conn = TdxConnection(ip, 7709, timeout)
    t0 = time.monotonic()
    try:
        await conn.connect()
        bars = await conn.execute(MacKlineCmd(1, "600000", KlinePeriod.DAY, Adjust.NONE, 0, 100))
        return (bool(bars), time.monotonic() - t0)
    except Exception:
        return (False, time.monotonic() - t0)
    finally:
        await conn.close()


async def probe_ex(ip: str, handshake: str, timeout: float = 5.0) -> tuple[bool, float]:
    conn = ExTdxConnection(ip, 7727, timeout, handshake)
    t0 = time.monotonic()
    try:
        await conn.connect()
        bars = await conn.execute(GetExInstrumentBarsCmd(31, "00700", 0, 0, 5))
        return (bool(bars), time.monotonic() - t0)
    except Exception:
        return (False, time.monotonic() - t0)
    finally:
        await conn.close()


async def probe_proto(proto: str, ip: str, handshake: str, timeout: float) -> tuple[bool, float]:
    """按协议分派到具体探针。"""
    if proto == "ex":
        return await probe_ex(ip, handshake, timeout)
    if proto == "mac":
        return await probe_mac(ip, timeout)
    return await probe_standard(ip, timeout)


async def probe_one(server: dict, timeout: float, discover: bool = False) -> dict:
    ip = server["ip"]
    declared = server.get("protocols", [])
    handshake = server.get("handshake") or "setup"
    # discover: 三种协议全测（EX 先按申报握手，失败再试另一种）；
    # 默认: 只测申报的协议（保持原行为，避免误改人工裁剪的清单）
    targets = list(ALL_PROTOCOLS) if discover else list(declared)

    per: dict[str, float | None] = {}
    for proto in targets:
        if proto == "ex":
            ok, lat = await probe_ex(ip, handshake, timeout)
            if not ok and discover:
                # 握手模式申报错的节点：换一种再试（login <-> setup）
                alt = "setup" if handshake == "login" else "login"
                ok, lat = await probe_ex(ip, alt, timeout)
        else:
            ok, lat = await probe_proto(proto, ip, handshake, timeout)
            if not ok and discover:
                # discover 会据结果**收窄 protocols**，单次抖动就会误删协议 → 失败重试一次
                ok, lat = await probe_proto(proto, ip, handshake, timeout)
        if ok:
            per[proto] = lat

    if discover and per:
        # 据实测结果修正申报协议（保序：standard / mac / ex）
        server["protocols"] = [p for p in ALL_PROTOCOLS if p in per]

    server["status"] = "ok" if per else "fail"
    server["latency"] = round(min(per.values()), 3) if per else None
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    server["checked_at"] = now
    server["checked_date"] = now[:10]
    server.setdefault("fast_pool", False)
    # 供表格打印用，不落盘
    server["_per_proto"] = {p: (round(v, 3) if v is not None else None) for p, v in per.items()}
    return server


async def main() -> None:
    ap = argparse.ArgumentParser(description="服务器清单导入+自动探测")
    ap.add_argument("--group", choices=["standard", "mac", "ex"], default=None)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--refresh", action="store_true", help="只重测status=ok的")
    ap.add_argument("--discover", action="store_true",
                    help="三种协议全测并据实修正 protocols（同时剔除 ip 非法的脏记录）")
    args = ap.parse_args()

    cfg = load_servers()
    all_servers = cfg["servers"]

    # --- 清理：ip 不合法的脏记录（历史遗留，如 "s1"） ---
    junk = [s.get("ip") for s in all_servers if not is_valid_host(s.get("ip"))]
    if junk:
        cfg["servers"] = [s for s in all_servers if is_valid_host(s.get("ip"))]
        all_servers = cfg["servers"]
        print("已剔除 ip 非法记录: %s" % ", ".join(map(str, junk)))

    servers = all_servers
    if args.group:
        servers = [s for s in servers if args.group in s.get("protocols", [])]
    if args.refresh:
        servers = [s for s in servers if s.get("status") == "ok"]
    if not servers:
        print("没有待探测的服务器")
        return

    print("=" * 96)
    print("探测 %d 台服务器 (标准/MAC:7709, EX:7727)%s..." % (
        len(servers), "  [discover: 全协议]" if args.discover else ""))
    print("=" * 96)
    t0 = time.time()
    results = await asyncio.gather(
        *[probe_one(s, args.timeout, args.discover) for s in servers]
    )
    elapsed = time.time() - t0

    # --- 入池标记：与运行时缓存同一规则（前N名 或 <200ms） ---
    ok_list = [s for s in results if s["status"] == "ok"]
    ok_list.sort(key=lambda s: s["latency"])
    fast_ips = {s["ip"] for i, s in enumerate(ok_list)
                if i < FAST_POOL_TOP_N or s["latency"] < FAST_POOL_MAX_LATENCY}
    for s in ok_list:
        s["fast_pool"] = s["ip"] in fast_ips
    fail_list = [s for s in results if s["status"] == "fail"]
    for s in fail_list:
        s["fast_pool"] = False

    # --- 写回（剔除内部诊断字段） ---
    today = time.strftime("%Y-%m-%d")
    updated = {s["ip"]: {k: v for k, v in s.items() if not k.startswith("_")} for s in results}
    for s in cfg["servers"]:
        if s["ip"] in updated:
            s.update(updated[s["ip"]])
    cfg["checked_date"] = today
    cfg["version"] = today  # 清单基线日期（无代码读取，仅标注本次复测时间）
    cfg["note"] = "离线探针复测(%s %s): 可达=%d/%d, 入池fast_pool=%d台" % (
        today, time.strftime("%H:%M"), len(ok_list), len(results), len(fast_ips))
    save_servers(cfg)

    print("\n可用: %d/%d (%.1fs)  入池: %d台" % (len(ok_list), len(results), elapsed, len(fast_ips)))
    print("-" * 96)
    for s in ok_list:
        per = s.get("_per_proto", {})
        detail = " ".join(
            "%s=%s" % (p, ("%.3fs" % v) if v is not None else "-")
            for p, v in per.items()
        ) or "/".join(s.get("protocols", []))
        print("  [OK] %-16s %-24s %-24s %.3fs  %s%s" % (
            s["ip"], "/".join(s.get("protocols", [])), s["name"], s["latency"],
            "★" if s["fast_pool"] else " ", detail))
    if fail_list:
        print("\n不可用: %d台" % len(fail_list))
        for s in fail_list:
            print("  [X] %-16s %-24s %s" % (
                s["ip"], "/".join(s.get("protocols", [])), s["name"]))
    print("\n已写回 %s" % SERVERS_FILE)


if __name__ == "__main__":
    asyncio.run(main())
