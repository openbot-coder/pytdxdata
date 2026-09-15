"""MAC 扩展命令：板块/集合竞价/异动/个股快照/扩展市场/服务器信息/资金流"""
from __future__ import annotations

import struct

from ..._binary import slice_bytes, unpack_from
from ...models import (
    AuctionItem, BelongBoard, BoardInfo, CapitalFlow, GoodsItem, MemberQuote,
    ServerInfo, SymbolSnapshot, UnusualItem,
)
from ..mac_frame import build_mac_request
from .base import BaseCommand

# 板块成分股常用字段位（FieldBit: 0x00昨收 0x01开 0x02高 0x03低 0x04收 0x05量 0x06量比 0x07额）
_MEMBER_BASIC_BITS = [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07]


def _build_bitmap(bits: list[int], size: int = 16) -> bytes:
    bm = bytearray(size)
    for b in bits:
        bm[b // 8] |= 1 << (b % 8)
    return bytes(bm)


def _field_fmt(bit: int) -> str:
    """按位返回解析格式（<f浮点 / <I无符号 / <i有符号）。"""
    if bit in (0x05, 0x08, 0x09, 0x0A, 0x0B, 0x13, 0x14, 0x15, 0x18, 0x19, 0x1A):
        return "<I"
    if bit in (0x2E,):
        return "<i"
    return "<f"


# ------------------------------------------------------------------ #
# 板块列表 0x1231
# ------------------------------------------------------------------ #

class MacBoardListCmd(BaseCommand[list[BoardInfo]]):
    def __init__(self, board_type: int = 0, start: int = 0, page_size: int = 150) -> None:
        self.board_type = board_type  # 0=全部 1=行业 2=概念 ...
        self.start = start
        self.page_size = page_size

    def build_request(self) -> bytes:
        body = struct.pack("<HHBBHH8x", self.page_size, self.board_type, 0, 0, self.start, 1)
        return build_mac_request(0x1231, body)

    def parse_response(self, body: bytes) -> list[BoardInfo]:
        results: list[BoardInfo] = []
        pos = 4  # 跳过2B count_all + 2B total
        n = (len(body) - pos) // 160
        for _ in range(n):
            # 每160B记录=板块段(80B) + 领涨股段(80B)，合并为一条
            b_seg = slice_bytes(body, pos, 80, "board segment")
            market, code_b, _pad, name_b, price, rise, pre = struct.unpack("<H6s16s44sfff", b_seg)
            s_seg = slice_bytes(body, pos + 80, 80, "symbol segment")
            s_mkt, s_code, _pad2, s_name, s_price, s_rise, s_pre = struct.unpack("<H6s16s44sfff", s_seg)
            results.append(BoardInfo(
                market=market,
                code=code_b.decode("gbk", errors="replace").rstrip("\x00"),
                name=name_b.decode("gbk", errors="replace").rstrip("\x00"),
                price=price, rise_speed=rise, pre_close=pre,
                symbol_market=s_mkt,
                symbol_code=s_code.decode("gbk", errors="replace").rstrip("\x00"),
                symbol_name=s_name.decode("gbk", errors="replace").rstrip("\x00"),
                symbol_price=s_price, symbol_rise_speed=s_rise, symbol_pre_close=s_pre,
            ))
            pos += 160
        return results


# ------------------------------------------------------------------ #
# 板块成分报价 0x122C
# ------------------------------------------------------------------ #

class MacBoardMembersQuotesCmd(BaseCommand[list[MemberQuote]]):
    def __init__(self, board_code: int, *, sort_type: int = 0, start: int = 0,
                 page_size: int = 80, bits: list[int] | None = None) -> None:
        self.board_code = board_code
        self.sort_type = sort_type
        self.start = start
        self.page_size = page_size
        self.bits = bits if bits is not None else list(_MEMBER_BASIC_BITS)

    def build_request(self) -> bytes:
        bitmap = _build_bitmap(self.bits)
        body = (struct.pack("<I9xHIHBB", self.board_code, self.sort_type,
                            self.start, self.page_size, 0, 0)
                + bitmap + struct.pack("<BBBB", 0, 0, 0, 1))
        return build_mac_request(0x122C, body)

    def parse_response(self, body: bytes) -> list[MemberQuote]:
        # 响应: 20B响应位图 + <IH total,row_count
        resp_bitmap = body[:20]
        pos = 20
        _total, row_count = unpack_from("<IH", body, pos, "members header")
        pos += 6
        results: list[MemberQuote] = []
        for _ in range(row_count):
            market, code_b, name_b = unpack_from("<H22s44s", body, pos, "member row")
            pos += 68
            fields: dict[str, float] = {}
            for i in range(160):
                if resp_bitmap[i // 8] & (1 << (i % 8)):
                    fmt = _field_fmt(i)
                    (v,) = unpack_from(fmt, body, pos, f"member field {i}")
                    fields[hex(i)] = float(v)
                    pos += struct.calcsize(fmt)
            results.append(MemberQuote(
                market=market,
                code=code_b.decode("gbk", errors="replace").rstrip("\x00"),
                name=name_b.decode("gbk", errors="replace").rstrip("\x00"),
                fields=fields,
            ))
        return results


# ------------------------------------------------------------------ #
# 个股所属板块 0x1218 (head=1)
# ------------------------------------------------------------------ #

class MacBelongBoardCmd(BaseCommand[list[BelongBoard]]):
    def __init__(self, market: int, code: str) -> None:
        self.market = market
        self.code = code

    def build_request(self) -> bytes:
        body = struct.pack("<H8s16x21s", self.market, self.code.encode("gbk"), b"Stock_GLHQ")
        return build_mac_request(0x1218, body, head_flag=1)

    def parse_response(self, body: bytes) -> list[BelongBoard]:
        # 响应: 27B头 + GBK JSON 数组
        json_part = body[27:].decode("gbk", errors="replace")
        results: list[BelongBoard] = []
        try:
            import json
            rows = json.loads(json_part)
            for row in rows:
                if len(row) < 6:
                    continue
                results.append(BelongBoard(
                    market=int(row[1]), board_code=str(row[2]),
                    board_name=str(row[3]),
                    close=float(row[4] or 0), pre_close=float(row[5] or 0),
                ))
        except Exception:
            pass
        return results


# ------------------------------------------------------------------ #
# 集合竞价 0x123D
# ------------------------------------------------------------------ #

class MacAuctionCmd(BaseCommand[list[AuctionItem]]):
    def __init__(self, market: int, code: str, start: int = 0, count: int = 100) -> None:
        self.market = market
        self.code = code
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        body = struct.pack("<H22sII10x", self.market, self.code.encode("gbk"),
                           self.start, self.count)
        return build_mac_request(0x123D, body)

    def parse_response(self, body: bytes) -> list[AuctionItem]:
        (count,) = unpack_from("<I", body, 24, "auction count")
        results: list[AuctionItem] = []
        for i in range(count):
            off = 36 + i * 16
            try:
                time_sec, price, matched, unmatched = unpack_from("<IfIi", body, off, "auction item")
            except ValueError:
                break
            h, m, s = time_sec // 3600, time_sec % 3600 // 60, time_sec % 60
            results.append(AuctionItem(
                time=f"{h:02d}:{m:02d}:{s:02d}", price=price,
                matched=matched, unmatched=unmatched,
            ))
        return results


# ------------------------------------------------------------------ #
# 市场异动 0x1237
# ------------------------------------------------------------------ #

class MacUnusualCmd(BaseCommand[list[UnusualItem]]):
    def __init__(self, market: int, start: int = 0, count: int = 600) -> None:
        self.market = market
        self.start = start
        self.count = min(count, 600)

    def build_request(self) -> bytes:
        body = struct.pack("<HH2xH2xH5H", self.market, self.start, self.count,
                           1, 200, 30, 40, 50, 200)
        return build_mac_request(0x1237, body)

    def parse_response(self, body: bytes) -> list[UnusualItem]:
        (count,) = unpack_from("<H", body, 0, "unusual count")
        results: list[UnusualItem] = []
        for i in range(count):
            off = 2 + i * 32
            if off + 32 > len(body):
                break
            # <H6sBBBHH = 7字段: market, code, pad, type, pad, index, z
            market, code_b, _p1, utype, _p2, _idx, _z = unpack_from(
                "<H6sBBBHH", body, off, "unusual record")
            desc, value = self._describe(utype, body[off + 15: off + 28])
            hour, minute_sec = unpack_from("<BH", body, off + 29, "unusual time")
            results.append(UnusualItem(
                market=market,
                code=code_b.decode("gbk", errors="replace").rstrip("\x00"),
                name="", type=utype, desc=desc, value=value,
                time=f"{hour:02d}:{minute_sec // 100:02d}:{minute_sec % 100:02d}",
            ))
        # 文本段: 股票名
        text = body[2 + count * 32:].decode("gbk", errors="ignore").strip(",").split(",")
        for i, item in enumerate(results):
            if i < len(text):
                item.name = text[i]
        return results

    @staticmethod
    def _describe(utype: int, data: bytes) -> tuple[str, float]:
        """异动类型描述。"""
        if len(data) < 13:
            return "", 0.0
        v1, v2, v3, v4 = struct.unpack("<B2fI", data)
        names = {
            0x03: "主力买卖", 0x04: "加速拉升", 0x05: "加速下跌", 0x06: "低位反弹",
            0x07: "高位回落", 0x08: "撑杆跳高", 0x09: "平台跳水", 0x0A: "单笔冲涨跌",
            0x0B: "区间放量", 0x0C: "区间缩量", 0x10: "大单托盘", 0x11: "大单压盘",
            0x12: "大单锁盘", 0x13: "竞价试买", 0x14: "竞价涨跌",
        }
        return names.get(utype, str(utype)), float(v2)


# ------------------------------------------------------------------ #
# 个股特征快照 0x122A
# ------------------------------------------------------------------ #

class MacSymbolInfoCmd(BaseCommand[SymbolSnapshot]):
    def __init__(self, market: int, code: str) -> None:
        self.market = market
        self.code = code

    def build_request(self) -> bytes:
        body = struct.pack("<H22sI12x", self.market, self.code.encode("gbk"), 1)
        return build_mac_request(0x122A, body)

    def parse_response(self, body: bytes) -> SymbolSnapshot:
        _market, code_b, name_b = unpack_from("<H22s44s", body, 8, "symbol info head")
        date, t, activity, pre_close, open_, high, low, close, momentum, vol, amount, in_vol, out_vol = \
            unpack_from("<III5ffIfII", body, 96, "symbol info body")
        turnover, avg_price = unpack_from("<If", body, 148, "symbol info tail")
        return SymbolSnapshot(
            market=self.market,
            code=code_b.decode("gbk", errors="replace").rstrip("\x00"),
            name=name_b.decode("gbk", errors="replace").rstrip("\x00"),
            date=date, time=t, activity=activity,
            pre_close=pre_close, open=open_, high=high, low=low, close=close,
            vol=vol, amount=amount, turnover=turnover, avg_price=avg_price,
        )


# ------------------------------------------------------------------ #
# 扩展市场商品 0x2562
# ------------------------------------------------------------------ #

class MacGoodsListCmd(BaseCommand[list[GoodsItem]]):
    def __init__(self, market: int, start: int = 0, count: int = 600) -> None:
        self.market = market
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        body = struct.pack("<HII", self.market, self.start, self.count)
        return build_mac_request(0x2562, body)

    def parse_response(self, body: bytes) -> list[GoodsItem]:
        (count,) = unpack_from("<H", body, 0, "goods count")
        results: list[GoodsItem] = []
        for i in range(count):
            off = 2 + i * 48
            try:
                category, name_b, _u, index, switch, v1, v2, v3, c1, c2 = \
                    unpack_from("<H23sHIBfffHH", body, off, "goods item")
            except ValueError:
                break
            results.append(GoodsItem(
                category=category,
                name=name_b.decode("gbk", errors="replace").rstrip("\x00"),
                code=v1, switch=switch, index=index,
            ))
        return results


# ------------------------------------------------------------------ #
# 服务器信息 0x120F
# ------------------------------------------------------------------ #

class MacServerInfoCmd(BaseCommand[ServerInfo]):
    def build_request(self) -> bytes:
        body = bytes.fromhex("04002d31" + "00" * 8 + "0027060e" + "00" * 52)
        return build_mac_request(0x120F, body)

    def parse_response(self, body: bytes) -> ServerInfo:
        if len(body) < 87:
            return ServerInfo(date=0, last_trade_date=0)
        pos = 0
        pos += 2          # count
        pos += 8          # flags
        pos += 3          # tag
        pos += 9          # reserved
        (date,) = unpack_from("<I", body, pos, "server date")
        pos += 4 + 4      # date + ts1
        sessions: list[tuple[int, int]] = []
        for _ in range(2):
            times = unpack_from("<8H", body, pos, "server session")
            # 每对(H开,H收)是分钟数(570=09:30, 690=11:30)
            sessions.append((times[0], times[1]))
            pos += 16
        pos += 1          # flag
        (last_date,) = unpack_from("<I", body, pos, "server last date")
        return ServerInfo(date=date, last_trade_date=last_date, sessions=sessions)


# ------------------------------------------------------------------ #
# K线偏移 0x124A
# ------------------------------------------------------------------ #

class MacKlineOffsetCmd(BaseCommand[tuple[int, int]]):
    def __init__(self, offset: int = 0, count: int = 128000) -> None:
        self.offset = offset
        self.count = min(count, 128000)

    def build_request(self) -> bytes:
        body = struct.pack("<II5x", self.offset, self.count)
        return build_mac_request(0x124A, body)

    def parse_response(self, body: bytes) -> tuple[int, int]:
        total = struct.unpack(">I", body[:4])[0]   # 大端
        (returned,) = unpack_from("<I", body, 4, "kline offset returned")
        return total, returned


# ------------------------------------------------------------------ #
# 资金流向 0x1218 (head=2)
# ------------------------------------------------------------------ #

class MacCapitalFlowCmd(BaseCommand[CapitalFlow | None]):
    def __init__(self, market: int, code: str) -> None:
        self.market = market
        self.code = code

    def build_request(self) -> bytes:
        body = struct.pack("<H8s16x21s", self.market, self.code.encode("gbk"), b"Stock_ZJLX")
        return build_mac_request(0x1218, body, head_flag=2)

    def parse_response(self, body: bytes) -> CapitalFlow | None:
        json_part = body[27:].decode("gbk", errors="replace")
        try:
            import json
            rows = json.loads(json_part)
            if len(rows) < 2:
                return None
            r0 = rows[0]  # [主买,主卖,散户买,散户卖]
            r1 = rows[1]  # [5日买,5日卖,超大,大,中,小]
            return CapitalFlow(
                market=self.market, code=self.code,
                main_buy=float(r0[0]), main_sell=float(r0[1]),
                retail_buy=float(r0[2]), retail_sell=float(r0[3]),
                buy5=float(r1[0]), sell5=float(r1[1]),
                big5=float(r1[3]), mid5=float(r1[4]),
            )
        except Exception:
            return None
class MacSymbolQuotesCmd(BaseCommand[list[MemberQuote]]):
    """批量获取自定义字段报价（最多80只/次）。"""

    def __init__(self, stocks: list[tuple[int, str]], bits: list[int] | None = None) -> None:
        self.stocks = stocks
        self.bits = bits if bits is not None else list(range(0x00, 0x08))

    @staticmethod
    def _build_bitmap(bits: list[int]) -> bytes:
        bm = bytearray(20)
        for b in bits:
            bm[b // 8] |= 1 << (b % 8)
        return bytes(bm)

    @staticmethod
    def _field_fmt(bit: int) -> str:
        if bit in (0x05, 0x08, 0x09, 0x0A, 0x0B, 0x13, 0x14, 0x15, 0x18, 0x19, 0x1A):
            return "<I"
        if bit in (0x2E,):
            return "<i"
        return "<f"

    def build_request(self) -> bytes:
        body = self._build_bitmap(self.bits) + struct.pack("<H", len(self.stocks))
        for mkt, code in self.stocks:
            body += struct.pack("<H22s", mkt, code.encode("gbk"))
        return build_mac_request(0x122B, body)

    def parse_response(self, body: bytes) -> list[MemberQuote]:
        resp_bitmap = body[:20]
        pos = 20  # 20B响应位图
        _total, row_count = unpack_from("<IH", body, pos, "quotes header")
        pos += 6
        results: list[MemberQuote] = []
        for _ in range(row_count):
            market, code_b, name_b = unpack_from("<H22s44s", body, pos, "quote row")
            pos += 68
            fields: dict[str, float] = {}
            for i in range(160):
                if resp_bitmap[i // 8] & (1 << (i % 8)):
                    fmt = self._field_fmt(i)
                    (v,) = unpack_from(fmt, body, pos, f"quote field {i}")
                    fields[hex(i)] = float(v)
                    pos += struct.calcsize(fmt)
            results.append(MemberQuote(
                market=market,
                code=code_b.decode("gbk", errors="replace").rstrip("\x00"),
                name=name_b.decode("gbk", errors="replace").rstrip("\x00"),
                fields=fields,
            ))
        return results


# ------------------------------------------------------------------ #
# 远程文件 0x1215 / 0x1217
# ------------------------------------------------------------------ #

class FileMeta:
    __slots__ = ("offset", "size", "flag", "md5")

    def __init__(self, offset: int, size: int, flag: int, md5: str) -> None:
        self.offset = offset
        self.size = size
        self.flag = flag
        self.md5 = md5


class MacFileListCmd(BaseCommand[FileMeta]):
    """远程文件元信息 0x1215。"""

    def __init__(self, filename: str) -> None:
        self.filename = filename

    def build_request(self) -> bytes:
        fn = self.filename.encode("gbk", errors="replace")[:70].ljust(70, b"\x00")
        body = struct.pack("<I", 0) + fn + b"\x00" * 30
        return build_mac_request(0x1215, body)

    def parse_response(self, body: bytes) -> FileMeta:
        offset, size, flag = unpack_from("<IIb", body, 0, "file meta")
        md5 = body[9:41].decode("ascii", errors="replace").rstrip("\x00")
        return FileMeta(offset=offset, size=size, flag=flag, md5=md5)


class MacFileDownloadCmd(BaseCommand[bytes]):
    """远程文件分块下载 0x1217。"""

    def __init__(self, filename: str, index: int, offset: int, size: int) -> None:
        self.filename = filename
        self.index = index
        self.offset = offset
        self.size = size

    def build_request(self) -> bytes:
        fn = self.filename.encode("gbk", errors="replace")[:70].ljust(70, b"\x00")
        body = struct.pack("<III", self.index, self.offset, self.size) + fn + b"\x00" * 30
        return build_mac_request(0x1217, body)

    def parse_response(self, body: bytes) -> bytes:
        return body[8:]

# ------------------------------------------------------------------ #
# 分时缩略采样 0x254D
# ------------------------------------------------------------------ #

class MacChartSamplingCmd(BaseCommand[list[float]]):
    """分时缩略采样价格点（约240个）。"""

    def __init__(self, market: int, code: str) -> None:
        self.market = market
        self.code = code

    def build_request(self) -> bytes:
        raw = self.code.encode("gbk")
        padded = (raw + b"\x00" * 22)[:22]
        body = struct.pack("<H22sHH9x", self.market, padded, 1, 20)
        return build_mac_request(0x254D, body)

    def parse_response(self, body: bytes) -> list[float]:
        if len(body) < 42:
            return []
        (count,) = unpack_from("<H", body, 40, "chart sampling count")
        prices: list[float] = []
        for i in range(count):
            off = 42 + i * 4
            if off + 4 > len(body):
                break
            (p,) = unpack_from("<f", body, off, "chart sampling price")
            prices.append(p)
        return prices
