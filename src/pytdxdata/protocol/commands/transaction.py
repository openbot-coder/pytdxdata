"""逐笔成交命令：标准通道（当日0x170801含笔数 / 历史0x013001）+ MAC通道（0x122F含trade_count）"""
from __future__ import annotations

import struct

from ..._binary import unpack_from
from ...models import TransactionRecord
from ...symbols import has_order_count
from ..datetime_ import get_time
from ..mac_frame import build_mac_request
from ..price import get_price
from .base import BaseCommand

STD_PAGE_SIZE = 800
MAC_PAGE_SIZE = 1000


class GetTransactionDataCmd(BaseCommand[list[TransactionRecord]]):
    """标准通道当日逐笔（0x170801）。协议含成交笔数(_num_orders)，此处显式解析暴露。"""

    def __init__(self, market: int, code: str, start: int, count: int = 800) -> None:
        self.market = market
        self.code = code.encode("utf-8")
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        header = bytes.fromhex("0c17080101010e000e00c50f".replace(" ", ""))
        return header + struct.pack("<H6sHH", self.market, self.code, self.start, self.count)

    def parse_response(self, body: bytes) -> list[TransactionRecord]:
        (num,) = unpack_from("<H", body, 0, "std transaction header")
        pos = 2
        last_price = 0
        records: list[TransactionRecord] = []
        code = self.code.decode("utf-8")
        has_tc = has_order_count(self.market, code)
        for _ in range(num):
            hour, minute, pos = get_time(body, pos)
            price_diff, pos = get_price(body, pos)
            vol, pos = get_price(body, pos)
            num_orders, pos = get_price(body, pos)   # 成交笔数（当日独有）
            bs_flag, pos = get_price(body, pos)
            _unknown, pos = get_price(body, pos)
            last_price += price_diff
            records.append(TransactionRecord(
                market=self.market, code=code,
                time=f"{hour:02d}:{minute:02d}:00",
                price=last_price / 100.0, vol=float(vol),
                # 指数/ETF/LOF 该字段是「价格×1000」占位，不是笔数
                trade_count=int(num_orders) if has_tc else 0,
                bs_flag=bs_flag, trade_count_available=has_tc,
            ))
        return records


class GetHistoryTransactionDataCmd(BaseCommand[list[TransactionRecord]]):
    """标准通道历史逐笔（0x013001，无笔数字段）。"""

    def __init__(self, market: int, code: str, date: int, start: int, count: int = 800) -> None:
        self.market = market
        self.code = code.encode("utf-8")
        self.date = date
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        header = bytes.fromhex("0c013001000112001200b50f".replace(" ", ""))
        return header + struct.pack("<IH6sHH", self.date, self.market, self.code, self.start, self.count)

    def parse_response(self, body: bytes) -> list[TransactionRecord]:
        (num,) = unpack_from("<H", body, 0, "hist transaction header")
        pos = 6  # 2(num) + 4(skip)
        last_price = 0
        records: list[TransactionRecord] = []
        code = self.code.decode("utf-8")
        for _ in range(num):
            hour, minute, pos = get_time(body, pos)
            price_diff, pos = get_price(body, pos)
            vol, pos = get_price(body, pos)
            bs_flag, pos = get_price(body, pos)
            _unknown, pos = get_price(body, pos)
            last_price += price_diff
            records.append(TransactionRecord(
                market=self.market, code=code,
                time=f"{hour:02d}:{minute:02d}:00",
                price=last_price / 100.0, vol=float(vol),
                trade_count=0, bs_flag=bs_flag,
                trade_count_available=False,   # 历史通道整条链路都没有笔数字段
            ))
        return records


class MacTransactionCmd(BaseCommand[list[TransactionRecord]]):
    """MAC通道逐笔（0x122F，每条18B固定：time+price+vol+trade_count+bs_flag，含成交笔数）。"""

    def __init__(self, market: int, code: str, ymd: int, start: int, count: int = 1000) -> None:
        self.market = market
        self.code = code
        self.ymd = ymd
        self.start = start
        self.count = count

    def build_request(self) -> bytes:
        body = struct.pack(
            "<H22sIIH10x",
            self.market,
            self.code.encode("gbk"),
            self.ymd,
            self.start,
            self.count,
        )
        return build_mac_request(0x122F, body)

    def parse_response(self, body: bytes) -> list[TransactionRecord]:
        (count,) = unpack_from("<H", body, 29, "mac transaction count")
        records: list[TransactionRecord] = []
        has_tc = has_order_count(self.market, self.code)
        for i in range(count):
            offset = 39 + i * 18
            try:
                # trade_count 必须按**有符号** int32 解析：集合竞价段（bs_flag=8）
                # 该字段实为负值，按 uint32 会读出 42 亿量级的脏数据。
                # 实测（2026-10-21 sh600000）：09:15:03 原始 96fcffff = -874。
                time_sec, price, volume, trade_count, bs_flag = unpack_from(
                    "<IfIiH", body, offset, "mac transaction item"
                )
            except ValueError:
                break
            h, m, s = time_sec // 3600, time_sec % 3600 // 60, time_sec % 60
            records.append(TransactionRecord(
                market=self.market, code=self.code,
                time=f"{h:02d}:{m:02d}:{s:02d}",
                price=price, vol=float(volume),
                # 指数/ETF/LOF 该字段是「价格×1000」占位，不是笔数
                trade_count=int(trade_count) if has_tc else 0,
                bs_flag=bs_flag, trade_count_available=has_tc,
            ))
        return records
