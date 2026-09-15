"""单条 asyncio TCP 连接：连接 + 3条握手 + 帧收发 + 命令执行"""
from __future__ import annotations

import asyncio
import time
from typing import Generic, TypeVar

from ..protocol.commands.base import BaseCommand
from ..protocol.commands.setup import SETUP_COMMANDS
from ..protocol.frame import HEADER_SIZE, decompress_body, parse_header

T = TypeVar("T")


class TdxConnection(Generic[T]):
    """一条通达信 TCP 连接（连接时自动握手，execute 发送命令并解析响应）。"""

    def __init__(self, host: str, port: int = 7709, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._last_result: T | None = None

    @property
    def alive(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout
        )
        await self._send_setup()

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._writer = None
            self._reader = None

    def _force_close(self) -> None:
        """强制中断底层连接（abort transport），用于超时/错误后让挂起的 read 立即失效。"""
        writer = self._writer
        self._writer = None
        self._reader = None
        if writer is not None:
            try:
                transport = writer.transport
                transport.abort()  # 立即 RST，解除任何挂起的 read()，避免协程无法被取消
            except (ConnectionError, OSError, AttributeError):
                try:
                    writer.close()
                except (ConnectionError, OSError):
                    pass

    async def ping(self) -> float | None:
        """连接+握手+发一条setup命令测延迟（秒）。失败返回 None。"""
        t0 = time.monotonic()
        try:
            await self.connect()
            assert self._writer is not None
            self._writer.write(SETUP_COMMANDS[0])
            await self._writer.drain()
            hdr_buf = await self._recv_exact(HEADER_SIZE)
            hdr = parse_header(hdr_buf)
            if hdr.zipsize > 0:
                await self._recv_exact(hdr.zipsize)
            return time.monotonic() - t0
        except (ConnectionError, OSError, asyncio.TimeoutError, ValueError):
            return None
        finally:
            await self.close()

    async def execute(self, cmd: BaseCommand[T]) -> T:
        """发送请求 → 读16B帧头 → 读body → 解压 → 命令解析。"""
        if not self.alive:
            raise ConnectionError(f"连接未建立或已关闭: {self.host}")
        assert self._reader is not None and self._writer is not None
        try:
            try:
                # 用手动 wait_for 包裹 IO，超时后强制关闭底层连接，
                # 避免 asyncio.timeout 与外层 wait_for 取消产生竞态导致协程无法中断。
                await asyncio.wait_for(self._io(cmd), self.timeout)
            except asyncio.TimeoutError:
                # 半开连接/服务器无响应：关闭连接使其真正失效，便于重试时换连接
                self._force_close()
                raise ConnectionError(f"通信超时 {self.host} ({self.timeout}s)") from None
            except (ConnectionError, OSError) as e:
                self._force_close()
                raise ConnectionError(f"通信错误 {self.host}: {e}") from e
        except (ConnectionError, OSError) as e:
            raise e
        return self._last_result  # type: ignore[return-value]

    async def _io(self, cmd: BaseCommand[T]) -> None:
        assert self._reader is not None and self._writer is not None
        self._writer.write(cmd.build_request())
        await self._writer.drain()
        header_buf = await self._recv_exact(HEADER_SIZE)
        header = parse_header(header_buf)
        raw_body = await self._recv_exact(header.zipsize)
        body = decompress_body(header, raw_body)
        self._last_result: T = cmd.parse_response(body)

    async def _send_setup(self) -> None:
        """按序发送3条握手命令并丢弃响应。"""
        assert self._reader is not None and self._writer is not None
        for cmd_bytes in SETUP_COMMANDS:
            try:
                async with asyncio.timeout(self.timeout):
                    self._writer.write(cmd_bytes)
                    await self._writer.drain()
                    hdr_buf = await self._recv_exact(HEADER_SIZE)
                    hdr = parse_header(hdr_buf)
                    if hdr.zipsize > 0:
                        await self._recv_exact(hdr.zipsize)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                # 部分服务器握手无响应，忽略
                pass

    async def _recv_exact(self, n: int) -> bytes:
        """循环读取直到 n 字节。"""
        assert self._reader is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = await asyncio.wait_for(
                self._reader.read(n - len(buf)), timeout=self.timeout
            )
            if not chunk:
                raise ConnectionError(f"连接被服务器关闭: {self.host}")
            buf.extend(chunk)
        return bytes(buf)
