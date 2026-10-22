"""标的代码解析：全库唯一的字符串口径。

统一写法（也是 CLI 的写法）::

    sz000001 / sh600000 / bj920992     A股（6位代码，必须带市场前缀）
    hk00700                           港股主板（5位）
    usAAPL                            美股（ticker）
    cffex:IFL0 / zz:AP0               期货（交易所前缀 + 合约，可带冒号）
    000001                            裸 6 位数字 = 沪市指数

**裸 6 位数字只当作沪市指数**：A股代码不唯一（``000001`` 既是上证指数也是平安银行），
要股票请写全前缀 ``sh000001`` / ``sz000001``。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import ExMarket, Market

# 通道：cn=标准/MAC A股通道，ex=扩展市场通道（端口 7727）
CN = "cn"
EX = "ex"
INDEX = "index"

_CN_ALIASES: dict[str, Market] = {
    "sz": Market.SZ,
    "sh": Market.SH,
    "bj": Market.BJ,
}
_CN_PREFIX: dict[int, str] = {int(m): k for k, m in _CN_ALIASES.items()}

# EX 常用别名（短前缀优先；其余成员用枚举名小写，如 hk_gem:08001）
_EX_ALIASES: dict[str, ExMarket] = {
    "hk": ExMarket.HK_MAIN_BOARD,
    "us": ExMarket.US_STOCK,
    "zz": ExMarket.ZZ_FUTURES,
    "dl": ExMarket.DL_FUTURES,
    "shf": ExMarket.SH_FUTURES,
    "cffex": ExMarket.CFFEX_FUTURES,
    "gz": ExMarket.GZ_FUTURES,
}
_EX_PREFIX: dict[int, str] = {int(m): k for k, m in _EX_ALIASES.items()}

ALL = "all"


def _ex_by_name(name: str) -> ExMarket | None:
    """按枚举名匹配（hk_main_board / us_stock / ...），保证 format→parse 可往返。"""
    key = name.upper()
    for member in ExMarket:
        if member.name == key:
            return member
    return None


def _lookup(text: str) -> tuple[int, str] | None:
    """前缀 -> (市场号, 通道)。"""
    if text in _CN_ALIASES:
        return int(_CN_ALIASES[text]), CN
    if text in _EX_ALIASES:
        return int(_EX_ALIASES[text]), EX
    member = _ex_by_name(text)
    if member is not None:
        return int(member), EX
    return None


def _hint() -> str:
    cn = "/".join(sorted(_CN_ALIASES))
    ex = "/".join(sorted(_EX_ALIASES))
    return f"A股: {cn}；扩展市场: {ex}（详见 pytdxdata.models.ExMarket）"


@dataclass(frozen=True, slots=True)
class Symbol:
    """解析后的标的。``kind``: cn=A股 / ex=扩展市场 / index=裸数字指数。"""

    market: int
    code: str
    kind: str
    raw: str = ""

    @property
    def is_ex(self) -> bool:
        return self.kind == EX

    @property
    def is_index(self) -> bool:
        return self.kind == INDEX

    @property
    def channel(self) -> str:
        """路由通道：ex 走扩展市场池，其余走标准/MAC A股池。"""
        return EX if self.is_ex else "std"

    @property
    def text(self) -> str:
        """规范化写法，同时用作记录上的 ``.symbol``。"""
        if self.is_index:
            return self.code
        return format_symbol(self.market, self.code)

    def as_pair(self) -> tuple[int, str]:
        """(市场号, 代码) 元组（协议层用）。"""
        return self.market, self.code

    def __str__(self) -> str:
        return self.text


def _prefix_for(market: int) -> str | None:
    """市场号 -> 短前缀；无短别名的扩展市场回退到枚举名小写。"""
    m = int(market)
    prefix = _CN_PREFIX.get(m) or _EX_PREFIX.get(m)
    if prefix is not None:
        return prefix
    try:
        return ExMarket(m).name.lower()
    except ValueError:
        return None


def format_symbol(market: int, code: str) -> str:
    """(市场号, 代码) -> 规范化字符串（``1, "600000"`` -> ``"sh600000"``）。"""
    code = str(code).strip()
    if not code:
        return ""
    prefix = _prefix_for(market)
    if prefix is None:
        return code
    # 2 字母前缀贴代码（hk00700），长前缀用冒号（cffex:IFL0 / hk_gem:08001）
    return f"{prefix}:{code}" if len(prefix) > 2 else f"{prefix}{code}"


def market_name(market: int) -> str:
    """市场号 -> 市场选择器字符串（``1`` -> ``"sh"``，``31`` -> ``"hk"``）。

    用于把旧接口的市场号参数转成统一接口要的市场选择器。
    """
    prefix = _prefix_for(market)
    if prefix is None:
        raise ValueError(f"未知市场号: {market}")
    return prefix


def parse_symbol(text: str) -> Symbol:
    """字符串 -> Symbol。无法识别时抛 ValueError 并列出可用前缀。"""
    raw = text
    sym = str(text).strip()
    if not sym:
        raise ValueError("标的为空")

    if ":" in sym:
        head, code = sym.split(":", 1)
        head, code = head.strip().lower(), code.strip()
        hit = _lookup(head)
        if hit is None:
            raise ValueError(f"未知市场前缀: {head}（{_hint()}）")
        if not code:
            raise ValueError(f"缺少代码: {raw}")
        return Symbol(market=hit[0], code=code.upper(), kind=hit[1], raw=raw)

    head, code = sym[:2].lower(), sym[2:]
    hit = _lookup(head)
    if hit is not None and code:
        return Symbol(market=hit[0], code=code.upper(), kind=hit[1], raw=raw)

    if sym.isdigit() and len(sym) == 6:
        # 裸 6 位 = 沪市指数（A股代码不唯一，股票必须带前缀）
        return Symbol(market=int(Market.SH), code=sym, kind=INDEX, raw=raw)

    raise ValueError(f"无法识别标的: {raw}（{_hint()}；裸6位数字=沪市指数）")


def parse_symbols(symbols: str | Sequence[str] | None) -> list[Symbol]:
    """接受单个字符串 / 字符串序列 / Symbol 序列，去重并保持输入顺序。"""
    if symbols is None:
        raise ValueError("symbols 不能为空")
    items: list = [symbols] if isinstance(symbols, str) else list(symbols)
    if not items:
        raise ValueError("symbols 不能为空")
    out: list[Symbol] = []
    seen: set[tuple[int, str]] = set()
    for item in items:
        sym = item if isinstance(item, Symbol) else parse_symbol(item)
        if sym.as_pair() in seen:
            continue
        seen.add(sym.as_pair())
        out.append(sym)
    return out


def cn_markets() -> list[int]:
    """A股市场号（深/沪/北）。"""
    return [int(m) for m in Market]


def ex_markets() -> list[int]:
    """扩展市场号，按枚举值升序。"""
    return sorted(int(m) for m in ExMarket)


def market_targets(text: str | None) -> list[tuple[int, str]]:
    """市场选择器 -> [(市场号, 通道)]；``None`` / ``"all"`` = 全部市场。

    市场号在两条通道里会撞车（``Market.SH=1`` 与 ``ExMarket.TEMP_STOCK=1``），
    所以必须带通道一起返回，调用方按通道分别请求。
    """
    key = "" if text is None else str(text).strip().lower()
    if not key or key == ALL:
        return ([(m, CN) for m in cn_markets()]
                + [(m, EX) for m in ex_markets()])
    hit = _lookup(key)
    if hit is None:
        raise ValueError(f"未知市场: {text}（{_hint()}；全部用 all）")
    return [hit]


def parse_market(text: str | None) -> int | None:
    """市场选择器 -> 单个市场号；``None`` / ``"all"`` 返回 None。

    只用于「本来就是单市场」的接口（如市场异动）。需要跨市场时用
    :func:`market_targets`。
    """
    targets = market_targets(text)
    return None if len(targets) > 1 else targets[0][0]


def is_cn_index(market: int, code: str) -> bool:
    """A股指数代码判定：88开头板块指数 / 沪市000 / 深市399。

    深市股票 000001 不是指数，必须带市场判断。
    """
    code = str(code)
    return (
        code.startswith("88")
        or (code.startswith("000") and int(market) == int(Market.SH))
        or (code.startswith("399") and int(market) == int(Market.SZ))
    )


def has_order_count(market: int, code: str) -> bool:
    """该标的的逐笔 ``trade_count`` 是否是**真实成交笔数**。

    通达信对**指数**和**多数场内基金**不提供笔数，但那 4 字节里仍塞了值：

    - 指数：``当时均价 × 1000``（见 :class:`~pytdxdata.models.TransactionRecord`）
    - ETF / LOF：``价格 × 1000``；货币 ETF（如 ``sh511990``）恒为 ``0``

    直接当笔数用会得到 3994 这种荒谬值（实测 ``sh510310``）。

    实测覆盖（2026-10-22，按代码前缀各取样 1~7 只）：

    ==================  ==========================  ==========
    品种                 前缀                         笔数可用
    ==================  ==========================  ==========
    沪股票/科创           600/601/603/605/688/689      是
    沪 LOF               501/502/506                  是
    沪 ETF/LOF           51/52/56/58                  **否**
    沪指数               000/88                       否
    沪可转债              110/111/113/118              是
    沪 EB（可交换债）      132                          是
    沪逆回购              204                          是
    深股票               000/001/002/003/300/301      是
    深 ETF               159                          **否**
    深 LOF/基金          15/16/17/18                  **否**
    深指数               399                          否
    深可转债              123/127/128                  是
    ==================  ==========================  ==========

    ``501x/502x/506x``（沪市 LOF）实测**有**真实笔数，所以不能用「沪市 5 开头
    一律当基金」的粗规则；必须按上面这组前缀精确判定。

    可转债（沪 110/111/113/118/132、深 123/127/128）**走的是兜底分支**——
    它们既不在沪市 ``51/52/56/58`` 里，也不在深市 ``159/15/16/17/18`` 里，
    所以两个分支都落到「是」。这不是巧合而是设计：兜底按「**排除法**」认定
    基金/指数，其余一律当普通品种。实测验证（2026-10-22，16 只当日 + 8 只历史日）：
    笔数合计与日线量级吻合（``sh111000`` 3631 条切片 / 20443 笔，
    ``sz128136`` 1939 条 / 14593 笔），且 ``Σvol`` 与日线 ``vol`` 比值 **1.000000**。
    """
    code = str(code)
    if is_cn_index(market, code):
        return False
    m = int(market)
    if m == int(Market.SH):
        return code[:2] not in ("51", "52", "56", "58")
    if m == int(Market.SZ):
        return not (code[:3] == "159" or code[:2] in ("15", "16", "17", "18"))
    return True
