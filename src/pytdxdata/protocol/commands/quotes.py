"""五档报价命令（标准通道 0x010C/method 0x05053E，最多80只/次）"""
from __future__ import annotations

import struct

from ..._binary import unpack_from
from ...models import SecurityQuote
from ...symbols import is_cn_index
from ..price import get_price
from ..volume import get_volume
from .base import BaseCommand

MAX_QUOTES = 80


# ====================================================================== #
# 权威小数位表（由证券清单 SecurityInfo.decimal_point 灌入）
# ====================================================================== #
# 查找顺序：**权威表 → 静态前缀规则**。权威表来自 ``get_universe`` 返回的
# ``SecurityInfo.decimal_point``（协议自带，见 protocol/commands/security_list.py），
# 由 ``pytdxdata.decimal_sync`` 在后台每日同步一次；未同步时静默退回静态规则，
# 行为与 0.6.5 完全一致。
_DECIMAL_PREFIX: dict[tuple[int, str], int] = {}
_DECIMAL_CODE: dict[tuple[int, str], int] = {}
_DECIMAL_MIXED: set[tuple[int, str]] = set()
_DECIMAL_SOURCE: str = ""


def install_decimal_map(
    items: "list[tuple[int, str, int]] | tuple[tuple[int, str, int], ...]",
    *,
    source: str = "",
) -> dict[str, int]:
    """用证券清单灌入权威小数位表。

    ``items`` 为 ``(market, code, decimal_point)`` 序列（直接来自
    ``SecurityInfo``）。按**三位代码前缀**归并成一个极小的表（全量 5 万只标的
    只产生数百个前缀）；若某个前缀出现**不同**小数位，则该前缀退化为逐代码查表，
    只把这些例外代码存进 ``_DECIMAL_CODE``——这样即使将来交易所新增代码段，
    也不会因为前缀规则过时而算错价格。

    返回统计：``{"prefixes", "codes", "mixed_prefixes", "stock"}``。
    """
    global _DECIMAL_SOURCE
    votes: dict[tuple[int, str], dict[int, int]] = {}
    per_code: dict[tuple[int, str], int] = {}
    for market, code, dp in items:
        c = str(code or "").strip().rstrip("\x00")
        if len(c) < 3:
            continue
        m = int(market)
        if m not in (0, 1):
            continue
        d = int(dp)
        if d <= 0:
            continue
        per_code[(m, c)] = d
        key = (m, c[:3])
        bucket = votes.setdefault(key, {})
        bucket[d] = bucket.get(d, 0) + 1

    _DECIMAL_PREFIX.clear()
    _DECIMAL_CODE.clear()
    _DECIMAL_MIXED.clear()
    for key, bucket in votes.items():
        if len(bucket) == 1:
            _DECIMAL_PREFIX[key] = next(iter(bucket))
        else:
            _DECIMAL_MIXED.add(key)
    for (m, c), d in per_code.items():
        if (m, c[:3]) in _DECIMAL_MIXED:
            _DECIMAL_CODE[(m, c)] = d
    _DECIMAL_SOURCE = source or "universe"
    return decimal_map_stats()


def clear_decimal_map() -> None:
    """清空权威表（回到纯静态前缀规则；测试与排障用）。"""
    global _DECIMAL_SOURCE
    _DECIMAL_PREFIX.clear()
    _DECIMAL_CODE.clear()
    _DECIMAL_MIXED.clear()
    _DECIMAL_SOURCE = ""


def decimal_map_stats() -> dict[str, int]:
    """权威表状态：已覆盖前缀数 / 例外代码数 / 混合前缀数 / 总条目数。"""
    return {
        "prefixes": len(_DECIMAL_PREFIX),
        "codes": len(_DECIMAL_CODE),
        "mixed_prefixes": len(_DECIMAL_MIXED),
        "stock": len(_DECIMAL_PREFIX) + len(_DECIMAL_CODE),
    }


def decimal_map_source() -> str:
    """权威表来源标记（``universe`` / ``cache`` / ``""``=未加载）。"""
    return _DECIMAL_SOURCE


def _decimal_digits_static(market: int, code: str) -> int:
    """静态前缀规则（**兜底**，权威表缺失时使用）。"""
    if market == 1:  # 沪市
        if code.startswith("5"):
            return 3                     # 基金/ETF/LOF（50/51/52/56/58 系）
        if code.startswith("900"):
            return 3                     # B股
        if code.startswith(("000", "36", "88", "99", "7", "6")):
            return 2                     # 指数/板块/优先股/配售/股票(60/601/603/605/688)
        return 4                         # 债券(01x/1xx/2xx)、回购(204xxx)、ABS
    if market == 0:  # 深市
        if code.startswith(("080", "158", "159", "160", "161", "162", "163",
                            "164", "165", "166", "167", "168", "169",
                            "180", "181")):
            return 3                     # 基金/ETF/LOF/配债
        if code.startswith(("000", "001", "002", "003", "070", "140",
                            "200", "201", "300", "301", "302", "395", "399")):
            return 2                     # 股票/指数/B股/优先股
        return 4                         # 债券(10x-14x/19x)、回购(131xxx)、ABS(50x/52x/56x)
    return 2


