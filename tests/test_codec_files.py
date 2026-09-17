"""服务器文件解析器测试：板块 .dat / 行业 tdxhy.cfg / 历史专业财报 gpcw。

全部离线构造二进制样本，不连网络。
"""
from __future__ import annotations

import io
import struct
import unittest.mock as mock
import zipfile

import pytest

from pytdxdata.api import TdxData
from pytdxdata.codec.block import infer_block_category, parse_block_dat
from pytdxdata.codec.financial import parse_financial_dat, parse_financial_file_list
from pytdxdata.codec.industry import parse_tdxhy_cfg
from pytdxdata.models import FinancialFileInfo, FinancialRecord, IndustryInfo, TdxBlock

# --------------------------------------------------------------------------- #
# 样本构造工具
# --------------------------------------------------------------------------- #

_HEADER = 384
_RECORD = 2813


def _make_block_dat(blocks: list[tuple[str, list[str], int, int]]) -> bytes:
    """构造板块 .dat：blocks = [(name, codes, declared_count, type), ...]"""
    out = bytearray(_HEADER)
    out += struct.pack("<H", len(blocks))
    for name, codes, declared_count, btype in blocks:
        rec = bytearray()
        rec += name.encode("gbk")[:9].ljust(9, b"\x00")
        rec += struct.pack("<HH", declared_count, btype)
        area = bytearray(2800)
        for i, code in enumerate(codes):
            area[i * 7 : (i + 1) * 7] = code.encode("ascii").ljust(7, b"\x00")
        rec += area
        assert len(rec) == _RECORD
        out += rec
    return bytes(out)


def _make_dat_raw(
    index_entries: list[tuple[bytes, int, int]],
    data_blob: bytes,
    *,
    header_report_date: int = 20260331,
    max_count: int | None = None,
    report_size: int = 8,
) -> bytes:
    """构造 gpcw*.dat：index_entries = [(code_bytes, market, offset), ...]"""
    count = len(index_entries) if max_count is None else max_count
    idx = b"".join(struct.pack("<6sBL", cb, m, off) for cb, m, off in index_entries)
    header = struct.pack("<1hI1H3L", 0, header_report_date, count, 0, report_size, 0)
    return header + idx + data_blob


def _make_financial_zip(dat_bytes: bytes, inner_name: str = "gpcw.dat") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_name, dat_bytes)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# parse_block_dat
# --------------------------------------------------------------------------- #

def test_parse_block_dat_basic():
    data = _make_block_dat([
        ("银行", ["000001", "600036"], 2, 1),
        ("房地产", ["000002"], 1, 1),
    ])
    blocks = parse_block_dat(data, "block_zs.dat")
    assert len(blocks) == 2
    assert isinstance(blocks[0], TdxBlock)
    assert blocks[0].name == "银行"
    assert blocks[0].category == 0
    assert blocks[0].count == 2
    assert blocks[0].codes == ["000001", "600036"]
    assert blocks[1].name == "房地产"
    assert blocks[1].codes == ["000002"]


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("block_zs.dat", 0),
        ("block_gn.dat", 2),
        ("block_fg.dat", 3),
        ("mystery.dat", 0),
        ("BLOCK_GN.DAT", 2),
    ],
)
def test_infer_block_category(filename, expected):
    assert infer_block_category(filename) == expected


def test_parse_block_dat_too_short():
    assert parse_block_dat(b"\x00" * 100) == []


def test_parse_block_dat_count_exceeds_body():
    """声明 3 条但只带 1 条记录 -> 只解析出 1 条。"""
    data = _make_block_dat([("银行", ["000001"], 1, 1)])
    data = data[: _HEADER] + struct.pack("<H", 3) + data[_HEADER + 2 :]
    blocks = parse_block_dat(data, "block_zs.dat")
    assert len(blocks) == 1


def test_parse_block_dat_declared_count_over_codes():
    """声明的成分数大于实际写入的代码数 -> 空槽位被跳过。"""
    data = _make_block_dat([("银行", ["000001", "600036"], 5, 1)])
    blocks = parse_block_dat(data, "block_zs.dat")
    assert blocks[0].count == 5
    assert blocks[0].codes == ["000001", "600036"]


