#!/usr/bin/env python3
"""文档漂移检查。

确保 `api.py` 里 `TdxData` 的公开方法与手写文档保持一致，双向检查：

1. **漏写**：代码里有、文档里没提到的方法
2. **过时**：文档里提到、代码里已不存在的方法

`docs/api/tdxdata.md` 由 mkdocstrings 自动生成，不在检查范围内（它不可能漂移）。
本脚本只管那些**手写**的接口清单，分两档：

- `MUST_LIST_ALL`：必须列全所有公开方法（漏写也报错）
- `MUST_NOT_BE_STALE`：入口 / 教程 / 精选清单，只要求「不提到已删除的方法」

用法::

    .venv/Scripts/python.exe scripts/check_docs.py    # Windows
    .venv/bin/python scripts/check_docs.py            # Linux / macOS

退出码 0 = 一致，1 = 存在漂移。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_FILE = ROOT / "src" / "pytdxdata" / "api.py"
PKG_DIR = ROOT / "src" / "pytdxdata"

# 必须**列全**所有公开方法的文档（漏写 = 漂移）
MUST_LIST_ALL = [
    ROOT / "docs" / "API_REFERENCE.md",
]

# 只需**不提到已删除的方法**的文档：
# 这些是入口 / 教程 / 精选清单 / 接口总览（只列推荐的统一接口，旧方法不强制列出），
# 但绝不能引用已不存在的方法
MUST_NOT_BE_STALE = [
    ROOT / "SKILL.md",
    ROOT / "README.md",
    ROOT / "docs" / "llms.txt",
    ROOT / "docs" / "index.md",
    ROOT / "docs" / "getting-started.md",
    ROOT / "docs" / "cookbook.md",
    ROOT / "docs" / "faq.md",
    ROOT / "docs" / "cli.md",
    ROOT / "docs" / "ai.md",
    ROOT / "docs" / "api" / "index.md",
    ROOT / "docs" / "refactor-audit.md",
]


def collect_api_methods(path: Path) -> tuple[set[str], set[str]]:
    """返回 (全部公开方法, 非 async 的公开方法)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    all_names: set[str] = set()
    sync_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TdxData":
            for item in node.body:
                if not isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if item.name.startswith("_"):
                    continue
                all_names.add(item.name)
                if not isinstance(item, ast.AsyncFunctionDef):
                    sync_names.add(item.name)
    return all_names, sync_names


def mentioned_in(text: str, name: str) -> bool:
    """文档中是否提到该方法（`get_kline` 不会误匹配 `get_kline_batch`）。"""
    return re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text) is not None


def collect_doc_references(text: str) -> set[str]:
    """抽取文档中以反引号标注的疑似方法名。

    同时覆盖两种写法：`` `get_kline` `` 与 `` `get_kline(market, code, ...)` ``。
    """
    return set(
        re.findall(r"`((?:get|download)_[a-z0-9_]+)\s*(?:\([^`()]*\))?`", text)
    )


def collect_package_symbols() -> set[str]:
    """收集包内所有模块级公开函数 / 类名，以及 ``__all__`` 导出的名字。

    文档里会合法地引用 `config.get_standard_hosts` 这类**模块级**函数，
    不把它们纳入白名单就会被误判成「引用了已删除的方法」。
    """
    names: set[str] = set()
    for path in PKG_DIR.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 语法错误交给 CI 的测试去抓
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                if not node.name.startswith("_"):
                    names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        try:
                            names.update(str(x) for x in ast.literal_eval(node.value))
                        except (ValueError, TypeError):
                            pass
    return names


def main() -> int:
    if not API_FILE.exists():
        print(f"[FAIL] 找不到 {API_FILE}")
        return 1

    methods, sync_methods = collect_api_methods(API_FILE)

    if "--list" in sys.argv:
        for name in sorted(methods):
            print(name)
        return 0

    print(f"api.py: TdxData 公开方法 {len(methods)} 个（含 start/close 生命周期）")

    # 「已删除」判定要排除包内其它公开符号（config 的模块级函数等），否则误报
    known = methods | collect_package_symbols()

    problems: list[str] = []

    if sync_methods:
        problems.append(
            "以下公开方法不是 async（本库约定全部公开方法为 async）: "
            + ", ".join(sorted(sync_methods))
        )

    for doc in MUST_LIST_ALL:
        if not doc.exists():
            problems.append(f"缺少文档文件: {doc.relative_to(ROOT)}")
            continue

        text = doc.read_text(encoding="utf-8")
        rel = doc.relative_to(ROOT).as_posix()

        missing = sorted(n for n in methods if not mentioned_in(text, n))
        if missing:
            problems.append(
                f"{rel} 漏写 {len(missing)} 个方法: " + ", ".join(missing)
            )

        stale = sorted(n for n in collect_doc_references(text) if n not in known)
        if stale:
            problems.append(f"{rel} 提到已不存在的方法: " + ", ".join(stale))

        if not missing and not stale:
            print(f"  [OK] {rel}")

    for doc in MUST_NOT_BE_STALE:
        if not doc.exists():
            continue

        rel = doc.relative_to(ROOT).as_posix()
        text = doc.read_text(encoding="utf-8")
        stale = sorted(n for n in collect_doc_references(text) if n not in known)
        if stale:
            problems.append(f"{rel} 提到已不存在的方法: " + ", ".join(stale))
        else:
            print(f"  [OK] {rel}（无过时引用）")

    if problems:
        print("\n[FAIL] 文档漂移:")
        for p in problems:
            print("  - " + p)
        return 1

    print("\n[OK] 文档与代码一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
