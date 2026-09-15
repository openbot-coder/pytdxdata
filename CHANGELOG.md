# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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

[Unreleased]: https://github.com/openbot-coder/pytdxdata/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/openbot-coder/pytdxdata/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/openbot-coder/pytdxdata/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/openbot-coder/pytdxdata/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/openbot-coder/pytdxdata/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/openbot-coder/pytdxdata/compare/v0.2.2...v0.3.0
