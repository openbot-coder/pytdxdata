"""命令注册表：命令类型 → (命令类, 通道, 页大小) 元数据"""
from __future__ import annotations

from .base import BaseCommand, TdxDecodeError, build_standard_request
from .finance import GetFinanceInfoCmd
from .kline import GetIndexBarsCmd, GetSecurityBarsCmd, PAGE_SIZE as KLINE_PAGE
from .mac import MacKlineCmd, MAC_KLINE_PAGE
from .minute import GetHistoryMinuteTimeDataCmd
from .quotes import GetSecurityQuotesCmd, MAX_QUOTES
from .security_list import GetSecurityCountCmd, GetSecurityListCmd, PAGE_SIZE as LIST_PAGE
from .transaction import (
    GetHistoryTransactionDataCmd,
    GetTransactionDataCmd,
    MacTransactionCmd,
    MAC_PAGE_SIZE,
    STD_PAGE_SIZE,
)

__all__ = [
    "BaseCommand", "TdxDecodeError", "build_standard_request",
    "GetSecurityBarsCmd", "GetIndexBarsCmd", "KLINE_PAGE",
    "GetSecurityQuotesCmd", "MAX_QUOTES",
    "GetSecurityCountCmd", "GetSecurityListCmd", "LIST_PAGE",
    "GetTransactionDataCmd", "GetHistoryTransactionDataCmd", "MacTransactionCmd",
    "STD_PAGE_SIZE", "MAC_PAGE_SIZE",
    "GetHistoryMinuteTimeDataCmd",
    "GetXdxrInfoCmd", "GetFinanceInfoCmd",
    "MacKlineCmd", "MAC_KLINE_PAGE",
]

# 循环依赖规避：xdxr 需要在此导入
from .xdxr import GetXdxrInfoCmd  # noqa: E402
