"""协议层单元测试：帧/价格差分/浮点/命令解析（黄金字节向量，对照 easy-tdx 已验证实现）"""
from __future__ import annotations

import struct
from collections import Counter
from datetime import date, datetime

import pytest

from pytdxdata.models import Adjust, KlinePeriod, SecurityBar
from pytdxdata.protocol.commands.ex_proto import GetExInstrumentBarsCmd
from pytdxdata.protocol.commands.kline import GetSecurityBarsCmd
from pytdxdata.protocol.commands.mac import MacKlineCmd
from pytdxdata.protocol.commands.minute import GetHistoryMinuteTimeDataCmd
from pytdxdata.protocol.commands.quotes import price_decimal_digits
from pytdxdata.protocol.commands.transaction import MacTransactionCmd
from pytdxdata.protocol.frame import FrameHeader, decompress_body, parse_header
from pytdxdata.protocol.price import decode_ohlc_diffs, get_price, put_price
from pytdxdata.protocol.volume import decode_volume

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


def test_mac_transaction_parse_negative_trade_count_auction():
    """集合竞价段 trade_count 是有符号 int32，负数不能被读成 42 亿（BUG 回归）。

    实测（2026-10-21，sh600000 09:15:03）：18 字节为
      17820000 0ad71b41 d9000000 96fcffff 0800
    最后四字节前的 96fcffff = int32 -874，旧解析按 uint32 得 4294966422。
    """
    body = bytearray(39 + 2 * 18)
    struct.pack_into("<H", body, 29, 2)
    # 第1条：集合竞价，trade_count = -874
    struct.pack_into("<IfIiH", body, 39, 33303, 9.74, 217, -874, 8)
    # 第2条：正常交易，trade_count = 3
    struct.pack_into("<IfIiH", body, 39 + 18, 34200, 9.75, 100, 3, 1)

    cmd = MacTransactionCmd(1, "600000", 20261021, 0, 100)
    recs = cmd.parse_response(bytes(body))
    assert len(recs) == 2
    assert recs[0].trade_count == -874, "负值必须保留符号，不能变成 4294966422"
    assert recs[0].bs_flag == 8
    assert recs[1].trade_count == 3
    # 正常交易段的量级不受影响
    assert max(r.trade_count for r in recs) == 3


# ---------- 跨时区时间戳归一化（美股 EX 通道） ----------
#
# 通达信 EX 通道对美股返回的时间戳把两个时区拼在一起：日期字段是**美东交易日**、
# 时分字段是**北京时间**。直拼出的 datetime 会把同一交易日的下午半场（北京
# 00:00~04:00）排到上午半场（北京 21:30~23:59）之前，单日 390 根全部错位。

def _bar(market: int, y: int, mo: int, d: int, h: int, mi: int) -> SecurityBar:
    return SecurityBar(market=market, code="X", open=1.0, high=1.0, low=1.0,
                       close=1.0, vol=1.0, amount=1.0,
                       year=y, month=mo, day=d, hour=h, minute=mi)


def test_us_evening_segment_keeps_date():
    # 北京 21:31 ~ 23:59 与美东交易日属同一自然日，日期不动
    b = _bar(74, 2026, 10, 12, 21, 31)
    assert b.datetime == datetime(2026, 10, 12, 21, 31)
    assert b.trading_date == "2026-10-12"


def test_us_midnight_segment_rolls_to_next_day():
    # 北京次日 00:00 ~ 04:00 实际是美东 12:00~16:00，日期要 +1 天
    b = _bar(74, 2026, 10, 12, 4, 0)
    assert b.datetime == datetime(2026, 10, 13, 4, 0)
    assert (b.hour, b.minute) == (4, 0)          # 时分不参与归一化
    assert b.trading_date == "2026-10-12"        # 仍归属美东 10-12 交易日


def test_us_split_hour_boundary():
    assert _bar(74, 2026, 10, 12, 11, 59).day == 13
    assert _bar(74, 2026, 10, 12, 12, 0).day == 12


def test_us_month_and_year_rollover():
    assert _bar(74, 2026, 10, 31, 4, 0).datetime == datetime(2026, 11, 1, 4, 0)
    assert _bar(74, 2026, 12, 31, 4, 0).datetime == datetime(2027, 1, 1, 4, 0)


def test_cn_and_hk_bars_untouched():
    cn = _bar(1, 2026, 10, 12, 9, 31)            # A股/标准通道
    assert (cn.year, cn.month, cn.day) == (2026, 10, 12)
    assert cn.trading_date == "2026-10-12"
    hk = _bar(31, 2026, 10, 12, 4, 0)            # 港股同为 UTC+8，凌晨段不应 +1
    assert (hk.year, hk.month, hk.day) == (2026, 10, 12)
    assert hk.trading_date == "2026-10-12"


