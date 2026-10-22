# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.6.7] - 2026-10-22

### 性能

- **全市场 A股报价 3.5s → 0.33s：单连接流水线 + 工作窃取**（5,227 只 / 66 片实测）。

  老实现 `get_quotes` 是**纯串行 for 循环**逐片发请求：协议硬上限 80 只/请求，
  全市场必须切 66 片，每片一次往返（≈60ms）→ 3.5s 全花在等网络上。

  关键发现：通达信响应是**自描述长度帧**（16B 帧头 + body）且服务器**按序应答**，
  因此同一 TCP 连接上可以连续下发多条请求、只付一次往返。实测单连接流水 66 个请求
  只要 526ms（串行 7,372ms），响应顺序与代码完全对得上（用昨收价交叉比对 30 只，零错配）。

  | 位置 | 变化 |
  |---|---|
  | `TdxConnection.execute_pipeline(cmds)`（新） | 一次 write 全部请求 → 按序读回全部响应。中途失败**强制断连**（响应流已错配，不可复用） |
  | `PooledConnection.execute_pipeline`（新） | 池化包装转发 |
  | `api._execute_std_pipeline`（新） | 借一条连接流水一整组分片；节点不支持流水线时自动降级为逐片 |
  | `get_quotes(..., pipeline=True, concurrency=None)` | 新参数；小批量（≤80 只）仍走原路径，零额外开销 |
  | `_quotes_std` | 改为**工作窃取**：分片放队列，连接做完一批回来再取，快的多干、慢的少干 |
  | `DynamicPool.warm(n)`（新） | **并发**预建连接。`acquire()` 建连在全局锁内一次一条，22 并发首次会串行握手 22 次 —— 全市场首拉因此多花 5~6s；预热后降到 0.78s |
  | `CacheManager.get_or_fetch_obj`（新） | 回源直接返回对象，省掉冷路径「pickle→立刻 unpickle」的一次白烧（5,227 条实测 ~40ms） |
  | `TdxData(pool_max=)` | 默认 12 → 24，让报价并发能到「每台 2 连接」（池仍是懒扩容，不影响其他接口） |

  调优实测（5,227 只 A股，11 台服务器，p50/max）：

  | 并发 | 每台连接 | 流水深度 | p50 | max |
  |---|---|---|---|---|
  | 11 | 1 | 6 | 455ms | 464ms |
  | 12 | 1~2 | 3 | 457ms | 523ms |
  | **22** | **2** | **3** | **325ms** | **334ms** |
  | 33 | 3 | 2 | 328ms | 345ms |

  ⚠️ 剩余下限由公网决定：本机 → 服务器 TCP 握手 RTT 59~80ms，66 片最多只能压到
  「1 次往返」；网络抖动时段实测会到 460~600ms。

  端到端实测（全默认参数 `TdxData()`，11 台服务器，5,227 只）：

  | 场景 | 耗时 |
  |---|---|
  | 首次调用（含并发建 22 条连接） | 783ms |
  | 稳态（第 2 次起） | **300~343ms** |
  | 命中 TTL 缓存 | 67ms |
  | 老版本对照（串行） | 5,996ms（首次）/ 3,460ms（稳态） |

### 修复

- **指数 / ETF / LOF 的 `trade_count` 不再伪装成成交笔数**。

  通达信对**指数**和**多数场内基金**不提供笔数，但逐笔报文里那 4 字节仍塞了值：
  指数是「当时均价 × 1000」，ETF/LOF 是「价格 × 1000」，货币 ETF（`sh511990`）恒为 `0`。
  旧版直接把它当笔数，于是 `tdx transactions sh510310` 会打印出「3994笔」这种假数据
  （实测 2026-10-22，价 3.989，原字段 3994）。

  | 位置 | 变化 |
  |---|---|
  | `TransactionRecord.trade_count_available`（新字段） | 该品种协议上是否带笔数；默认 `True` |
  | `symbols.has_order_count(market, code)`（新函数） | 按品种判定；**沪市 501x/502x/506x 实测有真实笔数**，不能按「5 开头=基金」一刀切 |
  | `transaction.py` 三个解析器 | 不可用时 `trade_count=0` + `trade_count_available=False`；历史通道（0x013001，无笔数字段）同样标记 |
  | `tdx transactions` CLI | 不可用时打印 `(-)`，不再打印假笔数 |

  实测样本（2026-10-22，按前缀各取样）：沪股票/科创/深股票/深可转债/沪逆回购 → 真实笔数；
  沪 `51/52/56/58`、深 `159/15x/16x/17x/18x`、所有指数 → 不可用。

