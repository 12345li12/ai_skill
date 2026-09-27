"""``C2d-1 DENY 收口``（`已解析的 DENY 授权结果 → Run 收口 + 验证回执`）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§164**（Scope Review）/ **§165**（范围稿冻结）/
**§166**（实现授权）/ **§167**（实现）。

⚠️ **一律复用，不抄第二套判定**：调用点 / keyword 形态 / 绑定集合 / 时钟 / import / 容器 /
属性取值 / 签名 / 返回标注 / 返回形态 / 守卫全部转发（见下方 import 块）。
本文件**不新增任何判定**（`§166.7` 的预计 = **0 类**）：

    「唯一写入点」由 ``calls_to`` 的**白名单集合相等**表达；
    「收口终态恒定」由 ``call_keyword_shapes`` 的**实参形态**表达；
    「两个收敛事实是观测」由同一形态能力表达（必须含 ``gate_outcome.`` / ``manager.routing_run.``）；
    「零出边」由 ``referenced_names`` 的交集为空表达；
    「零时钟 / 零 try / 入参恰好 3 个」由既有 ``clock_call_hits`` / ``ast.Try`` /
    ``function_signature_shape`` 表达 —— 以上都不需要新的 AST 判定。
"""

from __future__ import annotations

from _resume_wiring_locks import (  # noqa: F401  (复用 C2c 的检测器集合，不抄第二套)
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
