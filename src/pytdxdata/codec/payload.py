"""缓存载荷序列化：records <-> bytes（pickle + zlib level1，性能优先）"""
from __future__ import annotations

import pickle
import zlib
from typing import Any


def pack_payload(obj: Any) -> bytes:
    """序列化缓存载荷（压缩）。"""
    return zlib.compress(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL), level=1)


def unpack_payload(data: bytes) -> Any:
    """反序列化缓存载荷。"""
    return pickle.loads(zlib.decompress(data))