### 文档

- **可转债的量纲口径补全**（数据本身无缺陷，但对外文档一片空白，很容易被误判成 bug）。

  起因：核查可转债的分时（`0x122D`）与逐笔（`0x122F`）是否有类似 ETF/LOF 的笔数问题。
  结论是**价格 / 成交量 / 笔数三样全部正确**，顺带确认了 K 线标度也对；唯一的问题是
  `vol` 的单位没人写清楚 —— 直接拿 `amount / vol` 算均价会**错 10 倍**。

  实测（2026-10-22，16 只 A股可转债当日 + 8 只历史日 `2026-10-21`）：

  | 校验项 | 结果 |
  |---|---|
  | 分时末价 / 快照现价 | **1.000000** |
  | Σ分时 vol / 日线 vol | **1.000000** |
  | Σ逐笔 vol / 日线 vol | **1.000000** |
  | 逐笔 VWAP vs `amount/(vol×10)` | 吻合（差 ≤0.002，是 amount 精度） |
  | 快照 `pre_close` vs 上一交易日日线 `close` | **16/16 精确相等** |
  | `trade_count` | 真实笔数（`sh111000` 3631 条切片 / 20443 笔） |

  关键口径对照（这是文档缺失的部分）：

  | 品种 | 分时 / 逐笔 `vol` | 日线 `vol` | `Σ分时vol / 日线vol` | `amount / vol` |
  |---|---|---|---|---|
  | 股票 / ETF / LOF | 手（= 100 股） | 股 | **0.01** | 均价 ✓ |
  | 可转债 | 手（= 10 张） | **手** | **1.0** | 均价 **×10** ⚠️ |

  | 位置 | 变化 |
  |---|---|
  | `SecurityBar` / `TransactionRecord` / `MinuteBar` | 量纲注释补可转债（`vol`=手；日线 `amount/vol` 需再 `/10`） |
  | `symbols.has_order_count` | 实测表补沪可转债 `110/111/113/118`、沪 EB `132`、深可转债 `123/127/128`，并说明它们走「排除法」兜底分支 |
  | `docs/API_REFERENCE.md` | 新增**跨品种量纲对照表** |
  | `docs/llms.txt` / `SKILL.md` | 同步同一口径 |

  **代码逻辑未改**：可转债在 `has_order_count` 里本来就判为「有笔数」（不在基金/指数的
  排除前缀里），本次只是把实测依据补进注释与文档。

## [0.6.6] - 2026-10-22

### 改进

