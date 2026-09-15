"""握手命令原始字节（连接建立后必须按序发送3条并丢弃响应）"""
from __future__ import annotations

SETUP_CMD1 = bytes.fromhex("0c0218930001030003000d0001")
SETUP_CMD2 = bytes.fromhex("0c0218940001030003000d0002")
SETUP_CMD3 = bytes.fromhex(
    "0c031899000120002000db0fd5d0c9ccd6a4a8af0000008fc22540130000d500c9ccbdf0d7ea00000002"
)
SETUP_COMMANDS: tuple[bytes, ...] = (SETUP_CMD1, SETUP_CMD2, SETUP_CMD3)
