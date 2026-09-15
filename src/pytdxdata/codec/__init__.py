"""服务器文件解析器：板块(.dat) / 行业(.cfg) / 历史专业财报(.zip+.dat)。

均为纯标准库实现（struct / zipfile），无第三方依赖，可独立单测。
"""
from __future__ import annotations

from .block import infer_block_category, parse_block_dat
from .financial import parse_financial_dat, parse_financial_file_list
from .industry import parse_tdxhy_cfg
from .payload import pack_payload, unpack_payload

__all__ = [
    "infer_block_category",
    "pack_payload",
    "parse_block_dat",
    "parse_financial_dat",
    "parse_financial_file_list",
    "parse_tdxhy_cfg",
    "unpack_payload",
]