- **价格小数位改用协议自带的权威值，前缀规则降为兜底**。

  0.6.5 修好债券/回购标度时，采用的是「按代码前缀推断」+「用全量清单做离线校验」。
  但同一份证券清单接口（`get_universe`，底层 `GetSecurityListCmd`）本来就**返回**
  `SecurityInfo.decimal_point`（外加自描述浮点的 `pre_close`）——权威值一直就在手上，
  只是没接进取数路径。本次把它接进来：

  | 位置 | 说明 |
  |---|---|
  | `price_decimal_digits` | 查找顺序改为 **权威表 → 静态前缀规则**，未同步时行为与 0.6.5 完全一致 |
  | `decimal_sync`（新模块） | 每日后台同步一次 + 磁盘缓存（`%LOCALAPPDATA%\pytdxdata\decimal-points.json`） |
  | `TdxData.sync_decimal_points()` | 手动强制刷新（排障 / 立刻要权威表） |
  | `TdxData.decimal_point_status()` | 状态：`source` / `stock` / `mixed_prefixes` / `syncing` |

  **对用户仍无感**：`start()` 命中当日缓存就直接装表（实测 60 ms、零网络），
  未命中则挂后台 task（实测首次 50 s，含沪 27,520 + 深 24,462 条清单），
  不阻塞 `start()`（`start()` 仍 1.5 s）；`close()` 会取消未完成的同步。

  **表结构刻意做小**：全量 51,982 只标的只产生 **179 个**「三位前缀 → 小数位」映射
  （沪 96 + 深 83），内存里就是 179 条。若将来某段代码混用两种小数位，该前缀自动
  退化为**逐代码表**，只存例外代码（`mixed_prefixes` 会 > 0，可据此发现），
  不会静默算错。清单拉取不完整时表会偏小 —— 缺的部分退回静态规则，不影响正确性。

  为什么保留前缀规则：`get_quotes` 解析是纯同步、无网络的路径，解析那一刻可能还没
  同步过清单；离线 / 首次安装也必须能出价格。实测两者在全量清单上**零分歧**
  （51,982 条逐条比对，不一致 0 条），并存不会造成口径冲突。

  开关：`PYPYTDX_DATA_DISABLE_DECIMAL_SYNC=1` 关闭后台同步（CI / 测试用）。

### 说明

- 新增回归测试 16 项（权威表优先级 / 混合前缀退化 / 缓存跨日失效 / 损坏容错 /
  后台任务取消），测试总数 **289 → 306**。

## [0.6.5] - 2026-10-22

### 新增

- **指数成分成交汇总：一条接口拿到「某指数下全部股票的成交量与成交笔数」**
  （`get_index_members` / `get_index_trade_stats` / `get_index_trade_stats_multi`）。

  起因是一个绕不开的协议事实：**指数本身没有成交笔数**。指数逐笔（`sh000001`）的
  `trade_count` 字段实际是「当时均价 × 1000」（2026-10-21 已验证），`get_minutes` /
  `get_market_stat` / `SecurityBar` / `get_quotes` 的 160 个字段位里也都**没有**笔数
  （扫描 0x122C 全字段位确认，`0x8`/`0x9` 是外盘/内盘手数）。所以「指数笔数」唯一可行
  的口径是**把成分股的逐笔笔数累加**。

  ```python
  async with TdxData() as td:
      st = await td.get_index_trade_stats("000300")     # 沪深300，约 10 s
      for hhmm, cum in st.cum_trade_count():            # 截止该分钟的累计笔数
          print(hhmm, cum)
      st.minutes[0]            # MinuteTradeStat(time='09:25', volume=…, trade_count=…)
      st.total_volume          # 手
      st.total_trade_count     # 笔
  ```

  | 方法 | 作用 |
  |---|---|
  | `get_index_members(index, *, fields=None, count=100000)` | 指数成分股（含实时报价）。裸 6 位按**指数语义**解析：`000001`=上证指数，不是平安银行 |
  | `get_index_trade_stats(index, date=None, *, concurrency=24, batch_size=50, members=None, skip_errors=True, on_progress=None)` | 成分逐笔 → 每分钟成交量/笔数；`date` 支持历史 |
  | `get_index_trade_stats_multi(index, dates, *, ...)` | 多日批量（成分只请求一次），用于「近 N 日同时点均值」基准 |

  **口径校验（2026-10-22 实测）**：``total_amount`` 是**成分股**合计。对「成分 = 指数
  覆盖面」的指数，它与指数快照 ``amount`` 完全相等 —— 说明成分清单与量能口径一致。

  | 指数 | 成分 | 成分合计 | 指数快照 | 比值 | 耗时 |
  |---|---|---|---|---|---|
  | `sh000016` 上证50 | 50 | 661.38 亿 | 661.34 亿 | 1.0001 | 5.8 s |
  | `sh000300` 沪深300 | 300 | 2373.98 亿 | 2373.98 亿 | 1.0000 | 9.7 s |
  | `sh000001` 上证指数 | 2230 | 3907.00 亿 | 4111.56 亿 | 0.9503 | 35.6 s |

  历史日期同样成立（2026-10-21 收盘数据）：`sh000016` 1046.29 / 1045.59 亿（1.0007）、
  `sh000300` 3881.65 / 3879.30 亿（1.0006）。

  单只股票级别的口径更硬：`Σ(逐笔价 × 手 × 100)` 与日线 `amount` 比值
  **1.0000~1.0004**（`sh600000` / `sz000001` / `sh601398` / `bj920002` / `bj920015`
  全部吻合），`Σvol` 与快照 `vol` 比值 1.0000。

  CLI 同步新增：`tdx index-stats 000300 --top 10`。

  **三个必须知道的限制**：

  1. **深市 `399xxx` 的指数成交额是「全板块」口径**，不是样本股 —— 实测
     `sz399001` 快照 5081.40 亿 = 深市 2908 只股票合计 5081.01 亿，`sz399006`
     2568.60 亿 = 创业板全部股票合计。所以拿它跟 `total_amount` 比会得到 0.43 这种
     无意义比值，不是数据错。**`get_index_trade_stats` 返回的始终是成分口径。**
  2. **北交所（`899xxx`）没有笔数字段** —— 逐笔 `trade_count` 恒为 0，此时
     `trade_count_available` 为 `False`，只有成交量可用。
  3. **成分表比全市场滞后** —— 上证指数返回 2230 只，而 `get_universe("sh")` 里
     股票段共 2360 只，差 130 只（新上市尚未纳入服务端板块），这正是上表
     0.9503 的来源；`sh000016`/`sh000300` 这类固定样本指数不受影响。
     需要精确覆盖时可自行传 `members=[...]`。

