# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.6.0] - 2026-09-16

### 新增

- **统一对外接口**：一个数据类型 = 一个方法，跨市场自动选路。
  标的统用字符串写法（`sz000001` / `hk00700` / `usAAPL` / `cffex:IFL0`），
  A股/港股/美股/期货/期权混合查询一次返回。旧的 51 个按通道/市场分裂的接口中，
  33 个保留为 deprecated 别名（运行时发出 `DeprecationWarning`，**1.0 移除**），
  另 18 个（`get_quotes` / `get_xdxr` / `get_finance` / `get_auction` / `get_capital_flow` /
  `get_market_stat` / `get_unusual` / `get_server_info` / `get_kline_offset` /
  `get_board_members` / `get_board_summary` / `get_board_ranking` / `get_board_change_ranking` /
  `get_block_parsed` / `get_industry_map` / `get_financial_file_infos` /
  `get_financial_records_parsed` / `get_price_limits`）同名升级为统一接口。

  | 数据类型 | 新接口 | 合并掉的旧接口 |
  |----------|--------|----------------|
  | 标的清单 | `get_universe(market)` | get_security_count + get_security_list + get_security_list_all + get_goods_count + get_goods_list |
  | 报价 | `get_quotes(symbols, *, fields)` | get_quotes + get_stock_quotes + get_goods_quotes + get_goods_quotes_list |
  | K线 | `get_bars(symbols, period, ...)` | get_kline + get_index_kline + get_kline_batch + get_kline_since |
  | 指标 | `get_indicators(symbols, indicators, ...)` | get_stock_kline_with_indicators |
  | 逐笔 | `get_ticks(symbols, *, date)` | get_transactions + get_goods_transaction + get_goods_transaction_all |
  | 分时 | `get_minutes(symbols, *, date, sampling)` | get_minute + get_minute_batch + get_chart_sampling + get_goods_chart_sampling + get_goods_tick_chart |
  | F10 | `get_f10(symbol, section)` | get_company_info_category + get_company_info_content |
  | 文件 | `get_file(name)` | get_block_info + get_report_file + get_file_meta + download_file |
  | 财务文件 | `get_financial_file_infos()` / `get_financial_records_parsed(fn)` | get_financial_file_list + get_financial_records |
  | 板块列表 | `get_boards(kind)` | get_board_list |
  | 板块成员 | `get_board_members(board)` → 归一 `SecurityQuote` | get_board_members + get_stock_quotes_list（分类报价） |
  | 个股板块 | `get_board_of(symbols)` | get_belong_board |
  | 快照 | `get_snapshot(symbols)` | get_symbol_info |

  > `TdxData` 公开方法 63 个 = **28 个统一接口** + 33 个 deprecated 别名 + `start`/`close`。
  > 统一接口一律只接受字符串标的；同名旧方法不再兼容 `(market, code)` 元组写法。

- **库层标的解析** `pytdxdata.symbols`（原仅 CLI 内有）：`parse_symbol` / `format_symbol` /
  `parse_symbols` / `market_targets` / `is_cn_index`；裸 6 位数字视为沪市指数。
- **归一数据模型**：`SecurityBar` / `SecurityQuote` / `TransactionRecord` / `MinuteBar` /
  `SecurityInfo` 等均新增 `symbol`（规范化标的串）/ `name` / `fields`（原始字段位兜底），
  老构造写法不受影响（新字段有默认值）。
- `IndicatorSet` dataclass（`get_indicators` 返回单元：`bars + indicators` 组合）。
- `CacheKey.quote_key(..., tag=)` 支持按取数口径（标准五档 / MAC 字段位）隔离缓存。

### 修复

- **分页器连接泄漏**：`Paginator._worker` 换连接后旧连接不释放、池子被耗干
  （`_Slot` 包装 + 归还时按槽释放）。
- **日期区间 K线返回 0 条**：标准通道 `start` 偏移方向是递减（0=最新），旧的二分
  假设递增。改为运行时探测方向再二分（`_offset_descending` / `_bisect_offset`）。
- **池子全冷却死等**：所有服务器进冷却后 `acquire` 排队 30s 超时。
  补 `ignore_cooldown=True` 兜底，避免无人持有连接时永久等待。
- `HealthEngine.COOLDOWN_SECONDS` 保持 120s（旧的 FAIL_STREAK_COOLDOWN=1 意味着
  任意一次通信失败即冷却，实测下正常服务器不应被冷却，保留该行为）。

### 变更

- **`docs/api/index.md` 接口总览**重新组织：统一接口（推荐）优先于旧接口列表。
- `scripts/check_docs.py`：`docs/api/index.md` 从 `MUST_LIST_ALL` 移到
  `MUST_NOT_BE_STALE`（只列推荐的统一接口，旧方法不强制出现在总览页）。
- CLI `tdx quotes` 改为统一 `td.get_quotes(symbols)`（跨市场混合报价）。
- CLI `tdx kline` 改为统一 `td.get_bars(sym, period)`。
- 测试 127 个用例全绿（新增 codec mock `get_file` 适配 + check_docs 新方法列表 +
  `tests/test_all_interfaces.py` 全接口回归：mock 连接逐个调用全部 63 个公开方法，
  并以「覆盖哨兵」断言无方法遗漏）。

## [0.5.0] - 2026-09-15

### 新增

- **文档站**（MkDocs + Material + `mkdocstrings`），每次 push 自动构建并部署到
  <https://openbot-coder.github.io/pytdxdata/>
