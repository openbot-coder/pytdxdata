"""pytdxdata: 通达信行情数据客户端（动态连接池 + 双层TTL缓存 + 3连接并发分页，纯asyncio）

支持 A股(沪/深/北) + 港股 + 美股 + 期货 + 期权，一套接口全市场。
"""
from .api import TdxData
from .models import (
    Adjust,
    ExMarket,
    FinanceRecord,
    IndexTradeStats,
    KlinePeriod,
    Market,
    MinuteBar,
    MinuteTradeStat,
    SecurityBar,
    SecurityQuote,
    TransactionRecord,
    XdxrRecord,
)

__version__ = "0.6.9"
__all__ = [
    "TdxData",
    "Market",
    "ExMarket",
    "KlinePeriod",
    "Adjust",
    "SecurityBar",
    "SecurityQuote",
    "TransactionRecord",
    "MinuteBar",
    "MinuteTradeStat",
    "IndexTradeStats",
    "XdxrRecord",
    "FinanceRecord",
    "__version__",
]
