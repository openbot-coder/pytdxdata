"""标准通道扩展命令：公司信息(F10)/板块文件/市场统计/涨跌停价"""
from __future__ import annotations

import struct

from ..._binary import slice_bytes, unpack_from
from ...models import Market
from .base import BaseCommand

_FMT_HDR = "<HIHHHH"


def _std_req(head_hex: str, payload: bytes) -> bytes:
    return bytes.fromhex(head_hex) + payload


# ------------------------------------------------------------------ #
# 公司信息 F10 0x109B / 0x109C
# ------------------------------------------------------------------ #

class CompanyInfoCategory:
    """F10目录条目"""
    __slots__ = ("name", "filename", "start", "length")

    def __init__(self, name: str, filename: str, start: int, length: int) -> None:
        self.name = name
        self.filename = filename
        self.start = start
        self.length = length


class GetCompanyInfoCategoryCmd(BaseCommand[list[CompanyInfoCategory]]):
    def __init__(self, market: Market, code: str) -> None:
        self.market = market
        self.code = code

    def build_request(self) -> bytes:
        return _std_req("0c0f109b00010e000e00cf02",
                        struct.pack("<H6sI", int(self.market), self.code.encode("utf-8"), 0))

    def parse_response(self, body: bytes) -> list[CompanyInfoCategory]:
        (num,) = unpack_from("<H", body, 0, "f10 category count")
        out: list[CompanyInfoCategory] = []
        for i in range(num):
            off = 2 + i * 152
            name_b, filename_b, start, length = struct.unpack("<64s80sII", slice_bytes(body, off, 152, "f10 cat"))
            out.append(CompanyInfoCategory(
                name=name_b.decode("gbk", errors="replace").rstrip("\x00"),
                filename=filename_b.decode("gbk", errors="replace").rstrip("\x00"),
                start=start, length=length,
            ))
        return out


class GetCompanyInfoContentCmd(BaseCommand[str]):
    def __init__(self, market: Market, code: str, filename: str,
                 offset: int = 0, length: int = 65535) -> None:
        self.market = market
        self.code = code
        self.filename = filename
        self.offset = offset
        self.length = length

    def build_request(self) -> bytes:
        fn = self.filename.encode("gbk")[:80].ljust(80, b"\x00")
        return _std_req("0c07109c000168006800d002",
                        struct.pack("<H6sH80sIII", int(self.market),
                                    self.code.encode("utf-8"), 0, fn,
                                    self.offset, self.length, 0))

    def parse_response(self, body: bytes) -> str:
        # 12B头(10s+H len) + len字节GBK文本
        (length,) = unpack_from("<H", body, 10, "f10 content len")
        text = body[12:12 + length].decode("gbk", errors="replace")
        return text


# ------------------------------------------------------------------ #
# 板块文件 0x1869 / 0x186A
# ------------------------------------------------------------------ #

class BlockInfo:
    __slots__ = ("size", "md5", "filename")

    def __init__(self, size: int, md5: str, filename: str) -> None:
        self.size = size
        self.md5 = md5
        self.filename = filename


class GetBlockInfoMetaCmd(BaseCommand[BlockInfo]):
    def __init__(self, filename: str) -> None:
        self.filename = filename

    def build_request(self) -> bytes:
        fn = self.filename.encode("ascii", errors="replace")[:40].ljust(40, b"\x00")
        return _std_req("0c39186900012a002a00c502", fn)

    def parse_response(self, body: bytes) -> BlockInfo:
        size, _v1, md5_b, _v2 = struct.unpack("<I1s32s1s", body[:38])
        return BlockInfo(size=size, md5=md5_b.decode("ascii", errors="replace"),
                         filename=self.filename)


class GetBlockInfoCmd(BaseCommand[bytes]):
    """下载板块文件内容（分块）。"""

    def __init__(self, filename: str, start: int = 0, length: int = 0) -> None:
        self.filename = filename
        self.start = start
        self.length = length

    def build_request(self) -> bytes:
        fn = self.filename.encode("ascii", errors="replace")[:100].ljust(100, b"\x00")
        return _std_req("0c37186a00016e006e00b906",
                        struct.pack("<II", self.start, self.length) + fn)

    def parse_response(self, body: bytes) -> bytes:
        return body[4:]  # 跳过前4字节返回原始二进制


# ------------------------------------------------------------------ #
# 涨跌停价（本地计算，无网络命令）
# ------------------------------------------------------------------ #

def compute_price_limits(market: Market, code: str, name: str,
                         pre_close: float, listed_days: int = 9999) -> tuple[float | None, float | None]:
    """计算涨跌停价（参照 easy-tdx codec/price_rules）。指数/基金无涨跌停。"""
    code = (code or "").strip()
    if pre_close is None or pre_close <= 0:
        return None, None
    # 指数类无涨跌停
    if market == Market.SH and (code.startswith(("000", "880", "999")) or "指数" in (name or "")):
        return None, None
    if market == Market.SZ and (code.startswith(("395", "399")) or "指数" in (name or "")):
        return None, None
    # 涨跌幅
    if market == Market.BJ:
        pct = 0.30
    elif code.startswith(("688", "300", "301")):
        pct = 0.20
    elif "ST" in (name or "").upper():
        pct = 0.05
    else:
        pct = 0.10
    # 上市初期不设限（沪/深前5日, 北交首日）
    if listed_days <= 5 and market != Market.BJ:
        return None, None
    if listed_days <= 0:
        return None, None
    up = round(pre_close * (1 + pct) + 0.00001, 2)
    down = round(pre_close * (1 - pct) + 0.00001, 2)
    return up, down


# ------------------------------------------------------------------ #
# 市场统计（用报价接口查上证指数880005/880001/880006组合，由api层实现）
# ------------------------------------------------------------------ #

class MarketStat:
    __slots__ = ("up_count", "down_count", "flat_count", "total_count",
                 "total_amount", "total_vol", "total_market_cap",
                 "limit_up_count", "limit_down_count")

    def __init__(self) -> None:
        self.up_count = 0
        self.down_count = 0
        self.flat_count = 0
        self.total_count = 0
        self.total_amount = 0.0
        self.total_vol = 0.0
        self.total_market_cap = 0.0
        self.limit_up_count = 0
        self.limit_down_count = 0


# ------------------------------------------------------------------ #
# 报告/财务文件 0x06B9（分块下载，get_report_file/get_financial_file 共用）
# ------------------------------------------------------------------ #

class GetReportFileCmd(BaseCommand[bytes]):
    """下载远程报告/财务文件分块（tdxfin/gpcw.txt、gpcw*.zip 等）。"""

    def __init__(self, filename: str, start: int = 0, length: int = 30000) -> None:
        self.filename = filename
        self.start = start
        self.length = length

    def build_request(self) -> bytes:
        fn = self.filename.encode("ascii", errors="replace")[:100].ljust(100, b"\x00")
        return _std_req("0c37186a00016e006e00b906",
                        struct.pack("<II", self.start, self.length) + fn)

    def parse_response(self, body: bytes) -> bytes:
        return body[4:]  # 跳过前4字节返回原始文件块
