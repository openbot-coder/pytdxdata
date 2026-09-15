# 命令行工具

安装后自带 `tdx` 命令（等同 `python -m pytdxdata.cli`）。**19 个子命令**，覆盖全部主要能力，适合快速验证、脚本管道和临时取数。

```bash
tdx --help          # 查看全部子命令
tdx <cmd> --help    # 查看单个子命令参数
```

## 标的写法

几乎所有取行情的命令都接受同一个标的语法：

| 写法 | 含义 | 示例 |
|---|---|---|
| `sz000001` | 深市 | 平安银行 |
| `sh600000` | 沪市 | 浦发银行 |
| `bj920992` | 北交所 | — |
| `hk00700` | 港股 | 腾讯控股 |
| `usAAPL` | 美股 | 苹果 |
| `cffex:IFL0` | 期货（交易所前缀） | 中金所股指 |
| `000001` | **6 位纯数字 = 指数** | 上证指数 |

期货交易所前缀：`cffex:` 中金 / `shf:` 上期 / `zz:` 郑商 / `dl:` 大商 / `hk:` 港交所 / `gz:` 广期所。

## 行情

```bash
tdx quotes sz000001 sh600000 hk00700       # 五档报价（可多标的）
tdx info sz000001                          # 个股特征快照
tdx market-stat                            # 全市场统计（涨跌家数/总额/市值/涨跌停数）
```

## K 线

```bash
tdx kline sz000001 --period day --count 30 --adjust qfq
tdx kline sz000001 --period min1 --count 240
tdx kline cffex:IFL0 --count 30            # 期货
tdx index 000001 --count 30                # 指数
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `--period` | `day` | `min1` / `min3` / `min5` / `min15` / `min30` / `min60` / `day` / `week` / `month` / `season` / `year` |
| `--count` | `30` | 条数；`>800` 自动并发分页 |
| `--start` | `0` | 起始偏移（0 = 最新） |
| `--adjust` | `none` | `none` / `qfq`（前复权）/ `hfq`（后复权） |

## 逐笔与分时

```bash
tdx transactions sz000001 --count 50                # 今日逐笔（含成交笔数）
tdx transactions sz000001 --date 20260811 --count 50  # 历史逐笔
tdx minute sz000001                                # 今日分时
tdx minute sz000001 --date 20260811                 # 历史分时
```

## 板块

```bash
tdx board list --type 1                # 板块列表（0=全部 1=行业 2=概念）
tdx board members 881001 --count 30    # 板块成分股报价
tdx board ranking --type 1 --top 10    # 板块涨跌幅排行
tdx board change --type 1 --days 5 --top 10   # 板块 N 日涨跌幅排行
```

## 证券列表

```bash
tdx list --market sz --count 100       # 市场证券列表（--market sz/sh/bj）
```

## 其他标准通道数据

```bash
tdx xdxr sz000001       # 除权除息历史
tdx finance sz000001    # 财务快照
tdx auction sz000001    # 集合竞价
tdx flow sz000001       # 资金流向
tdx unusual --market sh --count 30   # 市场异动
tdx server-info         # 服务器交易时段
```

## 服务器文件（板块 / 行业 / 财报）

这四个命令是 **下载 + 解析一步到位** 的，不需要自己拆二进制格式。

```bash
# 板块文件（block_zs.dat 行业 / block_gn.dat 概念 / block_fg.dat 风格）
tdx block block_gn.dat --limit 10          # 列出板块与成分股数量
tdx block block_gn.dat --limit 3 --codes   # 额外打印成分股代码

# 行业分类（tdxhy.cfg）
tdx industry 600000 000001 --search 银行   # 指定代码 / 关键词搜索
tdx industry --limit 30                    # 只看前 30 条

# 历史专业财报
tdx financial-list                         # 索引：文件名 / MD5 / 大小（gpcw.txt）
tdx financial gpcw20260331.zip --limit 5   # 下载 + 解压 + 解析
tdx financial gpcw20260331.zip --code 600000 --fields 8   # 只取指定股票、打印更多字段
```

## 组合用法

CLI 输出是纯文本，适合直接进管道：

```bash
# 把科创板全部股票代码喂给循环
tdx list --market sh --count 2000 | grep '^68' | head -20 | while read code; do
    tdx kline "sh${code}" --count 5
done

# 快速扫一遍概念板块涨幅榜
tdx board ranking --type 2 --top 30
```

!!! tip "什么时候该用 CLI，什么时候该写代码"
    - **临时看数 / 验证连通性 / shell 脚本** → CLI
    - **批量拉取、落库、回测** → 用 Python API，CLI 每次调用都要重建连接池，批量场景开销大

## 下一步

- [场景示例](cookbook.md) — Python API 的批量用法
- [接口总览](api/index.md) — CLI 背后的方法
