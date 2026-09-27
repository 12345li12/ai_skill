"""``C2a 重决策请求载体``（`[14] → RedecisionRequest` 接线）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§109**（scope review）/ **§110**（冻结）/
**§111**（实现授权）。

⚠️ **一律复用，不抄第二套判定**：调用点 / keyword 形态 / 时钟 / import / 容器 / 属性赋值 /
签名 / 返回标注 / 返回形态 / 守卫 / 赋值形态全部转发（见下方 import 块）。
本文件**只新增一类**真实需要的判定：

    ``attribute_value_shapes`` —— 模块级 ``NAME = <表达式>`` 的右侧**源码形态**。

为什么必须有它：``REDECISION_TARGETS`` 是**白名单数据**（LRT-4 的判据来源），
它必须逐一等于 A17.3 的三个可达目标 —— 而
``module_level_container_assignments`` 只回答"有没有可变容器"（tuple 不算）、
``assignment_value_shapes`` 只收**函数内/模块级**的单目标 ``Name = …`` 的**右侧整体文本**
（其实它已经能给出这个形态 —— 但它的口径是"赋值形态字典"，本函数把它收敛成
"某个模块级常量的**逐元素**文本"，这样才能断言"恰好三项、且恰是那三项"）。
"""

from __future__ import annotations

import ast

from _cost_edge_locks import (  # noqa: F401  (复用 C1 的检测器集合，不抄第二套)
    annotated_assign_names,
    annotated_name_targets,
    assignment_value_shapes,
    attribute_assignment_targets,
    call_keyword_shapes,
    call_keyword_shapes_by_caller,
    calls_to,
    clock_call_hits,
    exception_subclass_names,
    forbidden_import_hits,
    function_raise_guards,
    function_return_annotation,
    function_return_shapes,
    function_signature_shape,
    imported_module_tokens,
    imported_names,
    module_level_container_assignments,
    module_level_function_names,
    none_coalescing_shapes,
    open_calls,
    referenced_names,
)

__all__ = [
    "annotated_assign_names",
    "annotated_name_targets",
    "assignment_value_shapes",
    "attribute_assignment_targets",
    "attribute_value_shapes",
    "call_keyword_shapes",
    "call_keyword_shapes_by_caller",
    "calls_to",
    "clock_call_hits",
    "exception_subclass_names",
    "forbidden_import_hits",
    "function_raise_guards",
    "function_return_annotation",
    "function_return_shapes",
    "function_signature_shape",
    "imported_module_tokens",
    "imported_names",
    "module_level_container_assignments",
    "module_level_function_names",
    "none_coalescing_shapes",
    "open_calls",
    "referenced_names",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def attribute_value_shapes(source: str, name: str) -> tuple[str, ...]:
    """模块级 ``name = <tuple 字面量>`` 的**逐元素**源码形态（按出现顺序）。

    用途（LRT-4）：证明``REDECISION_TARGETS`` **恰好**由 A17.3 的三个可达目标组成 ——
    ``("RecoveryTarget.PLANNER", "RecoveryTarget.MODEL_ROUTER", "RecoveryTarget.CAPABILITY")``。
    非 tuple 字面量 / 找不到该名字 ⇒ 返回 ``()``（让期望值比对**必然**失败，而不是静默通过）。
    """
    for node in _tree(source).body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != name:
            continue
        value = node.value
        if not isinstance(value, ast.Tuple):
            return ()
        return tuple(ast.unparse(item) for item in value.elts)
    return ()