- **[`SKILL.md`](https://github.com/openbot-coder/pytdxdata/blob/main/SKILL.md)** — 面向 AI 编程助手的技能文件：三条必守约定、枚举取值表、
  「想做什么 → 用哪个方法」决策表、9 条真实陷阱
- [`llms.txt`](https://openbot-coder.github.io/pytdxdata/llms.txt) — 面向 LLM 的结构化摘要，随站点发布到根路径
- `CHANGELOG.md` 独立成文件（此前内嵌在 README 中）
- `scripts/check_docs.py` + CI 步骤 — **文档漂移检查**：代码有而文档漏写、或文档引用了已删除的方法，都会 fail
- `tests/test_check_docs.py`（10 项）— 漂移检查器自身的自测

### 变更

- **README 瘦身为「入口层」**：361 → 129 行。只保留定位 / 安装 / 30 秒上手 / 特性 / 文档导航 / 免责声明；
  CLI 手册、API 表、通道路由、架构树、扩展市场、easy-tdx 对比、Changelog 全部移交文档站与 `CHANGELOG.md`
- API 文档改由 `mkdocstrings` **从 docstring 实时生成**，不再手抄（根治漂移）
- 漂移检查扩展为两档：`MUST_LIST_ALL`（须列全所有公开方法）+ `MUST_NOT_BE_STALE`（只需无过时引用），
  并把 `SKILL.md` / `README.md` / `llms.txt` / 教程类页面一并纳入
- 包元数据 `Documentation` 由仓库首页改指文档站
- 测试用例 **99 → 109**

### 文档

- 新增文档站 19 页：快速开始 / 场景示例 / CLI / FAQ / 贡献指南 / 更新日志 /
  API 参考（总览·TdxData·枚举·模型·指标·解析器）/ 深入原理（架构·协议·三池路由·缓存）
- 明确口径：`TdxData` 公开方法共 **53 个**（= 51 个数据方法 + `start` / `close`），
  此前各处笼统写的「51 个公开方法」不严，已统一更正

### 修复

- 修正**中文标题锚点被默认 slugify 剥光**导致跨页深链接全部失效的问题
  （改用 `pymdownx.slugs.slugify`，并给被引用的标题加显式 ASCII id；`--strict` 不会报这个错，只能靠锚点检查兜住）
- README 中标注的测试数量（85）与 pytest 实际收集数不符，已更正为 109 个用例

## [0.4.0] - 2026-09-15

### 新增

- **服务器文件「下载 + 解析」能力**（对齐并超越 easy-tdx 的文件解析）：
    - `get_block_parsed(filename)` — 板块 `block_zs/gn/fg.dat` → `list[TdxBlock]`（板块名 / 类别 / 成分股代码）
    - `get_industry_map()` — `tdxhy.cfg` → `dict[str, IndustryInfo]`（通达信行业 + 申万行业）
    - `get_financial_file_infos()` — `tdxfin/gpcw.txt` → `list[FinancialFileInfo]`（文件名 / MD5 / 大小）
    - `get_financial_records_parsed(filename)` — `gpcw*.zip` 解压 + 解析 → `list[FinancialRecord]`（每股原始字段）
- 解析器模块 `codec/block.py`、`codec/industry.py`、`codec/financial.py`（纯标准库，脱网可单测）
- 数据模型 `TdxBlock` / `IndustryInfo` / `FinancialFileInfo` / `FinancialRecord`
- CLI 新增 4 个命令：`tdx block` / `tdx industry` / `tdx financial-list` / `tdx financial`
- 测试 `tests/test_cli_files.py`（11 项 CLI 离线测试，FakeTD 桩）

### 修复

- 修复财报索引市场字段解析（`<6s1c1L` → `<6sBL`）。此前恒判深市，现市场字段 **0=深 / 1=沪** 正确

### 变更

- 公开异步方法 **47 → 51**，新增接口全部**向后兼容**（原 `bytes` 方法保留）
- README 与 `docs/API_REFERENCE.md` 同步更新

## [0.3.2] - 2026-08-17

### 新增

- `get_kline_batch(stocks, period, ...)` — 批量 K 线，多股票自动并发，连接池分配不同服务器
- `get_minute_batch(stocks, ...)` — 批量分时，多股票自动并发

## [0.3.1] - 2026-08-17

### 变更

- `get_kline(MIN_1)` 切换为 **MAC 通道**（`0x122E`），解决标准通道 1 分钟 K 线深度不足的问题
- `router.py` 中 `KlinePeriod.MIN_1` 强制路由到 MAC 通道
- 标准通道其他周期（`MIN_5/15/30/60/DAY/WEEK/MONTH`）不受影响

### 修复

- **修复 MAC K 线并发分页的数据空洞**：并发分页器把不同页分配到不同服务器，
    而各服务器数据深度不一致时 `stop_when_short` 会导致**非确定性数据丢失**。
    改为**单连接串行分页**（`_fetch_mac_kline_serial`，`start` 动态偏移，与 easy-tdx 一致）

## [0.3.0] - 2026-08-17

### 变更

- `get_minute` 切换为 **MAC 通道**（`0x122D` `MacTickChartCmd`），历史分时稳定返回完整 240 条（09:30~14:59）
- 移除不再使用的 `GetHistoryMinuteTimeDataCmd`

### 新增

- `protocol/commands/mac_tick.py` — MAC 单日分时图命令

## [0.2.2] 及之前

- 初始版本：动态连接池 + 双层 TTL 缓存 + 3 连接并发分页 + 双通道（标准 / MAC）+ 扩展市场（港股 / 美股 / 期货 / 期权）

[Unreleased]: https://github.com/openbot-coder/pytdxdata/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/openbot-coder/pytdxdata/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/openbot-coder/pytdxdata/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/openbot-coder/pytdxdata/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/openbot-coder/pytdxdata/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/openbot-coder/pytdxdata/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/openbot-coder/pytdxdata/compare/v0.2.2...v0.3.0