def test_us_full_session_is_monotonic_and_single_trading_day():
    # 实测口径：北京 21:31~23:59（149 根）+ 次日 00:00~04:00（241 根）= 390 根
    mins = list(range(21 * 60 + 31, 24 * 60)) + list(range(4 * 60 + 1))
    assert len(mins) == 390
    bars = [_bar(74, 2026, 10, 12, m // 60, m % 60) for m in mins]
    bars.sort(key=lambda b: b.datetime)
    dts = [b.datetime for b in bars]
    assert dts == sorted(dts)                    # 归一化后严格单调
    assert dts[0] == datetime(2026, 10, 12, 21, 31)
    assert dts[-1] == datetime(2026, 10, 13, 4, 0)
    # 全部归属于同一个美东交易日
    assert {b.trading_date for b in bars} == {"2026-10-12"}
    # 但横跨两个北京自然日（这正是不能用 datetime.date() 聚合的原因）
    by_day = Counter(b.datetime.date() for b in bars)
    assert by_day[date(2026, 10, 12)] == 149
    assert by_day[date(2026, 10, 13)] == 241


# ---------- MAC 通道 K线（0x122E）同样归一化 ----------

def _mac_bars_body(records: list[tuple[int, int]]) -> bytes:
    """构造 0x122E 响应体：33B头(count@24) + 每条36B(4B ymd + 4B time秒 + 7float)。"""
    body = bytearray(33 + len(records) * 36)
    struct.pack_into("<HBHI", body, 24, 7, 1, len(records), 0)
    for i, (ymd, tsec) in enumerate(records):
        struct.pack_into("<II7f", body, 33 + i * 36,
                         ymd, tsec, 10.0, 10.5, 9.5, 10.2, 0.0, 1000.0, 0.0)
    return bytes(body)


def test_mac_kline_us_bar_normalized():
    # _probe_bar（日期区间二分依赖它）对美股走的就是这条命令
    cmd = MacKlineCmd(74, "AAPL", KlinePeriod.MIN_1, Adjust.NONE, 0, 5)
    bars = cmd.parse_response(_mac_bars_body([(20261012, 4 * 3600)]))  # 北京 04:00
    assert len(bars) == 1
    assert bars[0].datetime == datetime(2026, 10, 13, 4, 0)
    assert bars[0].trading_date == "2026-10-12"


def test_mac_kline_hk_bar_untouched():
    cmd = MacKlineCmd(31, "00700", KlinePeriod.MIN_1, Adjust.NONE, 0, 5)
    bars = cmd.parse_response(_mac_bars_body([(20260729, 13 * 3600 + 40 * 60)]))
    assert (bars[0].datetime) == datetime(2026, 7, 29, 13, 40)
    assert bars[0].trading_date == "2026-07-29"


# ---------- 排序：EX 返回顺序按时间正序（切片取「最新 N 根」依赖它） ----------

def test_finish_bars_sorts_us_session_chronologically():
    from pytdxdata.api import TdxData
    from pytdxdata.symbols import parse_symbols

    sym = parse_symbols("usAAPL")[0]
    # 故意按错误的顺序给（凌晨段在前）
    out = TdxData._finish_bars([_bar(74, 2026, 10, 12, 4, 0),
                                _bar(74, 2026, 10, 12, 21, 31)], sym)
    assert [b.datetime for b in out] == [datetime(2026, 10, 12, 21, 31),
                                         datetime(2026, 10, 13, 4, 0)]
    assert {b.symbol for b in out} == {"usAAPL"}

# ---------- 报价小数位：沪市指数必须 2 位（回归：曾整体少 10 倍） ----------

@pytest.mark.parametrize("market,code,expect", [
    # 沪市指数：回归 bug —— 曾被判 3 位，导致价格少 10 倍
    (1, "000001", 2),   # 上证指数
    (1, "000016", 2),   # 上证50
    (1, "000300", 2),   # 沪深300
    (1, "000688", 2),   # 科创50
    (1, "880001", 2),   # 板块指数（8 开头，曾被判 3 位）
    (1, "880305", 2),
    # 沪市 ETF/基金：仍是 3 位
    (1, "510300", 3),
    (1, "588000", 3),
    (1, "511010", 3),
    (1, "501018", 3),
    # 沪市股票：2 位
    (1, "600000", 2),
    (1, "601398", 2),
    (1, "688111", 2),   # 科创板
    # 深市
    (0, "399001", 2),   # 深证成指
    (0, "399006", 2),   # 创业板指
    (0, "000001", 2),   # 平安银行（不是指数！）
    (0, "300750", 2),
    (0, "159915", 3),   # 深市 ETF
    # 债券/回购：2026-10-22 修正（沪市曾少 100 倍、深市曾少 10 倍）
    (1, "019742", 4),   # 沪市国债 24特国01（110.228，曾读成 11022.8）
    (1, "019547", 4),   # 沪市国债
    (1, "010706", 4),   # 沪市老国债 07国债06
    (1, "018014", 4),   # 沪市 018 段
    (1, "020812", 4),   # 沪市贴债
    (1, "110077", 4),   # 沪市可转债 洪城转债（199.279，曾读成 19927.9）
    (1, "113616", 4),   # 沪市可转债 韦尔转债
    (1, "118031", 4),   # 沪市可转债
    (1, "111000", 4),   # 沪市 111 段
    (1, "132024", 4),   # 沪市可交换债
    (1, "184832", 4),   # 沪市 184 段
    (1, "204001", 4),   # 沪市国债逆回购 GC001（1.385，曾读成 138.5）
    (1, "204028", 4),
    (0, "123107", 4),   # 深市可转债 温氏转债（112.189，曾读成 1121.89）
    (0, "128136", 4),   # 深市可转债 立讯转债
    (0, "127025", 4),   # 深市可转债
    (0, "131810", 4),   # 深市国债逆回购 R-001（1.38，曾读成 13.8）
    (0, "148004", 4),   # 深市 148 段
    (0, "112001", 4),   # 深市公司债
    (0, "101917", 4),   # 深市国债
    (0, "520261", 4),   # 深市 520 段
    # 沪市 B 股：3 位（2026-10-22 修正，曾按 2 位少 10 倍）
    (1, "900901", 3),   # 云赛Ｂ股
    (1, "900948", 3),
    # 深市 B 股：2 位（与沪市不同！）
    (0, "200011", 2),   # 深物业B
    (0, "200488", 2),
    # 边界：999xxx 是「指数」，不能被 9 开头当成 B 股
    (1, "999999", 2),   # 上证指数
    (1, "999998", 2),   # Ａ股指数
])
def test_price_decimal_digits(market, code, expect):
    assert price_decimal_digits(market, code) == expect


def test_price_decimal_digits_strips_nul():
    assert price_decimal_digits(1, "000016\x00\x00") == 2


# ---------- 美股分时跨日：hour 必须是合法时钟值（回归：曾出现 hour=27） ----------

def _us_minutes(n: int = 390) -> list:
    """构造 usAAPL 的 390 条分时：从北京 21:30 起连续 n 分钟。"""
    from pytdxdata.models import MinuteBar

    start = 21 * 60 + 30
    return [
        MinuteBar(price=100.0 + i * 0.01, vol=1.0,
                  hour=(start + i) // 60, minute=(start + i) % 60,
                  market=74, code="AAPL", date=20261022)
        for i in range(n)
    ]


def test_minute_bar_us_hour_is_valid_clock_value():
    """EX 分时的 minutes 是「自开盘起的连续分钟数」，直接 //60 会得到 hour=27。"""
    bars = _us_minutes()
    assert all(0 <= b.hour < 24 for b in bars)
    # 归一化前的原始值确实越界（21:30 + 390min = 28:00）
    assert max(b.hour + b.day_offset * 24 for b in bars) > 23


def test_minute_bar_us_day_offset_marks_next_day():
    bars = _us_minutes()
    assert bars[0].day_offset == 0          # 21:30 当晚
    assert bars[-1].day_offset == 1         # 03:59 次日
    assert (bars[-1].hour, bars[-1].minute) == (3, 59)


def test_minute_bar_us_datetime_is_monotonic_across_midnight():
    bars = _us_minutes()
    dts = [b.datetime for b in bars]
    assert dts == sorted(dts)
    assert dts[0] == datetime(2026, 10, 22, 21, 30)
    assert dts[-1] == datetime(2026, 10, 23, 3, 59)


def test_minute_bar_us_trading_date_spans_two_calendar_days():
    """一个美股交易日的分时横跨两个北京自然日，trading_date 必须都是同一天。"""
    bars = _us_minutes()
    tds = {b.trading_date for b in bars}
    assert tds == {"2026-10-22"}
    # 而 datetime.date() 会给出两天 —— 这正是不能用它的原因
    assert len({b.datetime.date() for b in bars}) == 2


def test_minute_bar_cn_unchanged():
    """A股/港股分时 hour 本就合法，归一化必须零行为变化。"""
    from pytdxdata.models import MinuteBar

    for hour, minute in ((9, 30), (11, 29), (13, 0), (14, 59)):
        b = MinuteBar(price=10.0, vol=1.0, hour=hour, minute=minute,
                      market=1, code="600000", date=20261022)
        assert (b.hour, b.minute) == (hour, minute)
        assert b.day_offset == 0
        assert b.datetime == datetime(2026, 10, 22, hour, minute)
        assert b.trading_date == "2026-10-22"


def test_minute_bar_cn_datetime_monotonic_full_session():
    """A股全天 240 条（09:30~11:29 + 13:00~14:59）时间轴严格递增。"""
    from pytdxdata.models import MinuteBar

    bars = []
    for idx in range(240):
        if idx < 120:
            hour, minute = 9 + (30 + idx) // 60, (30 + idx) % 60
        else:
            hour, minute = 13 + (idx - 120) // 60, (idx - 120) % 60
        bars.append(MinuteBar(price=10.0, vol=1.0, hour=hour, minute=minute,
                              market=1, code="600000", date=20261022))
    dts = [b.datetime for b in bars]
    assert dts == sorted(dts)
    assert dts[0] == datetime(2026, 10, 22, 9, 30)
    assert dts[-1] == datetime(2026, 10, 22, 14, 59)
    assert {b.trading_date for b in bars} == {"2026-10-22"}
