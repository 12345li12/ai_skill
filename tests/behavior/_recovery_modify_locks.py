"""``C2b-2b-1 MODIFY 重入请求载体``（`GateOutcome → ModifyRerouteRequest`）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§127**（scope review）/ **§128**（冻结记录）/
**§129**（实现授权）。

⚠️ **一律复用，不抄第二套判定**：调用点 / keyword 形态 / 绑定集合 / 时钟 / import / 容器 /
属性取值 / 签名 / 返回标注 / 返回形态 / 守卫全部转发（见下方 import 块）。
本文件**不新增任何判定**（§129.2 的预计 = **0 类**）：

    ``ModifyRerouteRequest`` 的两字段集合与 frozen 用 ``dataclasses.fields`` 在**行为侧**断言；
    「`modification` 零取值」由 ``referenced_names`` + ``attribute_value_shapes`` 组合表达；
    「facts 未被重建」由**身份级**证据（``request.policy_facts is 入参``）在行为侧断言 ——
    以上都不需要新的 AST 判定。

⚠️ 与 `_approval_gate_locks.py` 的**转发集合差异**（§129.13 ② 已登记）：
本文件**同样**转发 ``attribute_value_shapes``（用于"属性取值处不出现 `modification`"）。
"""

from __future__ import annotations

from _approval_gate_locks import (  # noqa: F401  (复用 C2b-2a 的检测器集合，不抄第二套)
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
