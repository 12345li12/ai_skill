"""``C2d-2a 执行编排组合根``（`外部事实 + 配置 → 装配好的执行编排对象图`）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§170**（C2d-2 Preflight）/
**§171**（C2d-2a Scope Review + 实现授权）。

⚠️ **一律复用，不抄第二套判定**：调用点 / keyword 形态 / 绑定集合 / 时钟 / import / 容器 /
属性取值 / 签名 / 返回标注 / 返回形态 / 守卫全部转发（见下方 import 块）。
本文件**不新增任何判定**（`§171.9` 的预计 = **0 类**）：

    「唯一构造点」由 ``calls_to`` / AST 调用计数表达；
    「零编排」由 ``calls_to`` 的**禁区集合**表达；
    「零事实制造」由同一能力表达（五项 seam 构造点 = 0）；
    「零出边」由 ``referenced_names`` 的交集表达；
    「零时钟 / 零 try / import 边界」由既有 ``clock_call_hits`` / ``ast.Try`` /
    ``imported_module_tokens`` 表达 —— 以上都不需要新的 AST 判定。
"""

from __future__ import annotations

from _deny_wiring_locks import (  # noqa: F401  (复用 C2d-1 的检测器集合，不抄第二套)
    annotated_assign_names,
    annotated_name_targets,
    assignment_value_shapes,
    attribute_assignment_targets,
    attribute_value_shapes,
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