### 修复

- **债券与回购价格全线偏差：沪市少 100 倍、深市少 10 倍**（`get_quotes`）。

  `price_decimal_digits` 只覆盖了「股票 2 位 / 基金 3 位」两档，债券与回购没有对应
  分支，落到默认档位。它们实际是 **4 位小数**，于是被读大了两个数量级：

  | 标的 | 真实价 | 修复前 | 偏差 |
  |---|---|---|---|
  | `sh019742` 国债 24特国01 | 110.2280 | 11022.8000 | ×100 |
  | `sh110077` 可转债 洪城转债 | 199.2790 | 19927.9000 | ×100 |
  | `sh204001` 回购 GC001 | 1.3850 | 138.5000 | ×100 |
  | `sh900901` B股 云赛Ｂ股 | 0.5670 | 5.6700 | ×10 |
  | `sz123107` 可转债 温氏转债 | 112.1890 | 1121.8900 | ×10 |
  | `sz131810` 回购 R-001 | 1.3800 | 13.8000 | ×10 |

  沪市 `010/018/019/020/1xx/2xx` 与深市 `10x-14x/19x/50x/52x/56x` 全部改为 4 位；
  沪市 B 股 `900xxx` 另改为 3 位（与深市 `200xxx` 的 2 位不同，两市不一致）。
  股票 / ETF / 指数 / 板块的行为**不变**。

  **验证方式（可作为该函数的验收标准）**：拿 `pre_close`（五档快照口径，走
  `price_decimal_digits`）除以上一交易日**日线收盘**（K线口径，走独立的 `/1000`
  路径），比值 1.0 即两口径一致。修复后 15 类标的全数为 1.0000，零偏差。

  **权威来源已确认**：证券列表里协议**自带** `SecurityInfo.decimal_point` 字段。
  2026-10-22 拉取全量 **51,982 只**（沪 27,520 + 深 24,462）逐条比对，本函数与
  协议字段**完全一致、零例外**——因为全量清单里没有任何一个「三位前缀」出现混合
  小数位。`get_quotes` 解析时拿不到证券清单，故仍用前缀推断；需要权威值时读
  `SecurityInfo.decimal_point`。

