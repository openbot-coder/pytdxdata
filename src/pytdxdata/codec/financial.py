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

import io
import struct
import zipfile

from ..models import FinancialFileInfo, FinancialRecord

_HEADER_FMT = "<1hI1H3L"
_INDEX_FMT = "<6sBL"

_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_INDEX_SIZE = struct.calcsize(_INDEX_FMT)
_ZIP_MAGIC = b"PK\x03\x04"


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


def looks_like_gpcw_dat(data: bytes) -> bool:
    """粗略判定字节流是否为 gpcw ``.dat``（校验 20 字节 header 的自洽性）。

    用于把「真的取到了财报数据」与「服务器对不存在的文件名回的 0/1 字节空壳」区分开：
    空壳要么长度不足、要么 header 全零，一律判否。

    Args:
        data: 待判定的字节流。

    Returns:
        可疑时返回 True。
    """
    if len(data) < _HEADER_SIZE:
        return False
    _unknown, _date, max_count, _u1, report_size, _u2 = struct.unpack(
        _HEADER_FMT, data[:_HEADER_SIZE]
    )
    if max_count <= 0 or report_size <= 0 or report_size % 4 != 0:
        return False
    # 索引区必须放得下 max_count 条 11 字节条目
    return _HEADER_SIZE + max_count * _INDEX_SIZE <= len(data)


def extract_gpcw_dat(data: bytes) -> bytes:
    """从「zip 包」或「未压缩 dat」中取出 ``.dat`` 字节；无法识别时返回 ``b""``。

    服务器**实际只提供未压缩的** ``tdxfin/gpcwYYYYMMDD.dat``，而 ``tdxfin/gpcw.txt``
    索引里写的是 ``gpcwYYYYMMDD.zip``，因此两种形态都要能走通。

    Args:
        data: zip 包字节或未压缩 ``.dat`` 字节。

    Returns:
        内层 / 原始的 ``.dat`` 字节；不是有效财报数据时返回 ``b""``。
    """
    if not data:
        return b""
    if data.startswith(_ZIP_MAGIC):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = [n for n in zf.namelist() if n.lower().endswith(".dat")]
                if not names:
                    return b""
                inner = zf.read(names[0])
        except (zipfile.BadZipFile, OSError, KeyError):
            return b""
        return inner if looks_like_gpcw_dat(inner) else b""
    return data if looks_like_gpcw_dat(data) else b""


def parse_financial_dat(data: bytes, report_date: int = 0) -> list[FinancialRecord]:
    """解析 gpcw ``.dat`` 二进制（zip 内的或服务器直接给的未压缩形式都行）。

    Args:
        data: .dat 文件完整字节。
        report_date: 报告期 YYYYMMDD；传 0 时使用 header 内嵌的报告期。

    Returns:
        FinancialRecord 列表；数据不足或字段非法时返回空列表。
    """
    header_size = _HEADER_SIZE
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

    index_size = _INDEX_SIZE
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