# --------------------------------------------------------------------------- #
# parse_tdxhy_cfg
# --------------------------------------------------------------------------- #

def test_parse_tdxhy_cfg_basic():
    content = "0|000001|A01|||S01\n1|600036|A02|||S02\n".encode("gbk")
    result = parse_tdxhy_cfg(content)
    assert set(result) == {"000001", "600036"}
    assert isinstance(result["000001"], IndustryInfo)
    assert result["000001"].tdx_industry == "A01"
    assert result["000001"].sw_industry == "S01"


def test_parse_tdxhy_cfg_short_line_skipped():
    """不足 3 列的行被跳过。"""
    content = "0|000001\nbadline\n".encode("gbk")
    assert parse_tdxhy_cfg(content) == {}


def test_parse_tdxhy_cfg_non_six_digit_skipped():
    content = "0|SH600036|A01|||S01\n0|60003|A01\n0|abcdef|A01|||S01\n".encode("gbk")
    assert parse_tdxhy_cfg(content) == {}


def test_parse_tdxhy_cfg_missing_sw_column():
    """只有 5 列时申万行业为空串。"""
    content = "0|000001|A01||\n".encode("gbk")
    result = parse_tdxhy_cfg(content)
    assert result["000001"].tdx_industry == "A01"
    assert result["000001"].sw_industry == ""


def test_parse_tdxhy_cfg_empty():
    assert parse_tdxhy_cfg(b"") == {}


# --------------------------------------------------------------------------- #
# parse_financial_file_list
# --------------------------------------------------------------------------- #

def test_parse_financial_file_list_basic():
    data = b"gpcw20260331.zip,abcdef012345,1024\ngpcw20251231.zip,deadbeef,2048\n"
    infos = parse_financial_file_list(data)
    assert len(infos) == 2
    assert isinstance(infos[0], FinancialFileInfo)
    assert infos[0].filename == "gpcw20260331.zip"
    assert infos[0].hash == "abcdef012345"
    assert infos[0].filesize == 1024


def test_parse_financial_file_list_skips_bad_lines():
    data = b"badline\nx,y,notanint\nok.zip,abc,10\n\n"
    infos = parse_financial_file_list(data)
    assert [i.filename for i in infos] == ["ok.zip"]


def test_parse_financial_file_list_empty():
    assert parse_financial_file_list(b"") == []


# --------------------------------------------------------------------------- #
# parse_financial_dat
# --------------------------------------------------------------------------- #

def _valid_two_record_dat() -> bytes:
    # data 起点 = 20(header) + 2*11(index) = 42
    blob = struct.pack("<2f", 1.5, 2.5) + struct.pack("<2f", 3.0, 4.0)
    return _make_dat_raw(
        [(b"600000", 1, 42), (b"000001", 0, 50)], blob
    )


def test_parse_financial_dat_explicit_date():
    recs = parse_financial_dat(_valid_two_record_dat(), report_date=20260101)
    assert len(recs) == 2
    assert isinstance(recs[0], FinancialRecord)
    assert recs[0].code == "600000"
    assert recs[0].market == 1
    assert recs[0].report_date == 20260101
    assert recs[0].fields == pytest.approx([1.5, 2.5])
    assert recs[1].code == "000001"
    assert recs[1].market == 0
    assert recs[1].fields == pytest.approx([3.0, 4.0])


def test_parse_financial_dat_fallback_date_from_header():
    recs = parse_financial_dat(_valid_two_record_dat(), report_date=0)
    assert recs[0].report_date == 20260331


def test_parse_financial_dat_too_short():
    assert parse_financial_dat(b"\x00" * 10) == []


def test_parse_financial_dat_zero_report_size():
    data = _make_dat_raw([(b"600000", 1, 31)], b"", report_size=0)
    assert parse_financial_dat(data) == []


def test_parse_financial_dat_skips_zero_offset_and_empty_code():
    blob = struct.pack("<2f", 1.0, 2.0)
    # 第 1 条 offset=0 跳过；第 2 条 code 全 0 跳过
    data = _make_dat_raw([(b"600000", 1, 0), (b"\x00" * 6, 1, 31)], blob)
    assert parse_financial_dat(data) == []