- **美股（EX 通道）分时的 `hour` 是非法时钟值**（`get_minutes("usAAPL")`）。

  EX 分时的 `minutes` 字段是**自开盘起的连续分钟数**，不是「当天的分钟数」。
  旧代码直接 `minutes // 60`，于是 390 条里 **240 条** 的 `hour` ≥ 24：

  ```
  修复前: 21:30, 21:31, ..., 23:59, 24:00, 24:01, ..., 27:59   ← hour=27 不存在
  修复后: 21:30, 21:31, ..., 23:59, 00:00, 00:01, ..., 03:59   ← 真实北京时间
  ```

  任何 `datetime(...)` 拼接都会因 `hour=27` 抛 `ValueError`，按小时聚合也会把
  凌晨段错误地归到同一个自然日。

  **修法**：`MinuteBar.__post_init__` 把越界的 `hour` 拆成
  「`hour %= 24` + `day_offset += hour // 24`」，并新增两个只读属性：

  | 属性 | 用途 |
  |---|---|
  | `MinuteBar.datetime` | 该分时点的时刻（北京时间，已含跨日偏移） |
  | `MinuteBar.trading_date` | 归属的交易日 `YYYY-MM-DD`。美股一个交易日横跨两个北京自然日（21:30~次日 04:00），**按交易日聚合必须用它**，与 `SecurityBar.trading_date` 口径一致 |

  A股（合成时间 09:30~15:00）/ 港股（09:30~16:00）/ 期货的 `hour` 本就合法
  → **零行为变化**，`day_offset` 恒为 0。

  实测（2026-10-22）：`usAAPL` / `usTSLA` / `usNVDA` 各 390 条，修复后
  `hour` 全在 0~23、时间轴严格单调、`trading_date` 收敛为单一交易日；
  `hk00700` 90 条无任何变化。

### 说明

- 新增回归测试：债券/回购/B股小数位 ×29、美股分时跨日 ×6、指数成分成交汇总 ×34、
  全接口 ×1，测试总数 **217 → 289**。

## [0.6.4] - 2026-10-22

### 修复

- **`get_ticks()` 返回列表不是时间序，每 `page_size` 处时间回跳一次**。

  `0x122F`（A股逐笔）与 EX 逐笔通道的偏移量 `start` 都是**从最新一笔往回数**的：
  实测 `sh600000`，`start=0` 返回 `14:07:48..15:20:16`（当天最新 1000 条），
  `start=4000` 返回 `09:15:03..09:41:18`（当天最早）。而 `paginator` 按 `start`
  升序拼接，于是拼出"新的块在前、旧的块在后"的倒序结构：

  | | 修复前 | 修复后 |
  |---|---|---|
  | `sh600000` 时间轴 | 4 处逆序跳跃（index 999 / 1999 / 2999） | 严格升序 |
  | `hk00700` 时间轴 | 21 处逆序跳跃（每 1800 条一次） | 严格升序 |

  修复分两层：① `PageSpec.newest_first=True` 让 `paginator` 反页序拼装（页内本身升序，
  只翻页序），这条能力保留给将来需要按"最新优先"锚点分页的命令；② A股逐笔最终改为
  **单连接串行游标**（见下一条），从根上避开多主机锚点错位。

  **`start` 参数语义同时明确**：它是"从最新往回数的偏移"，不是"从最早往后数的偏移"，
  `docstring` 已补充。注意这修正的是**顺序**，数据本身无缺口、无重复（4304 条唯一时间戳）。

