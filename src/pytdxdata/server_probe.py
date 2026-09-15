"""服务器清单导入+自动探测工具

读 servers.json → 并发探测每台可用性与延时（标准/MAC用7709、EX用7727）→ 写回状态。

用法:
  python -m pytdxdata.server_probe            # 全量探测
  python -m pytdxdata.server_probe --group ex # 只测EX
  python -m pytdxdata.server_probe --refresh  # 重新探测(status=ok的)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from .pool.connection import TdxConnection
from .pool.ex_connection import ExTdxConnection
from .protocol.commands.kline import GetSecurityBarsCmd
from .protocol.commands.mac import MacKlineCmd
from .protocol.commands.ex_proto import GetExInstrumentBarsCmd
from .models import KlinePeriod, Adjust

SERVERS_FILE = Path(__file__).resolve().parent / "servers.json"


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
        bars = await conn.execute(GetSecurityBarsCmd(1, "600000", 5, 0, 100))
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


async def probe_one(server: dict, timeout: float) -> dict:
    ip = server["ip"]
    protocols = server.get("protocols", [])
    best_lat = None
    ok = False
    # 逐协议探测（一台多协议: standard/mac同端口可复用连接——简化逐协议测）
    for proto in protocols:
        if proto == "ex":
            r, lat = await probe_ex(ip, server.get("handshake", "setup"), timeout)
        elif proto == "mac":
            r, lat = await probe_mac(ip, timeout)
        else:
            r, lat = await probe_standard(ip, timeout)
        if r:
            ok = True
            best_lat = lat if best_lat is None else min(best_lat, lat)
    server["status"] = "ok" if ok else "fail"
    server["latency"] = round(best_lat, 3) if best_lat is not None else None
    server["checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return server


async def main() -> None:
    ap = argparse.ArgumentParser(description="服务器清单导入+自动探测")
    ap.add_argument("--group", choices=["standard", "mac", "ex"], default=None)
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--refresh", action="store_true", help="只重测status=ok的")
    args = ap.parse_args()

    cfg = load_servers()
    servers = cfg["servers"]
    if args.group:
        servers = [s for s in servers if args.group in s.get("protocols", [])]
    if args.refresh:
        servers = [s for s in servers if s.get("status") == "ok"]
    if not servers:
        print("没有待探测的服务器")
        return

    print("=" * 90)
    print("探测 %d 台服务器 (标准/MAC:7709, EX:7727)..." % len(servers))
    print("=" * 90)
    t0 = time.time()
    results = await asyncio.gather(*[probe_one(s, args.timeout) for s in servers])
    elapsed = time.time() - t0

    # 写回
    updated = {s["ip"]: s for s in results}
    for s in cfg["servers"]:
        if s["ip"] in updated:
            s.update(updated[s["ip"]])
    save_servers(cfg)

    ok_list = [s for s in results if s["status"] == "ok"]
    ok_list.sort(key=lambda s: s["latency"])
    fail_list = [s for s in results if s["status"] == "fail"]
    print("\n可用: %d/%d (%.1fs)" % (len(ok_list), len(results), elapsed))
    print("-" * 90)
    for s in ok_list:
        print("  [OK] %-16s %-14s %-28s %.3fs  %s" % (
            s["ip"], "/".join(s.get("protocols", [])), s["name"],
            s["latency"], s.get("handshake") or ""))
    if fail_list:
        print("\n不可用: %d台" % len(fail_list))
        for s in fail_list:
            print("  [X] %-16s %s" % (s["ip"], "/".join(s.get("protocols", []))))
    print("\n已写回 %s" % SERVERS_FILE)


if __name__ == "__main__":
    asyncio.run(main())
