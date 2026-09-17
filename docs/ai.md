# AI 助手接入

pytdxdata 的枚举取值**完全没有直觉**（`KlinePeriod.DAY` 是 `4` 不是 `0`），
所以 AI 助手凭常识写代码**几乎必错**。为此项目提供两条 AI 通道，分工不同：

| 文件 | 面向 | 内容 |
|---|---|---|
| [`llms.txt`](https://openbot-coder.github.io/pytdxdata/llms.txt) | AI 爬虫 / 问答型模型 | 心智模型、能力分组、关键陷阱，自动随站点发布 |
| [`SKILL.md`](https://github.com/openbot-coder/pytdxdata/blob/main/SKILL.md) | AI **编程**助手 | 必守约定、枚举取值表、可运行模板、任务→方法决策表、11 条陷阱 |

`llms.txt` 解决「这个库能干什么」，`SKILL.md` 解决「代码该怎么写对」。

## 装载 SKILL.md

把文件放进你所用的 AI 助手的 skills 目录即可：

```bash
# WorkBuddy / Claude Code 风格
mkdir -p ~/.workbuddy/skills/pytdxdata
curl -o ~/.workbuddy/skills/pytdxdata/SKILL.md \
  https://raw.githubusercontent.com/openbot-coder/pytdxdata/main/SKILL.md
```

也可以直接在提示词里附上该文件的 URL，或把它作为项目级 skill 放进你的仓库。

## 里面有什么

- **三条必守约定** —— 唯一入口是 `TdxData` 且必须 `async with`；标的是字符串（如 `sh600000`）
  而不是 `(market, code)` 元组；返回值是 `list[dataclass]` 而不是 DataFrame
- **枚举取值表** —— `Market` / `KlinePeriod` / `Adjust` / `ExMarket` 的准确取值
- **可运行模板** —— K 线、复权、报价、逐笔、分时、批量，全部是能直接跑的代码
- **「想做什么 → 用哪个方法」决策表** —— 18 个常见需求的直接映射
- **11 条真实陷阱** —— 每条都注明后果和正确做法（如 `sh000001` 是上证指数、`sz000001` 才是平安银行）

## 为什么值得让 AI 读

同一个 6 位代码在不同市场含义不同、枚举值与直觉完全脱节、复权/1 分钟线会静默切换通道——
这些都不是「读一眼签名就能猜到」的信息。不加载技能，AI 通常会写出：

```python
# ❌ 几种典型错误
await td.get_bars("sh600000", 4, count=240)      # 4 是数字字面量；应用 KlinePeriod.DAY
await td.get_bars((Market.SH, "600000"))         # 统一接口只接受字符串标的，不接受元组
td.get_bars("sh600000")                          # 忘了 await，且没在 async with 内
df = bars.to_dataframe()                         # 返回值是 list[dataclass]，没有这个方法
```

装载技能后这类错误基本消失。

## 给文档/模型作者的提示

改接口后请同步这几处（CI 会自动校验漂移）：

- 手写清单（必须列全）：[`docs/api/index.md`](api/index.md)、[`docs/API_REFERENCE.md`](API_REFERENCE.md)
- 入口与教程（只要求无过时引用）：`SKILL.md`、`README.md`、`llms.txt`、`getting-started`、`cookbook`、`faq`、`cli`

校验命令：

```bash
python scripts/check_docs.py        # 退出码 1 = 存在漂移
python scripts/check_docs.py --list # 打印全部公开方法名
```

## 下一步

- [接口总览](api/index.md) — 有哪些能力
- [TdxData 方法](api/tdxdata.md) — 完整签名（docstring 自动生成）
- [枚举速查](api/enums.md) — 参数到底该填几