- **`TransactionRecord.trade_count` 在集合竞价段被读成 42 亿量级脏值**。

  该字段在协议里是**有符号** `int32`，但解析格式串写的是无符号 `I`。集合竞价段
  （`bs_flag=8`）真实值为负数，按 uint32 解出 `4294966422`：

  ```
  原始 18 字节: 17820000 0ad71b41 d9000000 96fcffff 0800
  修复前: trade_count = 4294966422
  修复后: trade_count = -874          ← 0xfffffc96 的 int32 补码
  ```

  实测 `sh600000` 全天 4304 条中有 **38 条**命中（占 0.88%），全部落在 09:15~09:25
  集合竞价段；正常交易段（`bs_flag` 0/1/2/5）**无一条为负**。旧行为会让
  `max()` / `mean()` 等统计直接失真（曾算出 `mean=37,920,247`）。

  格式串由 `<IfIIH` 改为 `<IfIiH`。**注意负值语义尚未确证**（可能是撤单量/净委托），
  目前只是如实保留符号，未做业务解释。

- **`get_quotes()` 沪市指数价格整体少 10 倍**。

  `price_decimal_digits` 把沪市 `000`（如 000001/000016/000300/000688）与 `8`
  开头（如板块指数 880001/880305）一律判成 3 位小数（除 1000），但协议原始整数
  实际是 **2 位**（除 100）→ 上证指数 3713 被读成 371、上证50 2747 被读成 274。

  实证（2026-10-21）：沪市指数 6 只 + 板块 2 只全部 `分时中位价 / 快照价 = 10.0`，
  而沪市 ETF（510300/588000/511010/501018）、股票（60/601）、科创板（688）、
  深市指数/股票/创业板/ETF 全部 `= 1.0` 正确。修法是**把沪市 `000`/`8` 从 3 位改回 2 位**，
  其余前缀（`5` ETF/基金、深市 `1` 基金转债）保持 3 位不变。回归测试覆盖 21 组代码。

- **`get_ticks()` 把集合竞价**委托**（`bs_flag=8`）当成成交计入，导致 Σvol 虚高且跨主机不稳定**。

  `0x122F` 第 5 字段 `bs_flag=8` 的记录是 **09:15~09:25 / 14:57~15:00 竞价段的撮合委托
  快照**，`vol` 是**累计值**（可被同段后续记录覆盖），根本不是成交增量。旧代码把它一并
  返回，两个后果：

  1. **Σvol 比真值虚高**：`sh600000` 全天 Σvol 含 b8 时 = 668,693，快照 vol = 462,562，
     比值 1.4456；剔掉 b8 后 Σvol = 462,634，比值 **1.000156**（与快照精确一致）。
  2. **跨主机不稳定**：干净主机不收录 b8（0 条）、脏主机收录 135 条。并发分页时各页
     来自不同主机，同一 `start`（从最新往回数的相对锚点）在不同主机落到**不同的真实成交**
     上 → 页边界既**重叠**（sh600000 冗余 116 条）又**漏条**（13:03 附近丢 59 条），
     同一查询两次调用结果不同。

  修复：① 分页拼装后**统一剔除 `bs_flag=8`**；② A股逐笔改用**单连接串行游标翻页**
  （`_ticks_cn_serial`），彻底消除跨主机锚点错位。修复后单主机 1.0002、剔 b8 后跨主机
  公共时间窗**逐条一致**。

- **文档化指数逐笔字段的量纲差异**（非 bug，是数据源固有语义）。

  指数（如 `sh000016`）的 `vol` 单位是**百元**（成交额/100），`trade_count` 是**均价×1000**
  （不是笔数），与个股完全不同。实测 `快照amount / Σtick.vol = 100.0002`、`tc/1000` 与分时
  `avg_price` 差 < 1.1。已写进 `TransactionRecord` 类注释与 `get_ticks` docstring，
  避免用户把指数 Σvol 直接和成分股成交量比（会误得 ~26 倍）。

### 说明

- 新增回归测试：价格小数位 ×20（19 组参数化 + 1 组去 NUL）、`get_ticks` 剔 b8 /
  单连接串行 / 多页无重叠 ×3，测试总数 **194 → 217**。

## [0.6.3] - 2026-10-18

