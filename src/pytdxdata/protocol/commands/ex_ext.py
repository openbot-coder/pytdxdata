"""EX扩展市场命令：商品列表/数量/逐笔（走EX服务器7727，12B EX帧头）"""
from __future__ import annotations

import struct
from typing import Any

from ..._binary import slice_bytes
from .base import BaseCommand
from ..._binary import unpack_from


def _ex_frame(cmd_id: int, body: bytes) -> bytes:
    """EX类命令帧：01 + 4B customize + 01 + zipsize + unzipsize + cmd(LE)。"""
    inner_len = 2 + len(body)
    return struct.pack("<BIBHHH", 0x01, 0, 1, inner_len, inner_len, cmd_id) + body


class GetExInstrumentInfoCmd(BaseCommand[list[dict]]):
    """扩展市场商品列表（0x23F5，EX帧，64B/条）。"""

    def __init__(self, market: int, start: int = 0, count: int = 100) -> None:
        self.market = market
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        return _ex_frame(0x23F5, struct.pack("<IH", self.start, self.count))

    def parse_response(self, body: bytes) -> list[dict]:
        if len(body) < 6:
            return []
        n = len(body) - 6
        items: list[dict] = []
        off = 6
        for _ in range(n // 64):
            if off + 64 > len(body):
                break
            rec = body[off:off + 64]
            # 前40B: BB3s9s17s9s (category/market/3B/code9/name17/desc9)
            _, mkt, _, code_b, name_b, desc_b = struct.unpack("<BB3s9s17s9s", rec[:40])
            items.append({
                "category": 0,
                "market": mkt,
                "code": code_b.rstrip(b"\x00").decode("gbk", errors="replace"),
                "name": name_b.rstrip(b"\x00").decode("gbk", errors="replace"),
                "desc": desc_b.rstrip(b"\x00").decode("gbk", errors="replace"),
            })
            off += 64
        return items


class GetExInstrumentCountCmd(BaseCommand[int]):
    """扩展市场商品数量（0x23F0，EX帧，body空）。"""

    def build_request(self) -> bytes:
        return _ex_frame(0x23F0, b"")

    def parse_response(self, body: bytes) -> int:
        if len(body) < 23:
            return 0
        (count,) = unpack_from("<I", body, 19, "ex instrument count")
        return int(count)


class GetExTransactionDataCmd(BaseCommand[list[dict]]):
    """扩展市场（港股）当日逐笔（0x23FC，EX帧，16B/条）。"""

    def __init__(self, market: int, code: str, start: int = 0, count: int = 1800) -> None:
        self.market = market
        self.code = code
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        body = struct.pack("<B9siH", self.market, self.code.encode("utf-8"), self.start, self.count)
        return _ex_frame(0x23FC, body)

    def parse_response(self, body: bytes) -> list[dict]:
        if len(body) < 16:
            return []
        (_, _, _, num) = struct.unpack("<B9s4sH", body[:16])
        items: list[dict] = []
        off = 16
        for _ in range(num):
            if off + 16 > len(body):
                break
            minutes, price_int, vol, zengcang, direction = struct.unpack(
                "<HIIiH", body[off:off + 16])
            items.append({
                "minutes": minutes,
                "price": price_int / 1000.0,  # 港股精确到0.001
                "vol": vol,
                "zengcang": zengcang,
                "direction": direction,
            })
            off += 16
        return items


class GetExHistoryTransactionDataCmd(BaseCommand[list[dict]]):
    """扩展市场（港股）历史逐笔（0x2406，EX帧）。"""

    def __init__(self, ymd: int, market: int, code: str,
                 start: int = 0, count: int = 1800) -> None:
        self.ymd = ymd
        self.market = market
        self.code = code
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        body = struct.pack("<IB9siH", self.ymd, self.market,
                           self.code.encode("utf-8"), self.start, self.count)
        return _ex_frame(0x2406, body)

    def parse_response(self, body: bytes) -> list[dict]:
        if len(body) < 16:
            return []
        (_, _, _, num) = struct.unpack("<B9s4sH", body[:16])
        items: list[dict] = []
        off = 16
        for _ in range(num):
            if off + 16 > len(body):
                break
            minutes, price_int, vol, zengcang, direction = struct.unpack(
                "<HIIiH", body[off:off + 16])
            items.append({
                "minutes": minutes,
                "price": price_int / 1000.0,
                "vol": vol,
                "zengcang": zengcang,
                "direction": direction,
            })
            off += 16
        return items


class SymbolTickChartCmd(BaseCommand[list[dict]]):
    """分时逐笔tick图（0x122D，MAC帧走EX通道，18B/tick）。"""

    def __init__(self, market: int, code: str, ymd: int = 0) -> None:
        self.market = market
        self.code = code
        self.ymd = ymd

    def build_request(self) -> bytes:
        from .mac_ext import build_mac_request
        raw = self.code.encode("gbk")
        padded = (raw + b"\x00" * 22)[:22]
        body = struct.pack("<H22sI5H", self.market, padded, self.ymd, 1, 0, 0, 0, 0)
        return build_mac_request(0x122D, body)

    def parse_response(self, body: bytes) -> list[dict]:
        if len(body) < 35:
            return []
        (_, _, _, _, ref_price, count) = struct.unpack("<H22sIBfH", body[:35])
        items: list[dict] = []
        off = 35
        for _ in range(count):
            if off + 18 > len(body):
                break
            minutes, price, avg, vol, momentum = struct.unpack("<HffIf", body[off:off + 18])
            items.append({
                "minutes": minutes,
                "price": price,
                "avg": avg,
                "vol": vol,
                "momentum": momentum,
            })
            off += 18
        return items
