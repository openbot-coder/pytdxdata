"""测试全局隔离。

默认关掉**权威小数位表的后台同步** —— 否则任何调用 ``TdxData.start()`` 的用例都会
顺手发起证券清单请求（26 + 25 个分页），测试会变慢且依赖网络。
需要验证同步行为本身的用例（``test_decimal_sync.py``）自己 ``delenv`` 后再测。
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _disable_decimal_sync():
    old = os.environ.get("PYPYTDX_DATA_DISABLE_DECIMAL_SYNC")
    os.environ["PYPYTDX_DATA_DISABLE_DECIMAL_SYNC"] = "1"
    yield
    if old is None:
        os.environ.pop("PYPYTDX_DATA_DISABLE_DECIMAL_SYNC", None)
    else:
        os.environ["PYPYTDX_DATA_DISABLE_DECIMAL_SYNC"] = old
