"""服务器清单与默认配置

服务器清单存于包内 ``servers.json`` —— 它是**随包分发的基线清单，运行期只读**。
每日首次启动的后台测速结果写入**用户级缓存目录**，运行期优先采用当日实测结果。

主机选取优先级（每个协议独立）::

    环境变量 PYPYTDX_DATA_*_HOSTS  >  当日测速缓存  >  包内清单 fast_pool
                                   >  包内清单 ok    >  代码内嵌常量

相关环境变量：

- ``PYPYTDX_DATA_CACHE_DIR``     覆盖用户级缓存目录（默认见 :func:`probe_cache_dir`）
- ``PYPYTDX_DATA_DISABLE_PROBE`` 置真值时既不读当日缓存也不测速（CI / 测试用，保证确定性）

为什么不把测速结果写回包内 ``servers.json``：安装目录可能是只读的（容器 / 系统级
site-packages / pipx），且多用户共用一份安装时彼此覆盖，运行期改写分发数据本身也不妥。
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
from datetime import date
from pathlib import Path

DEFAULT_PORT = 7709
DEFAULT_TIMEOUT = 10.0

_SERVERS_FILE = Path(__file__).resolve().parent / "servers.json"  # 包内清单(随包分发)

# 当日测速缓存（结构变更时递增，旧版本缓存自动失效）
PROBE_CACHE_VERSION = 1
_PROBE_CACHE_NAME = "hosts-today.json"


# ---------- 包内清单（只读基线） ----------
def _read_servers_json() -> dict:
    """读取包内清单；文件缺失 / 内容损坏 / 顶层非对象 → 空 dict。"""
    try:
        cfg = json.loads(_SERVERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _load_ok_hosts(protocol: str, *, fast_only: bool = False) -> list[str]:
    """从 servers.json 加载 status=ok 且支持指定协议的服务器。

    fast_only=True 时只返回测速入池(fast_pool)的服务器（排名前N或延迟<阈值），
    用于动态连接池优先使用优质节点。
    """
    out = []
    for s in _read_servers_json().get("servers", []):
        if s.get("status") != "ok":
            continue
        if protocol not in s.get("protocols", []):
            continue
        if fast_only and not s.get("fast_pool", False):
            continue
        out.append(s["ip"])
    return out


def _load_ex_handshakes() -> dict[str, str]:
    """包内清单里 EX 服务器的握手模式。"""
    return {s["ip"]: s.get("handshake", "setup")
            for s in _read_servers_json().get("servers", [])
            if "ex" in s.get("protocols", []) and s.get("status") == "ok"}


# ---------- 用户级当日测速缓存 ----------
def probe_cache_dir() -> Path:
    """用户级缓存目录（保证可写，绝不指向安装目录）。

    ``PYPYTDX_DATA_CACHE_DIR`` 可覆盖（测试指向临时目录）。按平台约定：

    - Windows: ``%LOCALAPPDATA%\\pytdxdata``
    - macOS:   ``~/Library/Caches/pytdxdata``
    - 其他:    ``$XDG_CACHE_HOME/pytdxdata`` 或 ``~/.cache/pytdxdata``
    """
    env = (os.environ.get("PYPYTDX_DATA_CACHE_DIR") or "").strip()
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return (Path(base) if base else Path.home() / "AppData" / "Local") / "pytdxdata"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pytdxdata"
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "pytdxdata"


def probe_cache_file() -> Path:
    """当日测速缓存文件路径（每次调用重新解析，便于测试改环境变量后立即生效）。"""
    return probe_cache_dir() / _PROBE_CACHE_NAME


def probe_disabled() -> bool:
    """是否已通过 ``PYPYTDX_DATA_DISABLE_PROBE`` 关闭当日测速缓存。"""
    return os.environ.get("PYPYTDX_DATA_DISABLE_PROBE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def load_probe_cache(today: str | None = None,
                     path: str | Path | None = None) -> dict | None:
    """读取**当日**测速缓存；缺失 / 损坏 / 非今日 / 版本不符 → ``None``。

    返回非 None 即表示"今天已经测过速了"，调用方应直接复用而不再刷新。
    ``path`` 可显式指定缓存文件（默认 :func:`probe_cache_file`），便于注入与测试。
    """
    if probe_disabled():
        return None
    target = Path(path) if path is not None else probe_cache_file()
    try:
        cfg = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict) or cfg.get("version") != PROBE_CACHE_VERSION:
        return None
    if str(cfg.get("date", "")) != (today or date.today().isoformat()):
        return None
    results = cfg.get("results")
    if not isinstance(results, dict) or not results:
        return None
    return cfg


def _load_cached_hosts(protocol: str) -> list[str]:
    """当日测速缓存中该协议**实测可达**的主机，按延迟升序（越快越靠前）。"""
    cfg = load_probe_cache()
    if cfg is None:
        return []
    rows: list[tuple[float, str]] = []
    for ip, per_proto in cfg["results"].items():
        if not isinstance(per_proto, dict):
            continue
        lat = per_proto.get(protocol)
        if isinstance(lat, bool) or not isinstance(lat, (int, float)):
            continue
        if lat < 0:
            continue
        rows.append((float(lat), str(ip)))
    rows.sort(key=lambda kv: (kv[0], kv[1]))
    return [ip for _, ip in rows]


# ---------- 代码内嵌兜底清单 ----------
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


# ---------- 测速候选枚举 ----------
def is_valid_host(host: object) -> bool:
    """是否是可用作服务器地址的合法主机（IPv4/IPv6 字面量，或含点的域名）。

    用于挡住测试 mock 混进来的伪主机名（如 ``"s1"``）被写进缓存 / 清单。
    """
    if not isinstance(host, str):
        return False
    h = host.strip()
    if not h or len(h) > 253 or any(c.isspace() for c in h):
        return False
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    return "." in h and all(
        part and part[0] != "-" and part[-1] != "-" and part.replace("-", "").isalnum()
        for part in h.split(".")
    )


def all_probe_candidates() -> dict[str, list[str]]:
    """[测速用] 返回 ``{协议: [候选主机]}`` 全量清单（已校验、去重、保序）。

    来源 = 包内清单申报的协议 ∪ 代码内嵌常量 ∪ 环境变量覆盖。

    与 ``get_*_hosts()`` 的关键区别：这里**不做 status / 延迟过滤**。
    测速必须能看到全部候选，否则"上次慢、这次快"的节点永远捞不回来，
    形成自指循环（只重测已在池里的少数节点，其余记录一直过期）。
    """
    out: dict[str, list[str]] = {"standard": [], "mac": [], "ex": []}
    for s in _read_servers_json().get("servers", []):
        ip = s.get("ip")
        for protocol in s.get("protocols", []):
            if protocol in out:
                out[protocol].append(ip)
    out["standard"] += STANDARD_HOSTS
    out["mac"] += MAC_HOSTS
    out["ex"] += EX_HOSTS
    for protocol, var in (("standard", "PYPYTDX_DATA_STANDARD_HOSTS"),
                          ("mac", "PYPYTDX_DATA_MAC_HOSTS"),
                          ("ex", "PYPYTDX_DATA_EX_HOSTS")):
        env = os.environ.get(var)
        if env:
            out[protocol] += env.split(",")
    return {
        protocol: [h for h in dict.fromkeys(hosts) if is_valid_host(h)]
        for protocol, hosts in out.items()
    }


# ---------- 对外取主机 ----------
def get_standard_hosts() -> list[str]:
    env = os.environ.get("PYPYTDX_DATA_STANDARD_HOSTS")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    cached = _load_cached_hosts("standard")
    if cached:
        return cached
    fast = _load_ok_hosts("standard", fast_only=True)
    if fast:
        return fast
    from_json = _load_ok_hosts("standard")
    return from_json or list(STANDARD_HOSTS)


def get_mac_hosts() -> list[str]:
    env = os.environ.get("PYPYTDX_DATA_MAC_HOSTS")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    cached = _load_cached_hosts("mac")
    if cached:
        return cached
    fast = _load_ok_hosts("mac", fast_only=True)
    if fast:
        return fast
    from_json = _load_ok_hosts("mac")
    return from_json or list(MAC_HOSTS)


def get_ex_hosts() -> list[str]:
    env = os.environ.get("PYPYTDX_DATA_EX_HOSTS")
    if env:
        return [h.strip() for h in env.split(",") if h.strip()]
    cached = _load_cached_hosts("ex")
    if cached:
        return cached
    from_json = _load_ok_hosts("ex")
    return from_json or list(EX_HOSTS)


def get_ex_handshakes() -> dict[str, str]:
    """EX服务器握手模式（json优先，代码兜底）。"""
    from_json = _load_ex_handshakes()
    return from_json or dict(EX_HANDSHAKES)
