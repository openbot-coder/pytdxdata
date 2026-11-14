"""EX 期货/期权合约乘数表（估算成交额用）。

0x23FF 帧内没有成交额槽位（见 ex_proto.py 注释）。要给期货/期权 K 线一个
可用的 ``amount``，只能客户端自算。本表覆盖主流境内品种，键为**品种代码
小写字母前缀**（从合约代码提取：``RB2611`` → ``rb``，``IFL0`` → ``if``）。

⚠️ 口径：``amount = vol × close × multiplier`` —— 按**收盘价**估算，
不是 Σ(逐笔成交价×手数×乘数) 的精确值，日线级别误差通常 <2%（收盘价
成交集中假设）。对多数策略足够；要精确成交额请用逐笔聚合。
"""
from __future__ import annotations

# 中金所（乘数：点×元）
_CFFEX = {
    "if": 200, "ih": 300, "ic": 200, "im": 200,
    "ifl0": 200, "ihl0": 300, "icl0": 200, "iml0": 200,
    "tf": 10_000, "t": 10_000, "ts": 10_000, "tl": 10_000, "tp": 10_000,
    "mo": 50, "ho": 50, "io": 100,
}
# 上期所
_SHF = {
    "rb": 10, "hc": 10, "wr": 10, "cu": 5, "al": 5, "zn": 5, "pb": 5,
    "ni": 1, "sn": 1, "ss": 5, "ao": 20, "au": 100, "ag": 15,
    "fu": 10, "bu": 10, "ru": 10, "br": 5, "sp": 10, "lu": 10, "nr": 10,
    "sc": 1000, "bc": 5, "ec": 50, "sh": 1_000_000, "dii": 100,
}
# 大商所
_DL = {
    "a": 10, "b": 10, "m": 10, "y": 10, "p": 10, "c": 10, "cs": 20,
    "i": 100, "j": 100, "jm": 60, "jd": 5, "fb": 10, "bb": 500,
    "l": 5, "v": 5, "pp": 5, "eg": 10, "eb": 5, "pg": 20, "rr": 10,
    "lh": 16, "px": 10, "lg": 50,
}
# 郑商所
_ZZ = {
    "sr": 10, "cf": 5, "cy": 5, "ap": 10, "cj": 5, "pj": 10,
    "ta": 5, "ma": 10, "fg": 20, "sa": 20, "ur": 20, "sf": 5, "sm": 5,
    "oi": 10, "rm": 10, "zc": 100, "wh": 20, "pm": 50, "ri": 20, "jr": 20,
    "lr": 20, "pf": 5, "sh": 30, "pk": 5, "cft": 5,
}
# 广期所
_GZ = {"si": 5, "lc": 20, "ps": 5}

_MARKET_TABLES = {
    "cffex": _CFFEX,
    "shf": _SHF,
    "dl": _DL,
    "zz": _ZZ,
    "gz": _GZ,
}

# 期权乘数：金融期权按标的；商品期权 = 标的期货乘数（同一品种字母前缀命中即可）。
# 沪深股票期权（50ETF/300ETF/500ETF/科创50ETF/创业板ETF）乘数 10000。
_STOCK_OPTION_MULTIPLIER = 10_000


def product_code(code: str) -> str:
    """合约代码 -> 品种小写前缀（``RB2611``→``rb``、``IFL0``→``ifl0``、``m2501``→``m``）。

    规则：取前缀直到首个数字；连续/主力后缀（L0、M0 等）保留在品种键里，
    表中给主力键与普通键都录入（见 _CFFEX 的 IFL0/IF 双键）。
    """
    s = code.strip().lower()
    out = []
    for ch in s:
        if ch.isdigit():
            break
        out.append(ch)
    return "".join(out)


def find_multiplier(ex_prefix: str, code: str, market: int | None = None) -> float | None:
    """(EX 市场短前缀, 合约代码[, 市场号]) -> 合约乘数；未知返回 None。

    期权市场按**市场号**判断（IntEnum 名含 OPTION）：股票期权乘数 10000，
    商品期权（ZZ/DL/CFFEX 期权市场号 4/5/7）回落到对应商品品种表。
    """
    if market is not None:
        name = _OPTION_MARKET_NAMES.get(int(market))
        if name == "stock":
            return _STOCK_OPTION_MULTIPLIER
        if name == "commodity":
            ex_prefix = {"ZZ_FUTURES_OPTION": "zz", "DL_FUTURES_OPTION": "dl",
                         "CFFEX_OPTION": "cffex", "GZ_OPTIONS": "gz"}.get(name, ex_prefix)
    s = ex_prefix.lower()
    if s.endswith("_option") or s in ("opt", "option"):
        return _STOCK_OPTION_MULTIPLIER
    table = _MARKET_TABLES.get(s)
    if table is None:
        return None
    prod = product_code(code)
    if prod in table:
        return float(table[prod])
    # 主力/连续后缀剥离后再试（IFL0 → IF）
    stripped = prod.rstrip("lmo0")
    return table.get(stripped)


_OPTION_MARKET_NAMES = {
    4: "commodity",    # ZZ_FUTURES_OPTION
    5: "commodity",    # DL_FUTURES_OPTION
    7: "commodity",    # CFFEX_OPTION
    67: "commodity",   # GZ_OPTIONS
    8: "stock",        # SH_STOCK_OPTION
    9: "stock",        # SZ_STOCK_OPTION
}
