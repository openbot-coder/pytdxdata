# TdxData 方法签名

> 本页由 `mkdocstrings` **从 `src/pytdxdata/api.py` 的 docstring 实时渲染**。
> 你看到的签名就是当前代码的签名——不需要手动维护，也就不会过期。

## 用法

```python
import asyncio
from pytdxdata import TdxData
from pytdxdata.models import KlinePeriod

async def main():
    async with TdxData() as td:
        bars = await td.get_bars("sh600000", KlinePeriod.DAY, count=240)
        print(bars[-1])

asyncio.run(main())
```

!!! tip "本页太长？"
    想看「有哪些能力」的地图，去 [接口总览](index.md)。
    本页是完整的签名字典，适合用浏览器 `Ctrl+F` 搜方法名。

## 完整定义

::: pytdxdata.TdxData
    options:
      members_order: source
      group_by_category: true
      show_category_heading: true
      show_root_heading: true
      show_root_full_path: false
      show_signature_annotations: true
      separate_signature: true
      merge_init_into_class: true
      docstring_section_style: list
      filters:
        - "!^_"

## 相关

- [枚举速查](enums.md) — 参数该填哪个成员
- [数据模型](models.md) — 返回值有哪些字段
- [场景示例](../cookbook.md) — 组合用法
