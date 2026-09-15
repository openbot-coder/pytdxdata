"""响应帧头解析 + zlib 按需解压（16字节固定头，标准/MAC两通道共用）"""
from __future__ import annotations

import zlib
from dataclasses import dataclass

from .._binary import unpack_from

HEADER_SIZE = 16
_HEADER_FMT = "<IIIHH"


@dataclass(frozen=True, slots=True)
class FrameHeader:
    magic: int      # 恒为 7654321 (0x0074CBB1)
    seq_id: int
    method: int
    zipsize: int    # body 实际长度
    unzipsize: int  # 解压后长度；相等=未压缩


def parse_header(buf: bytes | bytearray | memoryview) -> FrameHeader:
    magic, seq_id, method, zipsize, unzipsize = unpack_from(_HEADER_FMT, buf, 0, "frame header")
    return FrameHeader(magic, seq_id, method, zipsize, unzipsize)


def decompress_body(header: FrameHeader, raw_body: bytes) -> bytes:
    """按需 zlib 解压 body（zipsize==unzipsize 直接返回）。"""
    if len(raw_body) != header.zipsize:
        raise ValueError(
            f"frame body 长度不符: header={header.zipsize}, actual={len(raw_body)}"
        )
    if header.zipsize == header.unzipsize:
        return raw_body
    try:
        body = zlib.decompress(raw_body)
    except zlib.error as e:
        raise ValueError(f"zlib 解压失败: {e}") from e
    if len(body) != header.unzipsize:
        raise ValueError(
            f"解压长度不符: header={header.unzipsize}, actual={len(body)}"
        )
    return body
