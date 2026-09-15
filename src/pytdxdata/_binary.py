"""二进制解包工具"""
from __future__ import annotations

import struct
from typing import Any


def unpack_from(fmt: str, data: bytes | bytearray | memoryview, pos: int, what: str = "") -> tuple[Any, ...]:
    """从 pos 处按 fmt 解包，越界抛清晰错误。"""
    try:
        return struct.unpack_from(fmt, data, pos)
    except struct.error as e:
        raise ValueError(f"{what} 解包失败 @{pos} fmt={fmt}: {e}") from e


def slice_bytes(data: bytes | bytearray | memoryview, pos: int, length: int, what: str = "") -> bytes:
    end = pos + length
    if end > len(data):
        raise ValueError(f"{what} 截断 @{pos} need {length} have {len(data) - pos}")
    return bytes(data[pos:end])
