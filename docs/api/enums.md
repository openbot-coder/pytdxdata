# 枚举速查

!!! danger "先看这里——本项目最容易踩的坑"
    通达信的枚举取值**完全没有直觉**：`KlinePeriod.DAY` 是 **4** 不是 0 也不是 3，
    `ExMarket.HK_MAIN_BOARD` 是 **31**，`Market.SZ` 是 **0**。
    写死数字几乎必错，**请一律使用枚举成员**。

```python
from pytdxdata.models import Adjust, ExMarket, KlinePeriod, Market
```

## Market — 普通市场

| 成员 | 值 | 说明 |
|---|---|---|
| `Market.SZ` | `0` | 深圳 |
| `Market.SH` | `1` | 上海 |
| `Market.BJ` | `2` | 北京（北交所） |

!!! warning "代码不唯一"
    **市场 + 代码**才唯一。`(SH, "000001")` 是上证指数，`(SZ, "000001")` 是平安银行。
    所有接口的第一个参数都是 `market`，就是为了消歧。

## KlinePeriod — K 线周期

| 成员 | 值 | 说明 |
|---|---|---|
| `KlinePeriod.MIN_5` | `0` | 5 分钟 |
| `KlinePeriod.MIN_15` | `1` | 15 分钟 |
| `KlinePeriod.MIN_30` | `2` | 30 分钟 |
| `KlinePeriod.MIN_60` | `3` | 60 分钟 |
| `KlinePeriod.DAY` | `4` | **日线（最常用）** |
| `KlinePeriod.WEEK` | `5` | 周线 |
| `KlinePeriod.MONTH` | `6` | 月线 |
| `KlinePeriod.MIN_1` | `7` | 1 分钟 |
| `KlinePeriod.MIN_3` | `8` | 3 分钟 |
| `KlinePeriod.YEAR` | `9` | 年线 |
| `KlinePeriod.SEASON` | `10` | 季线 |
| `KlinePeriod.YEAR_ALT` | `11` | 另一套年线编码 |

取值**与通达信协议字节一致**（`KlineCategory`）。附带一个属性：

```python
KlinePeriod.MIN_1.is_minute    # True
KlinePeriod.DAY.is_minute      # False
```

!!! warning "`MIN_1` 会换通道"
    `MIN_1` 被强制路由到 **MAC 通道**并采用**单连接串行分页**，
    避免并发分页在多服务器深度不一致时产生数据空洞。
    详见 [通道与数据源](../internals/routing.md)。

## Adjust — 复权方式

| 成员 | 值 | 说明 |
|---|---|---|
| `Adjust.NONE` | `0` | 不复权（**默认**） |
| `Adjust.QFQ` | `1` | 前复权 |
| `Adjust.HFQ` | `2` | 后复权 |

`adjust != NONE` 时 K 线请求会切到 **MAC 通道**（标准通道不提供复权数据）。

!!! tip "和券商软件对不上，先差这里"
    很多券商前端默认前复权，而本包默认 `NONE`。对比数据前先确认两边复权方式一致。

## ExMarket — 扩展市场 {#exmarket}

港股 / 美股 / 期货 / 期权等走通达信 **EX 通道（端口 7727）**，`market` 参数传 `ExMarket` 成员。

### 常用值速查

| 成员 | 值 | 说明 |
|---|---|---|
| `ExMarket.SH_STOCK_OPTION` | `8` | 沪股票期权 |
| `ExMarket.SZ_STOCK_OPTION` | `9` | 深股票期权 |
| `ExMarket.ZZ_FUTURES` | `28` | 郑商所期货 |
| `ExMarket.DL_FUTURES` | `29` | 大商所期货 |
| `ExMarket.SH_FUTURES` | `30` | 上期所期货 |
| `ExMarket.HK_MAIN_BOARD` | `31` | **港股主板（最常用）** |
| `ExMarket.NEEQ` | `44` | 新三板 |
| `ExMarket.CFFEX_FUTURES` | `47` | 中金所期货（股指 / 国债） |
| `ExMarket.HK_GEM` | `48` | 港股创业板 |
| `ExMarket.GZ_FUTURES` | `66` | 广期所期货 |
| `ExMarket.GE_STOCK` | `73` | 德国股票 |
| `ExMarket.US_STOCK` | `74` | **美股（最常用）** |

```python
from pytdxdata.models import ExMarket, KlinePeriod

await td.get_kline(ExMarket.HK_MAIN_BOARD, "00700", KlinePeriod.DAY, count=700)   # 腾讯
await td.get_kline(ExMarket.US_STOCK, "AAPL", KlinePeriod.DAY, count=700)         # 苹果
await td.get_kline(ExMarket.CFFEX_FUTURES, "IFL0", KlinePeriod.DAY, count=700)    # 股指期货
await td.get_kline(ExMarket.SH_STOCK_OPTION, "10010971", KlinePeriod.DAY, count=700)
```

### 完整成员列表

以下由 `mkdocstrings` 从源码实时渲染，**以源码为准**：

::: pytdxdata.models.ExMarket
    options:
      show_root_heading: false
      show_root_toc_entry: false
      members_order: source

!!! note "美股期权 / 港股期权不在其中"
    通达信协议内**没有**美股、港股期权数据。美股期权需要走 CBOE 公开接口。

## 下一步

- [数据模型](models.md) — 返回对象字段
- [通道与数据源](../internals/routing.md) — 每个枚举对应哪台服务器