def price_decimal_digits(market: int, code: str) -> int:
    """报价小数位：**协议原始整数除以 ``10**该值``** 才是真实价格。

    查找顺序：

    1. **权威表**（:func:`install_decimal_map` 灌入）—— 来自证券清单里协议自带的
       ``SecurityInfo.decimal_point``，由 ``pytdxdata.decimal_sync`` 每日后台同步一次。
    2. **静态前缀规则**（:func:`_decimal_digits_static`）—— 权威表未就绪时兜底。
       四档：股票/指数/板块 ``2``、基金/ETF/B股 ``3``、债券/回购 ``4``。

    为什么还要静态规则：``get_quotes`` 解析是**纯同步、无网络**的路径，解析那一刻
    可能还没同步过证券清单；而现网首次安装/离线环境也必须能算出价格。

    权威表与静态规则的一致性：2026-10-22 拉取全量 51,982 只（沪 27,520 + 深 24,462）
    逐条比对，静态规则与 ``decimal_point`` **一一吻合、零例外**——因为全量清单里
    没有任何一个三位前缀出现混合小数位。因此两者并存不会产生口径冲突。

    历史修正（两次都是同一类错误：把某段前缀想当然地归错档）：

    - **2026-10-21**：沪市 ``000``/``8`` 被判成 3 位 → 上证指数/上证50/科创50/
      板块指数**价格少 10 倍**（3713 读成 371）。它们实际是 2 位。
    - **2026-10-22**：债券与回购全线偏差 —— 沪市 ``010/018/019/020/1xx/2xx``
      与深市 ``10x/11x/12x/13x/14x/19x/50x/52x/56x`` 实际是 **4 位**，原先
      沪市落到 2 位（**少 100 倍**）、深市落到 3 位（**少 10 倍**）。实测例：
      ``sh019742`` 110.228 读成 11022.8、``sh204001`` 1.385 读成 138.5、
      ``sz131810`` 1.38 读成 13.8。同时沪市 B 股 ``900xxx`` 是 3 位（原先 2 位）。

    实证方法：拿 ``pre_close``（快照口径）除以上一交易日**日线收盘**（K线口径，
    走 ``/1000`` 独立路径），比值即标度误差 —— 1.0 正确、10 少 10 倍、100 少 100 倍。
    """
    code = (code or "").strip().rstrip("\x00")
    m = int(market)
    if m in (0, 1):
        hit = _DECIMAL_PREFIX.get((m, code[:3]))
        if hit is not None:
            return hit
        hit = _DECIMAL_CODE.get((m, code))
        if hit is not None:
            return hit
    return _decimal_digits_static(m, code)


_SERVER_TIME_SKEW = 40
"""服务端在「50~59 秒」段会写成「90~99」秒，整数值固定多 40×10**d。"""


