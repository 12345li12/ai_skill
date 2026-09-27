"""``[09] Scheduler`` 的结构锁检测器（纯函数）。

用途与 `_context_locks.py` 相同：① 对**真实**生产源码断言；② 接受**合成变异源码**字符串做
probe，证明每条锁**会因目标语义变异而红**（变异证据永久留在 CI，不改真实文件）。

⚠️ **不是通用 Python 静态分析器**：每条检测器只覆盖本项目真实写法与 README §34–§37
明确列出的绕法；证明边界写在各自 docstring 里。可复用的部分**直接复用** `_context_locks`，
避免出现第二套判定。
"""

from __future__ import annotations

import ast

from _context_locks import (  # noqa: F401  (re-export 供测试统一引用)
    calls_to,
    exact_keys_comparisons,
    forbidden_import_hits,
    helper_call_count,
    keyword_only_defaults,
    mapping_get_calls,
    open_calls,
    resolved_symbols,
)

__all__ = [
    "calls_to",
    "dataclass_field_names",
    "enum_member_names",
    "exact_keys_comparisons",
    "forbidden_import_hits",
    "helper_call_count",
    "helper_call_operands",
    "keyword_only_defaults",
    "mapping_get_calls",
    "open_calls",
    "resolved_symbols",
    "scheduler_data_attribute_reads",
    "signature_shape",
    "subscripted_names",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def _method_node(source: str, class_name: str, method_name: str) -> ast.FunctionDef:
    """**限定在类内**查找方法（不限定类名会匹配到别处的同名函数）。"""
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(f"未找到 {class_name}.{method_name}")


def scheduler_data_attribute_reads(source: str) -> set[str]:
    """``scheduler.py`` 的**数据属性读取**名（L1）。

    两类**不算"读取输入字段"**，因此被排除：

    1. ``DispatchAction.<成员>`` —— **枚举常量引用**；
    2. 直接作为调用目标的属性（``decisions.append(...)``）—— **方法调用**。

    ⚠️ 注意：``bundle.goal.strip()`` 里的 ``goal`` **仍会被抓到**（它不是那次 Call 的 func），
    因此"读输入字段"这条纪律没有被削弱 —— 探针逐一证明（``plan.mode`` / ``spec.read_only`` /
    ``bundle.goal`` / ``facts.duplicate.related_branch_id`` 都变红）。
    """
    tree = _tree(source)
    call_targets = {
        id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    reads: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if id(node) in call_targets:
            continue
        if isinstance(node.value, ast.Name) and node.value.id == "DispatchAction":
            continue
        reads.add(node.attr)
    return reads


def helper_call_operands(source: str, helper_name: str) -> list[tuple[str, ...]]:
    """每次 ``helper_name(...)`` 调用的**实际操作数**（渲染回源码文本，L3）。

    "源码里存在一个 ``==``" 不算证据；L3 要求证明那次比较**作用在真实操作数上**。
    """
    operands: list[tuple[str, ...]] = []
    for node in ast.walk(_tree(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == helper_name
        ):
            operands.append(tuple(ast.unparse(arg) for arg in node.args))
    return operands


def signature_shape(source: str, class_name: str, method_name: str) -> dict[str, object]:
    """方法的完整参数形状（L11）：位置参数 / keyword-only / 默认值 / ``*args`` / ``**kwargs``。"""
    fn = _method_node(source, class_name, method_name)
    args = fn.args
    return {
        "positional": [a.arg for a in (*args.posonlyargs, *args.args)],
        "kwonly": [a.arg for a in args.kwonlyargs],
        "defaults": [default is not None for default in args.kw_defaults],
        "vararg": args.vararg.arg if args.vararg else None,
        "kwarg": args.kwarg.arg if args.kwarg else None,
    }


def subscripted_names(source: str) -> set[str]:
    """被 ``name[...]`` 取值的**简单名字**集合（L12）。

    用于证明 ``scheduler.py`` 只对 ``admission_facts_by_branch`` 取值，
    **不把** ``bundles`` / ``reusable_results_by_branch`` 里的对象**取出来**。
    """
    return {
        node.value.id
        for node in ast.walk(_tree(source))
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
    }


def dataclass_field_names(source: str, class_name: str) -> tuple[str, ...]:
    """类体内的**注解字段名**（L8 的 DTO 形状锁）。"""
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return tuple(
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            )
    raise AssertionError(f"未找到类 {class_name}")


def enum_member_names(source: str, class_name: str) -> tuple[str, ...]:
    """Enum 类体内的**赋值目标名**（L8 的成员形状锁）。"""
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return tuple(
                item.targets[0].id
                for item in node.body
                if isinstance(item, ast.Assign)
                and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)
            )
    raise AssertionError(f"未找到类 {class_name}")
