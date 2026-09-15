"""日期时间解码（通达信 TCP 两种格式：分钟级 zipday / 日线级 YYYYMMDD）"""
from __future__ import annotations

from .._binary import unpack_from


def get_datetime(category: int, data: bytes | bytearray | memoryview, pos: int) -> tuple[int, int, int, int, int, int]:
    """按K线周期解析4字节时间，返回 (year, month, day, hour, minute, new_pos)。

    分钟级（category<4 或 ==7/8）：2字节压缩日期+2字节分钟数，year=(>>11)+2004
    日线及以上：4字节 YYYYMMDD，hour=15, minute=0（收盘时间）
    """
    if category < 4 or category in (7, 8):
        zipday, tminutes = unpack_from("<HH", data, pos, "minute datetime")
        year = (zipday >> 11) + 2004
        month = (zipday % 2048) // 100
        day = (zipday % 2048) % 100
        hour = tminutes // 60
        minute = tminutes % 60
        return year, month, day, hour, minute, pos + 4
    (zipday,) = unpack_from("<I", data, pos, "day datetime")
    return zipday // 10000, (zipday % 10000) // 100, zipday % 100, 15, 0, pos + 4


def get_time(data: bytes | bytearray | memoryview, pos: int) -> tuple[int, int, int]:
    """解析2字节时间（分钟数），返回 (hour, minute, new_pos)。"""
    (tminutes,) = unpack_from("<H", data, pos, "trade time")
    return tminutes // 60, tminutes % 60, pos + 2
