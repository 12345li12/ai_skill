"""``C2b-2c-1 审批问题载体 + 发出 seam``（`GateOutcome → ApprovalQuestion → emit`）
的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§139**（scope review）/ **§140**（冻结记录）/
**§141**（实现授权）。

⚠️ **一律复用，不抄第二套判定**：调用点 / keyword 形态 / 绑定集合 / 时钟 / import / 容器 /
属性取值 / 签名 / 返回标注 / 返回形态 / 守卫全部转发（见下方 import 块）。
本文件**不新增任何判定**（§141.2 的预计 = **0 类**）：

    `ApprovalQuestion` 的五字段集合与 frozen 用 ``dataclasses.fields`` 在**行为侧**断言；
    「`emit` 恰好调用一次」与「返回值原样透传」用**行为侧**计数与 ``is`` 断言；
    「不要等待」由 ``clock_call_hits`` / ``open_calls`` / 导入白名单在**结构侧**表达 ——
    以上都不需要新的 AST 判定。
"""

from __future__ import annotations

from _recovery_modify_locks import (  # noqa: F401  (复用既有检测器集合，不抄第二套)
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
