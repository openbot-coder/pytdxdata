"""通达信 4 字节自定义浮点解码（成交量/股本专用）"""
from __future__ import annotations

from .._binary import unpack_from


def get_volume(data: bytes | bytearray | memoryview, pos: int) -> tuple[float, int]:
    """从 data[pos:pos+4] 解码成交量，返回 (volume_float, new_pos)。"""
    (ivol,) = unpack_from("<I", data, pos, "volume")
    return _decode_volume(ivol), pos + 4


def decode_volume(raw: int) -> float:
    """解码单个自定义浮点整数。"""
    return _decode_volume(raw)


def _decode_volume(ivol: int) -> float:
    if ivol == 0:
        return 0.0
    logpoint = (ivol >> 24) & 0xFF
    hleax = (ivol >> 16) & 0xFF
    lheax = (ivol >> 8) & 0xFF
    lleax = ivol & 0xFF

    exp = logpoint * 2 - 0x7F
    base = _pow2(exp)

    exp_h = logpoint * 2 - 0x86
    if hleax > 0x80:
        hi = _pow2(exp_h) * 128 + (hleax & 0x7F) * _pow2(exp_h + 1)
    else:
        hi = _pow2(exp_h) * hleax

    mid = _pow2(logpoint * 2 - 0x8E) * lheax
    lo = _pow2(logpoint * 2 - 0x96) * lleax
    if hleax & 0x80:
        mid *= 2.0
        lo *= 2.0
    return base + hi + mid + lo


def _pow2(exp: int) -> float:
    if exp >= 0:
        return float(1 << exp) if exp < 63 else 2.0**exp
    return 1.0 / (1 << (-exp)) if -exp < 63 else 2.0**exp
