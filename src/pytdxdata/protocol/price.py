"""变长有符号整数编解码（通达信价格编码）+ OHLC 差分还原"""
from __future__ import annotations

from .._binary import unpack_from


def get_price(data: bytes | bytearray | memoryview, pos: int) -> tuple[int, int]:
    """解码一个变长有符号整数，返回 (value, new_pos)。

    协议规则：首字节 bit7=继续, bit6=符号(1负), bit5~0=低6位；后续字节 bit7=继续, bit6~0=7位。
    """
    start = pos
    try:
        b = data[pos]
    except IndexError:
        raise ValueError(f"price varint 截断 @{start}")
    value = b & 0x3F
    negative = bool(b & 0x40)
    if b & 0x80:
        bit_shift = 6
        while True:
            pos += 1
            b = data[pos]
            value |= (b & 0x7F) << bit_shift
            bit_shift += 7
            if not (b & 0x80):
                break
    pos += 1
    return (-value if negative else value), pos


def put_price(value: int) -> bytes:
    """将整数编码为变长格式（构造请求包用）。"""
    negative = value < 0
    value = abs(value)
    first = value & 0x3F
    value >>= 6
    if negative:
        first |= 0x40
    if value:
        first |= 0x80
    result = bytearray([first])
    while value:
        b = value & 0x7F
        value >>= 7
        if value:
            b |= 0x80
        result.append(b)
    return bytes(result)


def decode_ohlc_diffs(data: bytes | bytearray | memoryview, pos: int) -> tuple[float, float, float, float, int]:
    """解码 K线单条记录的 OHLC 差分（相对基准），返回 (open, close, high, low, new_pos)。

    差分还原（与 pytdx/easy-tdx 一致）：
      open_abs  = open_diff  + pre_diff_base
      close_abs = open_abs   + close_diff
      high_abs  = open_abs   + high_diff
      low_abs   = open_abs   + low_diff
      pre_diff_base 更新为 open_abs + close_diff（由调用方维护）
    价格 /1000 还原为元。
    """
    open_diff, pos = get_price(data, pos)
    close_diff, pos = get_price(data, pos)
    high_diff, pos = get_price(data, pos)
    low_diff, pos = get_price(data, pos)
    return open_diff, close_diff, high_diff, low_diff, pos
