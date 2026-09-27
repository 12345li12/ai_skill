"""``[10] Agent Manager`` 的结构锁检测器（纯函数）。

用途与 `_scheduler_locks.py` 相同：① 对**真实**生产源码断言；② 接受**合成变异源码**字符串做
probe，证明每条锁**会因目标语义变异而红**（变异证据永久留在 CI，不改真实文件）。

⚠️ **不是通用 Python 静态分析器**：每条检测器只覆盖本项目真实写法与
`tests/behavior/README_step5b.md` §44.4 明确列出的绕法；证明边界写在各自 docstring 里。
可复用的部分**直接复用** `_context_locks`，避免出现第二套判定。

⚠️ **禁止子串扫描**：``BranchState.REJECTED`` 在 ``agent_manager.py`` 的 **docstring**
里被**故意提及**（说明"不写它"这条禁令），子串计数会因此误报 2 ⇒ L5 必须走 AST
（见 ``enum_member_references``）。
"""

from __future__ import annotations

import ast
from typing import Iterable

from _context_locks import (  # noqa: F401  (re-export 供测试统一引用)
    calls_to,
    class_method_node,
    exact_keys_comparisons,
    forbidden_import_hits,
    helper_call_count,
    keyword_only_defaults,
    mapping_get_calls,
    open_calls,
    resolved_symbols,
)
from _import_detectors import imported_module_tokens

# ⚠️ `dataclass_field_names` / `enum_member_names` 定义在 `_scheduler_locks.py`（`[09]` 轮引入）。
# 这里**直接复用**，不复制第二套判定；本文件不修改任何既有测试文件。
from _scheduler_locks import dataclass_field_names, enum_member_names  # noqa: F401

__all__ = [
    "calls_to",
    "class_method_node",
    "clock_call_hits",
    "dataclass_field_names",
    "enum_member_references",
    "exact_keys_comparisons",
    "forbidden_import_hits",
    "helper_call_count",
    "imported_module_tokens",
    "keyword_only_defaults",
    "manager_data_attribute_reads",
    "manager_self_attribute_reads",
    "mapping_get_calls",
    "module_constant_expression",
    "open_calls",
    "resolved_symbols",
]

#: L6：任何"读时钟"的调用属性名。``[10]`` 的时间戳必须由调用方显式传入。
CLOCK_ATTRIBUTES = ("now", "today", "utcnow", "time", "monotonic", "perf_counter")


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def _call_targets(tree: ast.Module) -> set[int]:
    return {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}


def manager_data_attribute_reads(source: str, *, enum_bases: Iterable[str]) -> set[str]:
    """``agent_manager.py`` 的**输入数据属性读取**名（L1）。

    三类**不算"读取输入字段"**，因此被排除：

    1. ``BranchState.<成员>`` / ``DispatchAction.<成员>`` —— **枚举常量引用**；
    2. 直接作为调用目标的属性（``self._records.append(...)``）—— **方法调用**；
    3. ``self.<名字>`` —— **本模块自己的私有状态**（由 ``manager_self_attribute_reads`` 单独锁）。

    ⚠️ 注意：``latest.state.value`` 里的 ``value``、``decision.reason`` 里的 ``reason``
    **仍会被抓到**（它们不是那次 Call 的 func），因此"借字段做判断"这条纪律没有被削弱 ——
    它们之所以在白名单里，是因为只出现在**错误消息**里（探针逐一证明）。
    """
    tree = _tree(source)
    call_targets = _call_targets(tree)
    bases = set(enum_bases)
    reads: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if id(node) in call_targets:
            continue
        if not isinstance(node.value, ast.Name):
            continue
        if node.value.id in bases or node.value.id == "self":
            continue
        reads.add(node.attr)
    return reads


def manager_self_attribute_reads(source: str) -> set[str]:
    """``self.<名字>`` 形式读取的名字（L2b：私有状态必须只有四个已知字段）。"""
    tree = _tree(source)
    call_targets = _call_targets(tree)
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and id(node) not in call_targets
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    }


def enum_member_references(source: str, enum_name: str) -> set[str]:
    """源码里**引用到的** ``enum_name.<成员>`` 成员名（L5）。

    两条路径都覆盖，因为它们都能把禁用成员带进运行代码：

    - ``BranchState.REJECTED``（属性访问）；
    - ``getattr(BranchState, "REJECTED")``（常量字符串取成员）。

    ⚠️ **证明边界**：不跟随变量间接（``name = "REJECTED"; getattr(BranchState, name)``）、
    不跟随 ``importlib`` / 跨模块 re-export；这不是通用静态分析器。
    """
    refs: set[str] = set()
    for node in ast.walk(_tree(source)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == enum_name
        ):
            refs.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == enum_name
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            refs.add(node.args[1].value)
    return refs


def clock_call_hits(source: str) -> list[tuple[str, int]]:
    """疑似"读时钟"的调用站点 ``[(属性名, 行号)…]``（L6）。

    覆盖 ``time.time()`` / ``datetime.now()`` / ``clock.monotonic()`` 这类**属性调用**。
    ``[10]`` 的所有时间戳必须由调用方显式传入（A13.2）。
    """
    hits: list[tuple[str, int]] = []
    for node in ast.walk(_tree(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CLOCK_ATTRIBUTES
        ):
            hits.append((node.func.attr, node.lineno))
    return hits


def module_constant_expression(source: str, name: str) -> str:
    """模块级常量 ``name`` 的**源码形态**（``ast.unparse``，L9/L10/L12 的枚举成员锁）。

    例如 ``_LIVE_STATES`` ⇒ ``"(BranchState.RUNNING, BranchState.WAITING_INPUT)"``。
    这样 A13.4 的"``WAITING_INPUT`` 必须计入"就变成**可断言的字面事实**，
    而不是只写在 docstring 里的注释。找不到则抛 ``AssertionError``。
    """
    for node in _tree(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return ast.unparse(node.value)
    raise AssertionError(f"未找到模块级常量 {name}")


def imported_module_paths(source: str) -> set[str]:
    """⚠️ **已删除**：本函数曾自行读取 ``ImportFrom.module``，与
    `_import_detectors.imported_module_tokens` 构成**第二套朴素判定**
    （``from . import X`` 这种 ``module is None`` 的形式会被绕过），
    因此被 `test_import_detectors.TestNoReDivergence` 判红。

    正确做法是**只用共用检测器**：见 ``test_agent_manager.py`` 的
    ``FROZEN_IMPORT_TOKENS``（对 ``imported_module_tokens`` 的**精确集合**断言）。
    """
    raise AssertionError("不要重新实现 import 检测器；请使用 imported_module_tokens")
