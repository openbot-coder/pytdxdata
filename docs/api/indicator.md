# 技术指标

纯函数实现，**不依赖 pandas / numpy**，输入 `list[float]` 输出 `list[float]`（或元组），
可直接在任意序列上用。也被 `TdxData.get_stock_kline_with_indicators()` 内部调用。

```python
from pytdxdata import indicator
```

## 快速示例

```python
closes = [b.close for b in bars]

macd_line, signal, hist = indicator.macd(closes)
rsi14 = indicator.rsi(closes, period=14)
upper, mid, lower = indicator.boll(closes, period=20, mult=2.0)
```

指标计算前端数据不足时，结果用 `None` 占位，长度与输入一致——**不会截断**，
方便直接和 K 线按下标对齐。

## API

::: pytdxdata.indicator
    options:
      members_order: source
      show_root_heading: false
      show_root_toc_entry: false
      filters:
        - "!^_"

## 与 get_stock_kline_with_indicators 的关系

服务端侧的指标请求走 MAC 通道（`get_stock_kline_with_indicators`，服务端算好返回）；
本地侧的指标走本模块。两者不互相依赖：

| 方式 | 位置 | 适用 |
|---|---|---|
| `get_stock_kline_with_indicators(...)` | 服务端计算 | 想少传数据、服务端算子齐全时 |
| `pytdxdata.indicator.*` | 本地计算 | 需要自定义参数、离线复算、和其他数据源混算 |
