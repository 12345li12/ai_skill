"""``Phase B``（``[13]/[14] → [10]`` 接线）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§99**（Phase B Implementation Scope Review）。

⚠️ **一律复用，不抄第二套判定**：import / 时钟 / IO / 调用点 / 属性链 / 属性赋值 / 模块级容器 /
异常子类 / 模块级函数定位与签名 / return 形态 / 守卫全部转发（见下方 import 块）。
本文件**只新增一类**真实需要的判定：

    ``call_keyword_shapes_by_caller`` —— ``<something>.<callee>(…)`` 的 keyword 实参**源码形态**，
    并**按所在的模块级函数分组**。

为什么必须有它（LTW-1 / LTW-2 / LTW-4 的机械形式）：

    · `_execution_locks.call_keyword_shapes` 只给"实参长什么样"，**不说**这处调用在哪个函数里；
    · `_context_locks.calls_to` 只给 ``(符号, 行号)``，**不说**实参形态；
    · 而本轮的断言**同时**需要两者：
        - 「唯一调用点 = ``converge_run``」+「四个实参**必须是同名形参本身**」（LTW-2）；
        - 「两个入口各自传给 ``_settle`` 的 ``state`` **恰好**是 COMPLETED / FAILED」（LTW-4）。
"""

from __future__ import annotations

import ast

from _terminal_locks import (  # noqa: F401  (复用 Phase A 的检测器集合，不抄第二套)
    annotated_attribute_targets,
    attribute_assignment_targets,
    attribute_chains,
    call_target_counts,
    calls_to,
    clock_call_hits,
    exception_subclass_names,
    forbidden_import_hits,
    function_raise_guards,
    imported_module_tokens,
    imported_names,
    method_call_target_counts,
    method_source_hashes,
    method_sources,
    module_level_container_assignments,
    open_calls,
    raise_guards,
    referenced_names,
    signature_shape,
)
from _validation_locks import assignment_value_shapes  # noqa: F401
from _validation_wiring_locks import (  # noqa: F401
    annotated_assign_names,
    call_keyword_shapes,
    function_node,
    function_return_annotation,
    function_return_shapes,
    function_signature_shape,
    module_level_function_names,
)

__all__ = [
    "annotated_assign_names",
    "annotated_attribute_targets",
    "assignment_value_shapes",
    "attribute_assignment_targets",
    "attribute_chains",
    "call_keyword_shapes",
    "call_keyword_shapes_by_caller",
    "call_target_counts",
    "calls_to",
    "clock_call_hits",
    "exception_subclass_names",
    "forbidden_import_hits",
    "function_node",
    "function_raise_guards",
    "function_return_annotation",
    "function_return_shapes",
    "function_signature_shape",
    "imported_module_tokens",
    "imported_names",
    "method_call_target_counts",
    "method_source_hashes",
    "method_sources",
    "module_level_container_assignments",
    "module_level_function_names",
    "open_calls",
    "raise_guards",
    "referenced_names",
    "signature_shape",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def call_keyword_shapes_by_caller(
    source: str, callee: str
) -> dict[str, dict[str, tuple[str, ...]]]:
    """``<something>.<callee>(…)`` 调用里每个 keyword 的**源码形态**，**按所在函数分组**。

    返回 ``{所在模块级函数名: {keyword 名: (出现过的实参源码文本…)}}``；
    ``**kwargs`` 形式的实参记作 ``"**"``（这样"用 ``**facts`` 绕过形状锁"会立刻显形）。

    ⚠️ 只看**模块级**函数（本轮的接线模块没有类）；嵌套函数体内的调用会归到外层函数名下。
    ⚠️ 接收者名字**不参与**匹配（``manager.close_run(…)`` 与 ``checked.close_run(…)`` 同等对待）
    —— 断言的是"**哪个函数**、用什么**形态**调用了 ``close_run``"，而不是变量叫什么。
    """
    found: dict[str, dict[str, list[str]]] = {}
    for node in _tree(source).body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            if isinstance(func, ast.Attribute):
                name: str | None = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                name = None
            if name != callee:
                continue
            bucket = found.setdefault(node.name, {})
            for keyword in child.keywords:
                key = keyword.arg if keyword.arg is not None else "**"
                bucket.setdefault(key, []).append(ast.unparse(keyword.value))
    return {
        caller: {key: tuple(values) for key, values in keywords.items()}
        for caller, keywords in found.items()
    }
