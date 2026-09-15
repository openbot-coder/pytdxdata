"""服务器清单与默认配置

服务器清单存于包内 servers.json，由 server_probe.py 导入时自动探测可用性与延时。
config.py 加载时只采用 status="ok" 的服务器；环境变量可覆盖；代码内嵌清单为兜底。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_PORT = 7709
DEFAULT_TIMEOUT = 10.0

_SERVERS_FILE = Path(__file__).resolve().parent / "servers.json"  # 包内清单(随包分发)


def _load_ok_hosts(protocol: str, *, fast_only: bool = False) -> list[str]:
    """从 servers.json 加载 status=ok 且支持指定协议的服务器。

    fast_only=True 时只返回当日测速入池(fast_pool)的服务器（排名前N或延迟<阈值），
    用于动态连接池优先使用优质节点。
    """
    try:
        cfg = json.loads(_SERVERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for s in cfg.get("servers", []):
        if s.get("status") != "ok":
            continue
        if protocol not in s.get("protocols", []):
            continue
        if fast_only and not s.get("fast_pool", False):
            continue
        out.append(s["ip"])
    return out


def _load_ex_handshakes() -> dict[str, str]:
    try:
        cfg = json.loads(_SERVERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {s["ip"]: s.get("handshake", "setup") for s in cfg.get("servers", [])
            if "ex" in s.get("protocols", []) and s.get("status") == "ok"}

# 标准行情服务器 —— 真实K线探测验证可用(2026-08-12, GetSecurityBarsCmd 600000 日K)
# 27台可用 = pytdxwrapper券商服务器(国泰君安/中信短线宝/华林/安信等) + 通达信官方主站
#   来源①: pytdxwrapper config.py TDX_HOSTS(券商+官方, 51台探测27可用)
#   来源②: easy-tdx known_hosts + connect.cfg官方主站(54台探测仅5可用, 已被券商清单覆盖)
# 另2台端口80可用(池统一7709暂不收录): 180.153.18.172 / 202.108.253.139
# 不可用特征: K线空响应(握手正常无数据, 仅ping测不出) —— 探测脚本: examples/probe_pytdxwrapper.py / probe_all_hosts.py
STANDARD_HOSTS: list[str] = [
    "139.9.52.158", "103.251.85.94", "58.67.221.146", "59.36.5.11",
    "103.221.142.82", "218.106.92.182", "60.12.136.251", "218.106.92.183",
    "60.12.136.250", "115.238.90.170", "220.178.55.86", "220.178.55.71",
    "180.153.18.170", "115.238.90.165", "60.191.117.167", "218.75.126.9",
    "115.238.56.198", "117.34.114.16", "159.75.55.232", "117.34.114.17",
    "117.34.114.14", "117.34.114.15", "117.34.114.13", "117.34.114.27",
    "117.34.114.18", "117.34.114.20", "117.34.114.30",
]

# MAC 服务器（独立主机池, 2026-08-12 全量复测: 24台→23台可用, 剔除117.34.114.17空响应）
# MAC专用(3): 121.37.207.165 / 123.60.47.136 / 121.36.248.138 (对标准命令返回空, 池探测用MacKlineCmd)
# 标准服务器MAC支持20台: 券商(国泰君安/中信/安信)与官方主站, 与STANDARD_HOSTS同端口7709
# 标准服务器不支持MAC的6台: 218.106.92.182/183 220.178.55.71/86(华林) 159.75.55.232 117.34.114.30
# 探测脚本: examples/probe_mac_all.py / probe_mac_support.py
MAC_HOSTS: list[str] = [
    "121.37.207.165", "139.9.52.158", "58.67.221.146", "59.36.5.11",
    "60.12.136.250", "60.12.136.251", "121.36.248.138", "60.191.117.167",
    "103.221.142.82", "218.75.126.9", "123.60.47.136", "180.153.18.170",
    "115.238.56.198", "115.238.90.170", "115.238.90.165", "103.251.85.94",
    "117.34.114.15", "117.34.114.18", "117.34.114.14", "117.34.114.20",
    "117.34.114.13", "117.34.114.27", "117.34.114.16",
]

# EX扩展行情服务器（港股/期货/美股，端口7727）
# MAC EX专用2台(0x2454 login 80B): 116.205.135.205 / 121.37.232.167
# pytdx EX协议2台(setup 0x2454 92B): 通达信扩展市场116.205.143.214(52市场) + 国泰君安103.221.142.82(60市场)
# 发现方式(2026-08-12): 券商服务器同机多协议——103.221.142.82同时是MAC服务器(7709)与EX服务器(7727)
EX_HOSTS: list[str] = [
    "116.205.135.205", "121.37.232.167", "116.205.143.214", "103.221.142.82",
]
# 各服务器握手模式: login(0x2454 login) / setup(0x2454 setup)
EX_HANDSHAKES: dict[str, str] = {
    "116.205.135.205": "login",
    "121.37.232.167": "login",
    "116.205.143.214": "setup",
    "103.221.142.82": "setup",
}


def get_standard_hosts() -> list[str]:
    env = os.environ.get("PYPYTDX_DATA_STANDARD_HOSTS")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    fast = _load_ok_hosts("standard", fast_only=True)
    if fast:
        return fast
    from_json = _load_ok_hosts("standard")
    return from_json or list(STANDARD_HOSTS)


def get_mac_hosts() -> list[str]:
    env = os.environ.get("PYPYTDX_DATA_MAC_HOSTS")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    fast = _load_ok_hosts("mac", fast_only=True)
    if fast:
        return fast
    from_json = _load_ok_hosts("mac")
    return from_json or list(MAC_HOSTS)


def get_ex_hosts() -> list[str]:
    env = os.environ.get("PYPYTDX_DATA_EX_HOSTS")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    from_json = _load_ok_hosts("ex")
    return from_json or list(EX_HOSTS)


def get_ex_handshakes() -> dict[str, str]:
    """EX服务器握手模式（json优先，代码兜底）。"""
    from_json = _load_ex_handshakes()
    return from_json or dict(EX_HANDSHAKES)
