"""除权除息命令（标准通道 0x1F1876，仅标准通道提供——MAC主机池不响应）"""
from __future__ import annotations

import struct

from ..._binary import slice_bytes, unpack_from
from ...models import XdxrRecord
from ..datetime_ import get_datetime
from ..volume import decode_volume
from .base import BaseCommand

# 与 easy-tdx 一致的分类名称映射（通达信协议标准）
XDXR_CATEGORY_NAMES = {
    1: "除权除息",
    2: "送配股上市",
    3: "非流通股上市",
    4: "未知股本变动",
    5: "股本变化",
    6: "增发新股",
    7: "股份回购",
    8: "增发新股上市",
    9: "转配股上市",
    10: "可转债上市",
    11: "扩缩股",
    12: "非流通股缩股",
    13: "送认购权证",
    14: "送认沽权证",
}


class GetXdxrInfoCmd(BaseCommand[list[XdxrRecord]]):
    """获取除权除息历史记录。"""

    def __init__(self, market: int, code: str) -> None:
        self.market = market
        self.code = code.encode("utf-8")

    def build_request(self) -> bytes:
        header = bytes.fromhex("0c1f187600010b000b000f000100".replace(" ", ""))
        return header + struct.pack("<B6s", self.market, self.code)

    def parse_response(self, body: bytes) -> list[XdxrRecord]:
        if len(body) < 11:
            raise ValueError("xdxr body 过短")
        pos = 9
        (num,) = unpack_from("<H", body, pos, "xdxr header")
        pos += 2
        records: list[XdxrRecord] = []
        for _ in range(num):
            market_b, code_b = unpack_from("<B6s", body, pos, "xdxr record header")
            pos += 7
            pos += 1  # padding
            year, month, day, _h, _m, pos = get_datetime(9, body, pos)
            (category,) = unpack_from("<B", body, pos, "xdxr category")
            pos += 1
            chunk = slice_bytes(body, pos, 16, "xdxr body")
            pos += 16

            rec = XdxrRecord(
                market=market_b, code=code_b.decode("utf-8").rstrip("\x00"),
                year=year, month=month, day=day, category=category,
                name=XDXR_CATEGORY_NAMES.get(category, str(category)),
            )
            if category == 1:
                fenhong, peigujia, songzhuangu, peigu = struct.unpack("<ffff", chunk)
                rec.fenhong = fenhong / 10.0
                rec.peigujia = peigujia
                rec.songzhuangu = songzhuangu / 10.0
                rec.peigu = peigu / 10.0
            elif category in (11, 12):
                _, _, suogu, _ = struct.unpack("<IIfI", chunk)
                rec.suogu = suogu
            elif category in (13, 14):
                xingquanjia, _, fenshu, _ = struct.unpack("<fIfI", chunk)
                rec.xingquanjia = xingquanjia
                rec.fenshu = fenshu
            else:
                # 股本变动类: 4个uint32 = 前流通/前总股本/后流通/后总股本(自定义浮点, 万股)
                ql, qz, hl, hz = struct.unpack("<IIII", chunk)
                rec.panqian_liutong = decode_volume(ql)
                rec.qian_zongguben = decode_volume(qz)
                rec.panhou_liutong = decode_volume(hl)
                rec.hou_zongguben = decode_volume(hz)
            records.append(rec)
        return records
