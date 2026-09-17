# 参与贡献

欢迎 Issue / PR。这个项目对「数据正确性」的要求高于「功能数量」——一个算错的字段比没有这个字段糟糕得多。

## 开发环境

```bash
git clone git@github.com:openbot-coder/pytdxdata.git
cd pytdxdata
uv sync                 # 建虚拟环境 + 安装 CLI + 装测试依赖
```

要求 Python **≥ 3.11**。项目用 `uv` 管理依赖和构建，构建后端是 `uv_build`。

## 跑测试

```bash
.venv/Scripts/python.exe -m pytest tests/ -q      # Windows
.venv/bin/python -m pytest tests/ -q              # Linux / macOS
```

!!! danger "不要用 `uv run pytest`（Windows）"
    Windows 上会报 `uv trampoline failed to canonicalize script path`。直接调 venv 里的解释器。

!!! warning "Windows 上异步测试会卡死"
    根因是 `asyncio` 建事件循环时用到的回环 `socketpair` 被本地代理 / 安全软件拦截。
    详见 [常见问题 → 开发与测试](faq.md)。

    因此**CI 在 Linux 上跑首轮异步验证**；Windows 侧只能做同步驱动验证。

现有测试全部是 **mock 驱动、不连网络** 的离线测试（`tests/test_api.py` 里的
`FakeApiConn` / `FakePool` 就是为这个准备的）。新增功能请沿用这个模式，
不要把真实网络请求写进单测。

## 代码约定

| 约定 | 说明 |
|---|---|
| 数据模型 | 全部 `@dataclass(slots=True)`，集中在 `models/__init__.py`（无子模块） |
| 解析器 | 统一放 `codec/`，**纯标准库**（`struct` / `zipfile`），做到脱网可单测 |
| API 兼容性 | **保持向后兼容**。新增能力用新方法名（如 `get_block_parsed`），不改旧方法签名或返回值 |
| 类型注解 | 全部公开接口必须有完整注解（`Typing :: Typed`） |
| 依赖 | 运行时不加新依赖是本项目的核心卖点，PR 里请勿引入 |

### 新增一个行情接口的完整步骤

1. `protocol/commands/<域>.py` — 实现命令类（帧组装 + 响应解析）
2. `router.py` — 若需要按命令选通道，在这里登记
3. `models/__init__.py` — 加返回的 dataclass / 枚举
4. `api.py` — 加 `TdxData` 公开方法
5. `cli.py` — 可选，加子命令
6. `tests/` — 离线测试（构造字节流 → 断言解析结果）
7. 文档 — 更新 `docs/api/index.md` 的方法表 + `docs/API_REFERENCE.md`

> 第 7 步别漏。历史上已经发生过「加了方法但文档没跟上」的漂移。

## 文档

文档站是 MkDocs + Material，配置在 `mkdocs.yml`，源文件在 `docs/`。

```bash
uv sync --group docs
uv run mkdocs serve          # http://127.0.0.1:8000 实时预览
uv run mkdocs build          # 产出到 site/
```

| 内容类型 | 写到哪 |
|---|---|
| 30 秒能看懂的东西、特性表、徽章 | `README.md`（**保持精简**） |
| 教程、场景示例、CLI 用法、原理 | `docs/` 对应页面 |
| 每个方法的参数 / 返回值 | `docs/api/`（**从 docstring 自动生成，不要手写**） |
| 版本变更 | `CHANGELOG.md`（唯一来源，文档站通过 snippet 引入） |

!!! tip "API 文档不要手抄"
    `docs/api/tdxdata.md` 这类页面用 `mkdocstrings` 直接从 `api.py` 的 docstring 渲染。
    你只要把 docstring 写好，文档就自动是对的。手抄签名一定会漂移。

## 提交与发布

分支模型：`develop` → PR → `main`。

```bash
git checkout -b feat/xxx
# ... 改代码 ...
.venv/Scripts/python.exe -m pytest tests/ -q
git commit -m "feat: xxx"
git push origin feat/xxx
```

### 发布流程（维护者）

1. 改 `pyproject.toml` + `src/pytdxdata/__init__.py` 的版本号
2. **`uv lock`** —— 同步 `uv.lock`，否则 CI 的 `uv sync --locked` 会失败
3. 提交并 push 到 `main`
4. 打标签并推送：
   ```bash
   git tag v0.6.0
   git push origin v0.6.0
   ```

推 tag 会自动触发 GitHub Actions：**test → build（含版本号一致性校验）→ OIDC 免密钥发布到 PyPI**。
不需要本地 token，也不需要 `twine`。

!!! warning "tag 已推送但发布失败时"
    同名 tag **不能直接重推**（push 无变化，workflow 不会触发）。先删再推：

    ```bash
    git push --delete origin v0.6.0
    git tag -d v0.6.0 && git tag v0.6.0
    git push origin v0.6.0
    ```

    或直接手动触发：`gh workflow run release.yml --ref v0.6.0`

文档站由 `.github/workflows/docs.yml` 在 push `main` 时自动部署到
<https://openbot-coder.github.io/pytdxdata/>。
