"""MAC EX 扩展行情通道：login(0x2454) + 端口7727 + 帧首字节改写"""
from __future__ import annotations

import asyncio
import struct
import time

from ..protocol.commands.base import BaseCommand
from ..protocol.frame import HEADER_SIZE, decompress_body, parse_header

# 80字节 login body（来自 opentdx，easy-tdx 已验证）
_LOGIN_BODY = bytes(
    bytearray.fromhex(
        "e5bb1c2fafe52594" "1f32c6e5d53dfb41" "5b734cc9cdbf0ac9"
        "2021bfdd1eb06d22" "d008884c1611cb13" "78f6abd824d899d2"
        "1f32c6e5d53dfb41" "1f32c6e5d53dfb41" "a9325ac935dc0837"
        "335a16e4ce17c1bb"
    )
)


class MacExLoginCmd(BaseCommand[bool]):
    """EX扩展行情登录（0x2454，head_flag=0x01）。"""

    def build_request(self) -> bytes:
        inner = struct.pack("<H", 0x2454) + _LOGIN_BODY
        header = struct.pack("<BIBHH", 0x01, 0, 1, len(inner), len(inner))
        return header + inner

    def parse_response(self, body: bytes) -> bool:
        return len(body) >= 2  # 响应body非空即成功


class ExTdxConnection:
    """EX扩展行情连接：TCP(7727) + login + 帧首字节0x1C→0x01改写。

    扩展行情服务器要求先login，且请求帧head_flag为0x01（MAC为0x1C）。
    """

    def __init__(self, host: str, port: int = 7727, timeout: float = 10.0,
                 handshake: str = "login") -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.handshake = handshake  # "login"=0x2454 login 80B | "setup"=0x2454 setup 70B
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self.last_used: float = 0.0

    @property
    def alive(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout
        )
        if self.handshake == "setup":
            from ..protocol.commands.ex_proto import ExSetupCmd
            await self.execute(ExSetupCmd())
        else:
            await self.execute(MacExLoginCmd())

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._writer = None
            self._reader = None

    async def execute(self, cmd: BaseCommand) -> object:
        if not self.alive:
            raise ConnectionError(f"EX连接未建立: {self.host}")
        assert self._reader is not None and self._writer is not None
        try:
            request = cmd.build_request()
            if request and request[0] == 0x1C:
                request = b"\x01" + request[1:]  # EX模式首字节改写
            self._writer.write(request)
            await self._writer.drain()
            self.last_used = time.monotonic()
            header_buf = await self._recv_exact(HEADER_SIZE)
            header = parse_header(header_buf)
            raw_body = await self._recv_exact(header.zipsize)
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            raise ConnectionError(f"EX通信错误 {self.host}: {e}") from e
        body = decompress_body(header, raw_body)
        return cmd.parse_response(body)

    async def _recv_exact(self, n: int) -> bytes:
        assert self._reader is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = await asyncio.wait_for(
                self._reader.read(n - len(buf)), timeout=self.timeout
            )
            if not chunk:
                raise ConnectionError(f"EX连接被关闭: {self.host}")
            buf.extend(chunk)
        return bytes(buf)