def format_server_time(raw: int, digits: int = 2) -> str:
    """报价快照时间解码：十进制数字拼接 ``HH MM SS`` + ``digits`` 位小数。

    编码（2026-10-30 盘中实测确定）::

        整数 = HH*10**(4+d) + MM*10**(2+d) + SS*10**d + 小数
        d    = 该品种**价格小数位**（股票 2 / ETF·LOF·B股 3 / 国债·回购 4）

    实测样例：``13224002``（d=2）→ ``13:22:40.02``；``132245903``（d=3）→
    ``13:22:45.903``；``1310228050``（d=4）→ ``13:10:22.8050``。

    历史错误（0.8.0 修正）：此前按「小时 + 百万分之一小时」解码 —— 用
    ``divmod(raw, 1_000_000)`` 取小时、余数按每秒 3600/1e6 折算。股票会把
    11:29:59 解成 **11:17:57**（基准错了、速率也只有真实时钟的 53%），
    ETF / 债券更会解出 ``113:00:21.222`` 这种小时 > 23 的非法值。

    ⚠️ 服务端怪癖：**SS 落在 50~59 秒时写成 90~99**（编码值 +40 秒）。实测
    3 分钟连续采样，未修正时该区间的滞后会从稳定的 10.6 秒突然变成 -29.4 秒
    （正好差 40）。因此这里对 ``SS >= 60`` 一律减 40 秒还原。

    ⚠️ 指数（``sh000001`` / ``sz399001`` …）该字段**不是**这只时钟：解出来
    MM 常 > 59、且不同指数互相矛盾（同一时刻 000001 是 12:89、399001 是 12:15），
    协议里没有给出可用信息。这类返回值一律为空串，不要编造时间；要指数时间
    请用 :meth:`~pytdxdata.api.TdxData.get_minutes` 或 K 线。
    """
    if raw <= 0:
        return ""
    d = digits if 0 <= digits <= 6 else 2
    scale = 10 ** d
    frac = raw % scale
    rest = raw // scale
    seconds = rest % 100
    minutes = (rest // 100) % 100
    hours = rest // 10000
    if seconds >= 60:
        seconds -= _SERVER_TIME_SKEW
    if hours > 23 or minutes > 59 or not 0 <= seconds <= 59:
        return ""
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{frac:0{d}d}"


class GetSecurityQuotesCmd(BaseCommand[list[SecurityQuote]]):
    """批量获取实时五档行情（最多80只/次）。"""

    def __init__(self, stocks: list[tuple[int, str]]) -> None:
        if not stocks:
            raise ValueError("stocks 不能为空")
        if len(stocks) > MAX_QUOTES:
            raise ValueError(f"单次最多查询{MAX_QUOTES}只")
        self.stocks = stocks

    def build_request(self) -> bytes:
        n = len(self.stocks)
        payload_len = n * 7 + 12
        header = struct.pack(
            "<HIHHIIHH", 0x010C, 0x02006320, payload_len, payload_len, 0x0005053E, 0, 0, n
        )
        body = bytearray(header)
        for market, code in self.stocks:
            body.extend(struct.pack("<B6s", int(market), code.encode("utf-8")))
        return bytes(body)

    def parse_response(self, body: bytes) -> list[SecurityQuote]:
        pos = 2  # 跳过2字节魔数
        (num,) = unpack_from("<H", body, pos, "quotes header")
        pos += 2
        results: list[SecurityQuote] = []

        for _ in range(num):
            market_b, code_b, _active1 = unpack_from("<B6sH", body, pos, "quote record header")
            pos += 9

            price_raw, pos = get_price(body, pos)
            last_close_diff, pos = get_price(body, pos)
            open_diff, pos = get_price(body, pos)
            high_diff, pos = get_price(body, pos)
            low_diff, pos = get_price(body, pos)
            unknown_0, pos = get_price(body, pos)   # server_time原始
            _unknown_1, pos = get_price(body, pos)
            vol, pos = get_price(body, pos)
            cur_vol, pos = get_price(body, pos)
            amount, _pos2 = get_volume(body, pos)
            pos = _pos2  # get_volume 已推进4字节
            s_vol, pos = get_price(body, pos)
            b_vol, pos = get_price(body, pos)
            _unknown_2, pos = get_price(body, pos)
            _unknown_3, pos = get_price(body, pos)

            bid: list[tuple[float, float]] = []
            ask: list[tuple[float, float]] = []
            for _d in range(5):
                bid_d, pos = get_price(body, pos)
                ask_d, pos = get_price(body, pos)
                bv, pos = get_price(body, pos)
                av, pos = get_price(body, pos)
                bid.append((bid_d, float(bv)))
                ask.append((ask_d, float(av)))

            (trading_status,) = unpack_from("<H", body, pos, "quote tail")
            pos += 2
            for _d in range(4):
                _, pos = get_price(body, pos)
            _rise_speed, _active2 = unpack_from("<hH", body, pos, "quote tail2")
            pos += 4

            code = code_b.decode("utf-8").rstrip("\x00")
            dp = price_decimal_digits(market_b, code)
            divisor = 10 ** dp
            p = price_raw / divisor
            results.append(SecurityQuote(
                market=market_b, code=code, price=p,
                cur_vol=float(cur_vol),
                rise_speed=_rise_speed / 100.0,
                decimal_point=dp,
                open_amount=float(_unknown_3),
                pre_close=(price_raw + last_close_diff) / divisor,
                open=(price_raw + open_diff) / divisor,
                high=(price_raw + high_diff) / divisor,
                low=(price_raw + low_diff) / divisor,
                vol=float(vol), amount=amount, s_vol=float(s_vol), b_vol=float(b_vol),
                bid=[((price_raw + d) / divisor, v) for d, v in bid],
                ask=[((price_raw + d) / divisor, v) for d, v in ask],
                server_time="" if is_cn_index(market_b, code)
                else format_server_time(unknown_0, dp),
                trading_status=trading_status,
            ))
        return results
