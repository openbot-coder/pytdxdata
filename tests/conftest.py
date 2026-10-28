"""测试全局隔离。

默认关掉两个**后台网络行为**：
- 权威小数位表同步：否则任何调用 ``TdxData.start()`` 的用例都会顺手发起证券
  清单请求（26 + 25 个分页），测试变慢且依赖网络。
- 当日服务器测速：后台 task 会真的去连主机取K线，会污染「往返次数」这类
  断言（实测同一个用例的 ``calls`` 在 3/4 之间随机跳），也会拖慢整体。

需要验证这两个行为本身的用例（``test_decimal_sync.py`` / ``test_probe_cache.py``）
自己 ``delenv`` 后再测。
"""
from __future__ import annotations

import os

import pytest

_ENV_KEYS = ("PYPYTDX_DATA_DISABLE_DECIMAL_SYNC", "PYPYTDX_DATA_DISABLE_PROBE")


@pytest.fixture(autouse=True, scope="session")
def _disable_background_network():
    old = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ[k] = "1"
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
