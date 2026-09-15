"""命令基类：请求构造 + 响应解析（不含IO）"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class BaseCommand(ABC, Generic[T]):
    """所有行情命令的基类。子类实现 build_request/parse_response。"""

    @abstractmethod
    def build_request(self) -> bytes:
        """构造请求包（含完整帧头）。"""
        ...

    @abstractmethod
    def parse_response(self, body: bytes) -> T:
        """解析解压后的响应 body，返回强类型结果。"""
        ...


class TdxDecodeError(Exception):
    """协议解析错误（服务器截断/ret_count撒谎等）。"""


def build_standard_request(
    cmd_id: int,
    method: int,
    payload: bytes,
    seq_id: int = 0x01016408,
) -> bytes:
    """构造标准通道请求帧：12字节头 + payload。"""
    import struct
    total = 12 + len(payload)
    header = struct.pack("<HIHHHH", cmd_id, seq_id, total, total, method, 0)
    return header + payload
