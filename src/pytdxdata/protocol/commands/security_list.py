"""证券列表命令（标准通道 0x011864，1000条/页）"""
from __future__ import annotations

import struct

from ..._binary import slice_bytes, unpack_from
from ...models import SecurityInfo
from ..volume import decode_volume
from .base import BaseCommand

PAGE_SIZE = 1000
_RECORD_SIZE = 29


class GetSecurityCountCmd(BaseCommand[int]):
    """获取市场证券数量。"""

    def __init__(self, market: int) -> None:
        self.market = market

    def build_request(self) -> bytes:
        # 与 easy-tdx 一致: 0c0c186c 头 + market(H) + 4字节尾部
        header = bytes.fromhex("0c0c186c0001080008004e04".replace(" ", ""))
        return header + struct.pack("<H", self.market) + b"\x75\xc7\x33\x01"

    def parse_response(self, body: bytes) -> int:
        (count,) = unpack_from("<H", body, 0, "security count")
        return int(count)


class GetSecurityListCmd(BaseCommand[list[SecurityInfo]]):
    """分页获取证券列表。"""

    def __init__(self, market: int, start: int) -> None:
        self.market = market
        self.start = start

    def build_request(self) -> bytes:
        header = bytes.fromhex("0c0118640101060006005004".replace(" ", ""))
        return header + struct.pack("<HHH", self.market, self.start, 0)

    def parse_response(self, body: bytes) -> list[SecurityInfo]:
        (num,) = unpack_from("<H", body, 0, "security list header")
        pos = 2
        results: list[SecurityInfo] = []
        for _ in range(num):
            raw = slice_bytes(body, pos, _RECORD_SIZE, "security list record")
            (code_bytes, volunit, name_bytes, _u1, decimal_point, pre_close_raw, _u2) = (
                struct.unpack("<6sH8s4sBI4s", raw)
            )
            results.append(SecurityInfo(
                market=self.market,
                code=code_bytes.decode("utf-8", errors="replace").rstrip("\x00"),
                name=name_bytes.decode("gbk", errors="replace").rstrip("\x00"),
                volunit=volunit,
                decimal_point=decimal_point,
                pre_close=decode_volume(pre_close_raw),
            ))
            pos += _RECORD_SIZE
        return results
