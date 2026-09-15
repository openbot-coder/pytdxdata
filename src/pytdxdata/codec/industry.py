"""通达信行业分类配置 (tdxhy.cfg) 解析。

文件为 GBK 文本，每行形如::

    市场|代码|通达信行业|||申万行业|...

只保留 6 位 A 股代码；通达信行业取第 3 列，申万行业取第 6 列（可能缺失）。
"""
from __future__ import annotations

from ..models import IndustryInfo


def parse_tdxhy_cfg(content: bytes) -> dict[str, IndustryInfo]:
    """解析 tdxhy.cfg 字节内容。

    Args:
        content: 文件完整字节（GBK 编码）。

    Returns:
        ``{6位代码: IndustryInfo}``；空输入或非法行会被跳过。
    """
    results: dict[str, IndustryInfo] = {}
    if not content:
        return results

    text = content.decode("gbk", errors="replace")
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        code = parts[1].strip()
        if len(code) != 6 or not code.isdigit():
            continue
        results[code] = IndustryInfo(
            code=code,
            tdx_industry=parts[2].strip(),
            sw_industry=parts[5].strip() if len(parts) >= 6 else "",
        )
    return results
