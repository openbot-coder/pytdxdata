"""MAC 协议请求帧构建（10字节头 + 2字节命令ID + body）"""
from __future__ import annotations

import struct

_MAC_HEADER_FMT = "<BIBHH"
_MAC_HEADER_SIZE = 10
_MAC_HEAD_FLAG = 0x1C


def build_mac_request(msg_id: int, body: bytes, *, head_flag: int = _MAC_HEAD_FLAG) -> bytes:
    """构建 MAC 协议请求帧。"""
    inner = struct.pack("<H", msg_id) + body
    header = struct.pack(
        _MAC_HEADER_FMT,
        head_flag,
        0,              # customize
        1,              # version
        len(inner),     # zipsize
        len(inner),     # unzipsize（MAC不压缩请求）
    )
    return header + inner
