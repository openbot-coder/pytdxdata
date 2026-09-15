"""历史专业财报解析：gpcw.txt 索引 + gpcw*.zip 内 .dat 二进制。

.dat 二进制格式（参考 pytdx.crawler.history_financial_crawler）::

    Header 20B : <1h I 1H 3L
        - unknown        int16    (h)
        - report_date    uint32   (I)   报告期 YYYYMMDD
        - max_count      uint16   (H)   股票索引条目数
        - unknown1       uint32   (L)
        - report_size    uint32   (L)   每条股票数据字节数
        - unknown2       uint32   (L)
    Index  max_count × 11B : <6s B L
        - code           6 bytes  股票代码
        - market         1 byte   市场（0=深 1=沪）
        - file_offset    uint32   绝对偏移（从文件开头算）
    Data   file_offset 处 : report_size/4 个 float32
"""
from __future__ import annotations

import struct

from ..models import FinancialFileInfo, FinancialRecord

_HEADER_FMT = "<1hI1H3L"
_INDEX_FMT = "<6sBL"


def parse_financial_file_list(data: bytes) -> list[FinancialFileInfo]:
    """解析 tdxfin/gpcw.txt 内容（每行 ``filename,md5hash,filesize``）。"""
    if not data:
        return []
    text = data.decode("utf-8", errors="replace").strip()
    out: list[FinancialFileInfo] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            continue
        try:
            filesize = int(parts[2])
        except ValueError:
            continue
        out.append(FinancialFileInfo(filename=parts[0], hash=parts[1], filesize=filesize))
    return out


def parse_financial_dat(data: bytes, report_date: int = 0) -> list[FinancialRecord]:
    """解析 gpcw*.zip 内的 .dat 二进制。

    Args:
        data: .dat 文件完整字节。
        report_date: 报告期 YYYYMMDD；传 0 时使用 header 内嵌的报告期。

    Returns:
        FinancialRecord 列表；数据不足或字段非法时返回空列表。
    """
    header_size = struct.calcsize(_HEADER_FMT)
    if len(data) < header_size:
        return []

    header = struct.unpack(_HEADER_FMT, data[:header_size])
    dat_report_date = header[1]
    max_count = header[2]
    report_size = header[4]

    if report_date == 0:
        report_date = dat_report_date

    num_fields = report_size // 4
    if num_fields <= 0:
        return []

    index_size = struct.calcsize(_INDEX_FMT)
    report_fmt = f"<{num_fields}f"
    report_pack_size = struct.calcsize(report_fmt)

    out: list[FinancialRecord] = []
    for i in range(max_count):
        idx_pos = header_size + i * index_size
        if idx_pos + index_size > len(data):
            break

        code_bytes, market, file_offset = struct.unpack(
            _INDEX_FMT, data[idx_pos : idx_pos + index_size]
        )
        code = code_bytes.decode("ascii", errors="replace").rstrip("\x00")
        if not code or file_offset == 0:
            continue
        if file_offset + report_pack_size > len(data):
            continue

        fields = list(
            struct.unpack(report_fmt, data[file_offset : file_offset + report_pack_size])
        )
        out.append(
            FinancialRecord(
                code=code, market=market, report_date=report_date, fields=fields
            )
        )
    return out
