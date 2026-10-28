"""pytdx EX扩展行情协议（setup握手 + 0x23xx命令，服务116.205.143.214等扩展市场服务器）"""
from __future__ import annotations

import struct

from ..._binary import unpack_from
from ...models import ExMarket
from ..datetime_ import get_datetime
from .base import BaseCommand

# 0x23FF 的「槽位 5 是成交额」的市场集合（其余市场该槽位是持仓量、槽位 7 是结算价）。
# 实测依据（2026-10-28，日线，逐条校验 成交额/(close×vol)）：
#   31 港股主板 = 100.0（vol 单位手，1手=100股）、49 港股基金 = 100.3、
#   48 港股创业板 ≈1.0、44 新三板 ≈0.99、74 美股 ≈1.0（vol 单位股）。
# ⚠️ 27 港股指数 / 62 中证指数 该槽位既非成交额也非结算价（量级对不上），故**不在**表内，
#    保持 amount=0.0 的原始行为，避免给出可疑数值。
_EX_AMOUNT_MARKETS: frozenset[int] = frozenset({
    int(ExMarket.HK_MAIN_BOARD),
    int(ExMarket.HK_GEM),
    int(ExMarket.HK_FUND),
    int(ExMarket.NEEQ),
    int(ExMarket.US_STOCK),
})

# pytdx EX setup 握手命令（完整84B: 12B帧头+72B body, 0x2454）—— 与MAC EX login同ID不同body
EX_SETUP_CMD = bytes.fromhex("0101486500015200520054241f32c6e5d53dfb411f32c6e5d53dfb411f32c6e5d53dfb411f32c6e5d53dfb411f32c6e5d53dfb411f32c6e5d53dfb411f32c6e5d53dfb411f32c6e5d53dfb41cce16dffd5ba3fb8cbc57a054f7748ea")


def _ex_frame(cmd_id: int, body: bytes) -> bytes:
    """EX类命令帧：01 + 4B customize + 01 + zipsize + unzipsize + cmd(LE)。"""
    inner_len = 2 + len(body)
    return struct.pack("<BIBHHH", 0x01, 0, 1, inner_len, inner_len, cmd_id) + body


class ExSetupCmd(BaseCommand[bool]):
    """pytdx EX 握手（完整84B原样发送, 0x2454）。"""

    def build_request(self) -> bytes:
        return EX_SETUP_CMD

    def parse_response(self, body: bytes) -> bool:
        return len(body) >= 2


class GetMarketsCmd(BaseCommand[list[dict]]):
    """EX市场列表（0x23F4，请求无body）。"""

    def build_request(self) -> bytes:
        return _ex_frame(0x23F4, b"")

    def parse_response(self, body: bytes) -> list[dict]:
        if len(body) < 2:
            return []
        (cnt,) = struct.unpack("<H", body[:2])
        items: list[dict] = []
        off = 2
        for _ in range(cnt):
            if off + 64 > len(body):
                break
            category, name_b, market, short_b, _, _ = struct.unpack(
                "<B32sB2s26s2s", body[off:off + 64])
            off += 64
            if category == 0 and market == 0:
                continue
            items.append({
                "market": market,
                "category": category,
                "name": name_b.rstrip(b"\x00").decode("gbk", errors="replace"),
                "short_name": short_b.rstrip(b"\x00").decode("gbk", errors="replace"),
            })
        return items


class GetExInstrumentBarsCmd(BaseCommand[list[dict]]):
    """EX商品K线（0x23FF, 32B/条: 4B时间戳+28B行情）。

    后 28B 的字段语义**按市场分两类**（2026-10-28 全市场实测，见 ``_EX_AMOUNT_MARKETS``）:

    * 权益类（港股主板/创业板/基金、新三板、美股）::

          open(4) high(4) low(4) close(4) 成交额(4f) 成交量(4I) 保留(4f)

      成交额可用；校验 ``成交额 / (close × 成交量)`` = 100.0（港股主板/基金，
      ``vol`` 单位是「手」=100 股）或 ≈1.0（港股创业板/新三板/美股，``vol`` 单位是股）。

    * 期货期权类（中金所/上期所/郑商所/大商所及各期权）::

          open(4) high(4) low(4) close(4) 持仓量(4I) 成交量(4I) 结算价(4f)

      **协议帧内没有成交额**（``amount`` 恒 0.0，不是解析漏读）；末位 float 是结算价，
      日线有值、分钟线为 0（``settle`` 归一化成 ``None``）。
    """

    def __init__(self, market: int, code: str, category: int = 0,
                 start: int = 0, count: int = 700) -> None:
        self.market = market
        self.code = code
        self.category = category
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        body = struct.pack("<B9sHHIH", self.market, self.code.encode("utf-8"),
                           self.category, 1, self.start, self.count)
        return _ex_frame(0x23FF, body)

    def parse_response(self, body: bytes) -> list[dict]:
        if len(body) < 20:
            return []
        (ret_count,) = unpack_from("<H", body, 18, "ex bars count")
        amount_market = self.market in _EX_AMOUNT_MARKETS
        items: list[dict] = []
        off = 20
        for _ in range(ret_count):
            if off + 32 > len(body):
                break
            # 时间4字节按category分格式：分钟级=2B zipday+2B tminutes；日线及以上=YYYYMMDD
            year, month, day, hour, minute, _ = get_datetime(self.category, body, off)
            raw = body[off + 4: off + 32]
            open_, high, low, close = struct.unpack("<ffff", raw[:16])
            item = {
                "year": year, "month": month, "day": day,
                "hour": hour, "minute": minute,
                "open": open_, "high": high, "low": low, "close": close,
            }
            if amount_market:
                # 权益类：槽位 5 = 成交额(float)，槽位 7 语义未知（保留不用）
                amount, trade, _reserved = struct.unpack("<fIf", raw[16:28])
                item.update(amount=amount, trade=trade, position=0, settle=None)
            else:
                # 期货/期权：槽位 5 = 持仓量，槽位 7 = 结算价（分钟线为 0 → None）
                position, trade, settle = struct.unpack("<IIf", raw[16:28])
                item.update(amount=0.0, trade=trade, position=position,
                            settle=settle or None)
            items.append(item)
            off += 32
        return items


class GetExInstrumentQuoteCmd(BaseCommand[dict | None]):
    """EX商品报价（0x23FA, 150B行情）。"""

    def __init__(self, market: int, code: str) -> None:
        self.market = market
        self.code = code

    def build_request(self) -> bytes:
        return _ex_frame(0x23FA, struct.pack("<B9s", self.market, self.code.encode("utf-8")))

    def parse_response(self, body: bytes) -> dict | None:
        if len(body) < 150:
            return None
        # <B9s + 4B未知 + 136B: pre_close/open/high/low/price(5f) + 9I(kaicang,_unk1,zongliang,xianliang,...) + 五档
        off = 14
        fields = struct.unpack("<5f", body[off:off + 20])
        off += 20
        ints = struct.unpack("<9I", body[off:off + 36])
        off += 36
        zongliang, xianliang = ints[2], ints[3]
        price = fields[4]
        return {
            "market": self.market,
            "code": self.code,
            "pre_close": fields[0], "open": fields[1],
            "high": fields[2], "low": fields[3], "price": price,
            "zongliang": zongliang, "xianliang": xianliang,
        }