def test_parse_financial_dat_offset_out_of_range():
    data = _make_dat_raw([(b"600000", 1, 9999)], struct.pack("<2f", 1.0, 2.0))
    assert parse_financial_dat(data) == []


def test_parse_financial_dat_max_count_breaks():
    """max_count 大于实际索引 -> 越界即 break。"""
    blob = struct.pack("<2f", 1.0, 2.0)
    data = _make_dat_raw([(b"600000", 1, 31)], blob, max_count=5)
    recs = parse_financial_dat(data)
    assert len(recs) == 1


# --------------------------------------------------------------------------- #
# TdxData 解析接口
# --------------------------------------------------------------------------- #

def _new_tdxdata() -> TdxData:
    with mock.patch("pytdxdata.api.get_standard_hosts", return_value=["s1"]), \
         mock.patch("pytdxdata.api.get_mac_hosts", return_value=["m1"]), \
         mock.patch("pytdxdata.api.get_ex_hosts", return_value=[]):
        return TdxData(cache_dir=None, pool_min=1, pool_max=2, ex_servers=[])


@pytest.mark.asyncio
async def test_api_get_block_parsed():
    td = _new_tdxdata()
    raw = _make_block_dat([("银行", ["000001"], 1, 1)])
    with mock.patch.object(td, "get_file", new=mock.AsyncMock(return_value=raw)):
        blocks = await td.get_block_parsed("block_gn.dat")
    assert len(blocks) == 1
    assert blocks[0].category == 2  # gn -> 概念


@pytest.mark.asyncio
async def test_api_get_industry_map():
    td = _new_tdxdata()
    raw = "0|000001|A01|||S01\n".encode("gbk")
    patched = mock.AsyncMock(return_value=raw)
    with mock.patch.object(td, "get_file", new=patched):
        result = await td.get_industry_map()
    patched.assert_awaited_once_with("tdxhy.cfg")
    assert result["000001"].sw_industry == "S01"


@pytest.mark.asyncio
async def test_api_get_financial_file_infos():
    td = _new_tdxdata()
    raw = b"gpcw20260331.zip,abc,1024\n"
    patched = mock.AsyncMock(return_value=raw)
    with mock.patch.object(td, "get_file", new=patched):
        infos = await td.get_financial_file_infos()
    patched.assert_awaited_once_with("tdxfin/gpcw.txt")
    assert infos[0].filesize == 1024


@pytest.mark.asyncio
async def test_api_get_financial_records_parsed():
    td = _new_tdxdata()
    zip_bytes = _make_financial_zip(_valid_two_record_dat())
    with mock.patch.object(td, "get_file",
                           new=mock.AsyncMock(return_value=zip_bytes)):
        recs = await td.get_financial_records_parsed("tdxfin/gpcw20260331.zip")
    assert len(recs) == 2
    assert recs[0].report_date == 20260331  # 取自文件名


@pytest.mark.asyncio
async def test_api_get_financial_records_parsed_empty_zip_data():
    td = _new_tdxdata()
    with mock.patch.object(td, "get_file",
                           new=mock.AsyncMock(return_value=b"")):
        assert await td.get_financial_records_parsed("tdxfin/gpcw20260331.zip") == []


@pytest.mark.asyncio
async def test_api_get_financial_records_parsed_no_dat_member():
    td = _new_tdxdata()
    zip_bytes = _make_financial_zip(b"whatever", inner_name="readme.txt")
    with mock.patch.object(td, "get_file",
                           new=mock.AsyncMock(return_value=zip_bytes)):
        assert await td.get_financial_records_parsed("nodate.zip") == []


@pytest.mark.asyncio
async def test_api_get_financial_records_parsed_no_date_in_name():
    """文件名无 8 位日期 -> 回退用 .dat 头部报告期。"""
    td = _new_tdxdata()
    zip_bytes = _make_financial_zip(_valid_two_record_dat())
    with mock.patch.object(td, "get_file",
                           new=mock.AsyncMock(return_value=zip_bytes)):
        recs = await td.get_financial_records_parsed("gpcw.zip")
    assert recs[0].report_date == 20260331
