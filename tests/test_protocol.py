"""协议层单元测试：帧/价格差分/浮点/命令解析（黄金字节向量，对照 easy-tdx 已验证实现）"""
from __future__ import annotations

import struct

import pytest

from pytdxdata.protocol.frame import FrameHeader, decompress_body, parse_header
from pytdxdata.protocol.price import get_price, put_price, decode_ohlc_diffs
from pytdxdata.protocol.volume import decode_volume
from pytdxdata.protocol.commands.ex_proto import GetExInstrumentBarsCmd
from pytdxdata.protocol.commands.kline import GetSecurityBarsCmd
from pytdxdata.protocol.commands.minute import GetHistoryMinuteTimeDataCmd
from pytdxdata.protocol.commands.transaction import MacTransactionCmd


# ---------- 变长有符号整数 ----------

@pytest.mark.parametrize("value", [0, 1, -1, 63, 64, -64, 127, -128, 1000, -100000, 2**20])
def test_price_roundtrip(value: int):
    enc = put_price(value)
    dec, pos = get_price(enc, 0)
    assert dec == value
    assert pos == len(enc)


def test_price_negative_flag():
    # 首字节 bit6=符号
    enc = put_price(-5)
    assert enc[0] & 0x40
    dec, _ = get_price(enc, 0)
    assert dec == -5


# ---------- OHLC 差分还原 ----------

def test_decode_ohlc_diffs():
    # 手工构造：open_diff=100, close_diff=-20, high_diff=10, low_diff=-30
    body = bytearray()
    for v in (100, -20, 10, -30):
        body.extend(put_price(v))
    od, cd, hd, ld, pos = decode_ohlc_diffs(body, 0)
    assert (od, cd, hd, ld) == (100, -20, 10, -30)
    assert pos == len(body)


# ---------- 自定义4字节浮点 ----------

def test_decode_volume_zero():
    assert decode_volume(0) == 0.0


def test_decode_volume_known():
    # 经验值：0x00000001 应为极小正数（非0）
    assert decode_volume(1) > 0


# ---------- 响应帧头 ----------

def test_frame_header():
    # magic=7654321, 未压缩: zipsize==unzipsize
    buf = struct.pack("<IIIHH", 7654321, 0, 0, 100, 100)
    h = parse_header(buf)
    assert h.magic == 7654321
    assert h.zipsize == 100
    assert h.unzipsize == 100


def test_decompress_body_uncompressed():
    h = FrameHeader(7654321, 0, 0, 5, 5)
    assert decompress_body(h, b"hello") == b"hello"


def test_decompress_body_zlib():
    import zlib as z
    raw = z.compress(b"x" * 100)
    h = FrameHeader(7654321, 0, 0, len(raw), 100)
    assert decompress_body(h, raw) == b"x" * 100


# ---------- K线命令 ----------

def test_kline_build_request():
    cmd = GetSecurityBarsCmd(1, "600000", 4, 0, 100)  # DAY
    req = cmd.build_request()
    assert len(req) == 38
    assert req[0:2] == struct.pack("<H", 0x010C)  # 命令ID
    # category 字节应等于 4 (DAY, 与 easy-tdx KlineCategory 一致)
    assert req[20] == 4


def test_kline_parse_empty_response():
    cmd = GetSecurityBarsCmd(1, "600000", 5, 0, 100)
    with pytest.raises(Exception):
        # 2字节 header 声称100条但body为空 → 解析失败
        cmd.parse_response(b"\x64\x00")


# ---------- 分时命令（0x0130 历史分时） ----------

def test_history_minute_parse():
    # 响应: num(2B) + 4B头(skip=6 从num后算起) + 每条3变长字段(price_diff, unknown, vol)
    body = bytearray(struct.pack("<H", 3) + b"\x00" * 4)
    for pd, unk, v in ((914, 0, 553), (-1, 0, 784), (2, 0, 4157)):
        body.extend(put_price(pd))
        body.extend(put_price(unk))
        body.extend(put_price(v))
    cmd = GetHistoryMinuteTimeDataCmd(1, "600000", 20260813)
    bars = cmd.parse_response(bytes(body))
    assert [round(b.price, 2) for b in bars] == [9.14, 9.13, 9.15]
    assert [int(b.vol) for b in bars] == [553, 784, 4157]
    assert bars[0].hour == 9 and bars[0].minute == 30
    assert bars[2].hour == 9 and bars[2].minute == 32


# ---------- EX K线（0x23FF）日期解码 ----------

def _ex_bars_body(raw_dates: list[int]) -> bytes:
    """构造 0x23FF 响应体：20B头(count@18) + 每条32B(4B时间 + 28B行情)。"""
    body = bytearray(20 + len(raw_dates) * 32)
    struct.pack_into("<H", body, 18, len(raw_dates))
    for i, raw in enumerate(raw_dates):
        off = 20 + i * 32
        struct.pack_into("<I", body, off, raw)
        struct.pack_into("<ffffIIf", body, off + 4, 10.0, 10.5, 9.5, 10.2, 0, 1000, 0)
    return bytes(body)


def test_ex_bars_minute_datetime():
    # category=0(分钟级): 4字节=2B zipday + 2B tminutes
    # 53785305 = tminutes 820(13:40) + zipday 45785(2026-07-29)；旧解析误读为 5378-53-05
    raw = struct.unpack("<I", struct.pack("<HH", 45785, 820))[0]
    assert raw == 53785305
    cmd = GetExInstrumentBarsCmd(31, "00700", 0, 0, 5)
    bars = cmd.parse_response(_ex_bars_body([raw]))
    assert len(bars) == 1
    assert (bars[0]["year"], bars[0]["month"], bars[0]["day"]) == (2026, 7, 29)
    assert (bars[0]["hour"], bars[0]["minute"]) == (13, 40)
    assert bars[0]["close"] == pytest.approx(10.2)


def test_ex_bars_day_datetime():
    # category=4(日线): 4字节 YYYYMMDD，hour=15
    cmd = GetExInstrumentBarsCmd(31, "00700", 4, 0, 5)
    bars = cmd.parse_response(_ex_bars_body([20260729]))
    assert len(bars) == 1
    assert (bars[0]["year"], bars[0]["month"], bars[0]["day"]) == (2026, 7, 29)
    assert (bars[0]["hour"], bars[0]["minute"]) == (15, 0)


# ---------- MAC 逐笔命令 ----------

def test_mac_transaction_request():
    cmd = MacTransactionCmd(0, "000001", 20260811, 0, 100)
    req = cmd.build_request()
    assert len(req) == 10 + 2 + 44  # 10头 + 2msg_id + body
    assert req[0] == 0x1C  # head_flag


def test_mac_transaction_parse():
    # 构造响应: 39字节头(count@29=2) + 2条×18字节
    body = bytearray(39 + 2 * 18)
    struct.pack_into("<H", body, 29, 2)
    for i, (ts, price, vol, tc, flag) in enumerate([
        (34200, 11.25, 100, 3, 1),   # 09:30:00
        (34201, 11.26, 50, 1, 0),
    ]):
        off = 39 + i * 18
        struct.pack_into("<IfIIH", body, off, ts, price, vol, tc, flag)
    cmd = MacTransactionCmd(0, "000001", 20260811, 0, 100)
    recs = cmd.parse_response(bytes(body))
    assert len(recs) == 2
    assert recs[0].time == "09:30:00"
    assert recs[0].price == 11.25
    assert recs[0].vol == 100
    assert recs[0].trade_count == 3
    assert recs[0].bs_flag == 1
    assert recs[1].trade_count == 1
