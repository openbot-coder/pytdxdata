# 文件解析器

通达信服务器上除了行情，还有一批**静态文件**：板块成分、行业分类、历史专业财报。
这些文件是自定义二进制格式，本项目把解析器独立成 `pytdxdata.codec` 子模块：

- **纯标准库**（`struct` / `zipfile`），可以脱网单测
- **与网络层解耦**，可以直接拿本地文件字节调用
- 每个解析函数的 docstring 里都写明了**字节格式**，方便对照排错

```python
from pytdxdata.codec import parse_block_dat, parse_tdxhy_cfg, parse_financial_dat

blocks = parse_block_dat(open("block_gn.dat", "rb").read(), "block_gn.dat")
```

## 三类文件

| 文件 | 解析函数 | 产出 | 说明 |
|---|---|---|---|
| `block_zs/gn/fg.dat` | `parse_block_dat` | `list[TdxBlock]` | 板块成分股代码列表 |
| `tdxhy.cfg` | `parse_tdxhy_cfg` | `dict[str, IndustryInfo]` | 通达信行业 + 申万行业 |
| `gpcw.txt` | `parse_financial_file_list` | `list[FinancialFileInfo]` | 财报文件索引（文件名 / MD5 / 大小） |
| `gpcw*.zip` 内 `.dat` | `parse_financial_dat` | `list[FinancialRecord]` | 每股历史财报原始字段 |

!!! note "走网络时用 TdxData 的解析方法"
    如果不想自己管下载，直接用封装好的方法（内部就是调这些解析器）：

    `get_block_parsed()` / `get_industry_map()` / `get_financial_file_infos()` / `get_financial_records_parsed()`
    — 见 [接口总览 → 除权 / 财务 / F10 / 文件](index.md#file-methods)。

## 关键细节

**板块类型由文件名推断**，因为文件头里没有可靠的分类字段：

| 文件名包含 | category |
|---|---|
| `zs` | `0` 行业 |
| `gn` | `2` 概念 |
| `fg` | `3` 风格 |
| 其他 | `0` |

**单条板块记录最多 400 只成分股**（代码区 2800 字节 ÷ 每只 7 字节）。

**财报报告期的来源优先级**：文件名 → `.dat` 头部内嵌值。两个都拿不到才为空。

**市场字段**：`0` = 深，`1` = 沪。
> 这里曾经有过一个 bug——上游实现用 `<6s1c1L` 导致市场字段恒判为深市，
> 本包用 `<6sBL` 修正。见 [更新日志](../changelog.md)。

## API

::: pytdxdata.codec
    options:
      members_order: source
      show_root_heading: false
      show_root_toc_entry: false
      filters:
        - "!^_"

## 下一步

- [协议与命令](../internals/protocol.md) — 行情数据的线上协议格式
- [场景示例 → 板块成分离线解析](../cookbook.md#offline-block-snapshot)