- fix(pool): ExPool acquire 背压等待（Condition 排队，默认 30s 超时）替代 3x0.05s 快速失败；超时异常带池状态
- feat(api): 新增 ex_pool_cap 参数（默认 2），单 EX 服务器场景不再静默退化为 1 并发（fixes #1）

## [0.6.2] - 2026-10-13

### 修复

- **美股分钟级 K 线的 `datetime` 全部错位**（`get_bars("usAAPL", KlinePeriod.MIN_1)`）。

  通达信 EX 通道返回的美股分钟线时间戳，把**两个时区拼进了同一个五元组**：日期分量是
  **美东交易日**（协议里的 `zipday`），时分分量却是**北京时间**（`tminutes`）。直拼出的
  `datetime` 会把同一交易日的**下午半场排到上午半场之前**——单日 390 根不仅首末颠倒，
  而且全部落在同一个北京自然日里。于是任何依赖 `datetime` 的行为都是错的：排序、
  `count` 切片（"取最新 N 根"）、日期区间二分与过滤、增量更新水位线。

  | 观测项 | 修复前 | 修复后 |
  |---|---|---|
  | 单日 390 根的日期分布 | **全部**落在 10-12（hour 0~23 混在一起） | 10-12 的 21:31~23:59（149 根）+ 10-13 的 00:00~04:00（241 根） |
  | 时间轴 | 上午半场被排到下午半场**之前** | 严格单调递增 |
  | `count=30` 是否取到真正最新的 30 根 | 否（拿到上一交易日的 23:30~23:59） | 是（当日收盘段 03:31~04:00） |
  | 每组首根 `open` / 末根 `close` 对日线 | 对不上 | **逐位相同**（10-07/08/09/12 四个交易日实测） |

  现在 `SecurityBar.datetime` **统一为北京时间**：美股凌晨段（北京 hour < 12）的日期
  自动 +1 天（含月末 / 年末进位）。A股 / 港股 / 期货原始即为北京时间，**零行为变化**。

  新增两个只读属性：

  | 属性 | 用途 |
  |---|---|
  | `SecurityBar.trading_date` | 该 bar 归属的交易日 `YYYY-MM-DD`。美股一个交易日横跨两个北京自然日，**按交易日聚合（分钟线合成日线、日内统计）必须用它，不能用 `datetime.date()`** |
  | `SecurityBar.is_us` | 是否美股（EX 通道 `US_STOCK`） |

  归一化在 `SecurityBar.__post_init__` 中实现，因此 **EX 的两条 K 线命令路径**
  （`_bars_ex` 的 0x23FF 与日期区间二分所依赖的 MAC 0x122E）自动共用同一份口径，
  不会出现"取数按一套时间、二分按另一套时间"的错位。

### 说明

- 分钟线缓存 TTL 为 **60 秒**，升级后旧缓存最多残留 1 分钟即自然失效，**无需手动清理**；
  日线（`period >= DAY`）不受本缺陷影响。
- 本版只修 K 线。**同日发现的同类问题未包含在内**：EX 分时（`get_minutes`）对美股返回的
  `MinuteBar.hour` 是**未取模的连续分钟数**（实测可达 27，即次日 03:xx），且 `MinuteBar`
  无日期分量可承载跨日信息——修法涉及"是否新增字段 / 日期如何归属"的取舍，留待下一版决策。
- 新增 9 项针对性回归测试（分界点、月末与年末进位、390 根整日单调性、MAC 路径同样归一化）。

## [0.6.1] - 2026-09-21

### 变更

