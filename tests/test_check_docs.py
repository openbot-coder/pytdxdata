"""`scripts/check_docs.py` 的自测。

防漂移脚本本身也需要被验证——一个永远返回 0 的检查等于没有检查。
这里锁定三类行为：反引号抽取规则、词边界、以及「包内模块级符号不算已删除」。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_docs.py"


def _load_check_docs():
    spec = importlib.util.spec_from_file_location("_check_docs", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_check_docs"] = module
    spec.loader.exec_module(module)
    return module


cd = _load_check_docs()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 裸方法名
        ("`get_kline`", {"get_kline"}),
        # 带签名的写法（README / API_REFERENCE 里大量出现）
        ("`get_kline(market, code, period)`", {"get_kline"}),
        ("`download_file(filename, filesize)`", {"download_file"}),
        # 同一行多个
        ("`get_quotes(stocks)` 和 `get_minute`", {"get_quotes", "get_minute"}),
        # 带对象前缀的不是方法引用
        ("`td.get_kline(1, '600000')`", set()),
        # 非 get_/download_ 前缀不匹配
        ("`list[SecurityBar]`", set()),
        # 没有反引号就不是引用
        ("get_kline 没有反引号", set()),
    ],
)
def test_collect_doc_references(text: str, expected: set[str]) -> None:
    assert cd.collect_doc_references(text) == expected


def test_mentioned_in_respects_word_boundary() -> None:
    """`get_kline` 不应误匹配 `get_kline_batch`。"""
    assert not cd.mentioned_in("用 `get_kline_batch` 批量拉取", "get_kline")
    assert cd.mentioned_in("用 `get_kline(m, c, p)` 拉取", "get_kline")


def test_package_level_functions_are_known_symbols() -> None:
    """config.py 的模块级函数必须被纳入白名单，否则会被误报成「已删除」。"""
    known = cd.collect_api_methods(cd.API_FILE)[0] | cd.collect_package_symbols()
    for name in (
        "get_standard_hosts",
        "get_mac_hosts",
        "get_ex_hosts",
        "get_ex_handshakes",
    ):
        assert name in known


def test_repository_docs_are_in_sync() -> None:
    """仓库当前状态必须无漂移（等价于 CI 里那一步）。"""
    assert cd.main() == 0
