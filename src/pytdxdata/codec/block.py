"""板块文件 (.dat) 解析：通达信 block_zs/gn/fg.dat -> TdxBlock 列表。

字节格式（与 easy-tdx / pytdx 一致）::

    Header : 384 字节（跳过，含版本/日期等未知字段）
    Count  : 2 字节  uint16 LE —— 板块条数
    Body   : count × 2813 字节
             每条 = 9s  板块名(GBK)
                  + H   成分股数量
                  + H   类型
                  + 2800s 代码区（每只股票占 7 字节 ASCII）
"""
from __future__ import annotations

import struct

from ..models import TdxBlock

_HEADER_SIZE = 384
_RECORD_SIZE = 2813
_CODE_SLOT = 7
_MAX_CODES = 2800 // _CODE_SLOT  # 单条记录最多 400 只


def infer_block_category(filename: str) -> int:
    """按文件名推断板块分类：zs=行业(0) / gn=概念(2) / fg=风格(3)。"""
    name = filename.lower()
    if "gn" in name:
        return 2
    if "fg" in name:
        return 3
    return 0


def parse_block_dat(data: bytes, filename: str = "") -> list[TdxBlock]:
    """解析通达信 .dat 板块文件字节内容。

    Args:
        data: 文件完整字节。
        filename: 用于推断 category（如 ``block_gn.dat``）。

    Returns:
        TdxBlock 列表；数据不足或为空时返回空列表。
    """
    if len(data) < _HEADER_SIZE + 2:
        return []

    (count,) = struct.unpack_from("<H", data, _HEADER_SIZE)
    category = infer_block_category(filename)

    results: list[TdxBlock] = []
    pos = _HEADER_SIZE + 2
    for _ in range(count):
        if pos + _RECORD_SIZE > len(data):
            break

        name = data[pos : pos + 9].decode("gbk", errors="replace").strip("\x00").strip()
        (stock_count,) = struct.unpack_from("<H", data, pos + 9)

        codes: list[str] = []
        codes_start = pos + 13
        for i in range(min(stock_count, _MAX_CODES)):
            raw = data[codes_start + i * _CODE_SLOT : codes_start + (i + 1) * _CODE_SLOT]
            code = raw.decode("ascii", errors="replace").strip("\x00").strip()
            if code:
                codes.append(code)

        results.append(
            TdxBlock(name=name, category=category, count=stock_count, codes=codes)
        )
        pos += _RECORD_SIZE

    return results
