"""``C2d-2b RouterCore wrapper``（`决策链 + 装配 → 可注入的入口实现`）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§174**（Scope Review）/ **§175**（范围稿）/
**§176**（实现授权）。

⚠️ **一律复用，不抄第二套判定**：调用点 / keyword 形态 / 绑定集合 / 时钟 / import / 容器 /
属性取值 / 签名 / 返回标注 / 返回形态 / 守卫全部转发（见下方 import 块）。
本文件**不新增任何判定**（`§176.3` 的预计 = **0 类**）：

    「零 DTO」由 ``ast.ClassDef.decorator_list`` 表达；
    「不跑 pass / 不制造事实」由 ``calls_to`` 的**禁区集合**表达；
    「组合根成员访问白名单」由 AST 属性访问集合表达（见测试侧局部助手）；
    「零出边 / 零时钟 / 零 try / import 边界」由既有 ``referenced_names`` /
    ``clock_call_hits`` / ``ast.Try`` / ``imported_module_tokens`` 表达。
"""

from __future__ import annotations

from _composition_root_locks import (  # noqa: F401  (复用 C2d-2a 的检测器集合，不抄第二套)
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