- **重写「当日首次启动测速缓存」，改为对用户完全无感**。原实现在 v0.5.0 已随包发布，但存在
  三个会让它**静默失效**的缺陷，本次一并修正：

  | 缺陷 | 原实现 | 现在 |
  |---|---|---|
  | 候选集自指 | 连接池只把自己**已过滤后的成员**交给探针 → 只重测了 9 台，另外 20+ 台候选（含快节点）永远测不到 | 探针内部取**全部候选**（包内清单 ∪ 代码常量 ∪ env） |
  | 写回安装目录 | 结果写回 `src/pytdxdata/servers.json` → 容器 / 系统级安装只读、多用户互相覆盖 | 改写入**用户级缓存目录**，包内清单运行时**绝不被改写** |
  | 探测太弱 | 只做握手（`TdxConnection.test`），测不出「握手正常但 K 线空响应」的坏节点 | 发**真实业务命令**并要求返回非空（标准 / MAC / EX 各一） |
  | EX 池漏测 | EX 的 4 台从未被探测 | EX 一并纳入（7727 + 各自握手模式） |

- 测速结果按**实测延迟升序**被 `config.get_standard_hosts()` 等采用；当天已有缓存则**不再刷新**。
  后台执行，不阻塞 `start()`、不打印、失败静默降级。

- 本版基于 **0.6.0 的统一对外接口**构建（`get_bars` + 字符串标的）；同时把 0.6.0
  全接口回归测试里对旧探针入口（`pool.get_probe_cache`）的替身改为新入口（`pool.schedule_probe`），
  保证单测不触发真实网络测速。

### 修复

- **A股分时缩略采样 100% 失败**（`get_minutes(sampling=True)` / `get_chart_sampling()`）：
  旧实现走 `MacChartSamplingCmd`（**0x254D**），但该 opcode 属于 **EX 协议**，7709 标准 /
  MAC 主机对它一律不响应——实测 3 台主机各 10s 超时，叠加连接池 3 次重试约 24s 才抛错。
  现改为**由分时（0x122D）数据派生**价格点（分时本身就是 240 点，语义一致），
  实测 4 个 A股 场景均在 0.1s 内返回 240 点；EX 通道仍用 0x254D（84 点）。

- **历史专业财报整条链路不可用**（`get_financial_records_parsed()`）：
  `tdxfin/gpcw.txt` 索引里写的是裸 `gpcwYYYYMMDD.zip`，而服务器**实际只提供未压缩的**
  `tdxfin/gpcwYYYYMMDD.dat`。旧实现把索引名直接丢给 `zipfile` → 必然 `BadZipFile`；
  废弃别名 `get_financial_records()` 更危险，会**静默返回 0~1 字节空壳**而不报错。
  现按候选名（`tdxfin/*.zip` → `tdxfin/*.dat` → 原名）逐个尝试并校验数据形态，
  zip 与未压缩 dat 都支持。实测 `gpcw20260630.zip` → **5568 条**、
  `gpcw20260331.zip` → **5547 条**（样本 000001，584 个 float 字段），
  别名返回 **13,068,116 字节**真实数据；尚未生成的占位条目
  （如 `gpcw20260930.zip`，filesize=164）优雅返回空列表而非报错。

### 新增

- `codec.financial` 新增 `looks_like_gpcw_dat()` / `extract_gpcw_dat()`：
  财报字节的形态判定与「zip / 未压缩 dat」统一提取（纯标准库，可脱网单测）
- 环境变量 `PYPYTDX_DATA_CACHE_DIR`（覆盖测速缓存目录）、
  `PYPYTDX_DATA_DISABLE_PROBE`（关闭测速，CI / 测试用）
- `tests/test_probe_cache.py`（37 项）：当日缓存不刷新、跨日重测、
  原子写入且不留 tmp、**不改写包内 `servers.json`（逐字节校验）**、
  单飞并发、非法主机过滤、`DISABLE_PROBE`、池 `start()` 不阻塞 / `close()` 取消后台探针

### 文档

- [安装与配置](https://openbot-coder.github.io/pytdxdata/getting-started/#当日自动测速无感)：
  新增「当日自动测速（无感）」小节——缓存路径、优先级链、环境变量一览
- [通道与数据源](https://openbot-coder.github.io/pytdxdata/internals/routing/#daily-probe)：
  新增「运行时：当日测速缓存」小节，逐条说明必须踩对的实现要点与反例

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
