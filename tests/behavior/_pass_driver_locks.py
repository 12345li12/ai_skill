"""``C2d-2c pass driver``（`authoritative ExecutionGraph + 单 pass 驱动`）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§181**（C2d-2c Scope Review）/
**§182**（Scope Correction）/ **§183 · §185**（`C2d-2a` extension）/ **§186**（实现授权申请）。

⚠️ **一律复用，不抄第二套判定**：模块级对象 / 调用点 / 绑定集合 / 导入面 / 调用顺序 /
循环 / 时钟 / 返回值 / 签名 / 守卫全部转发（见下方 import 块）。
本文件**不新增任何判定**（`§186.7` 的预计 = **0 类**）：

    「单 pass」由 ``calls_to``（``run_pass`` 恰 1 处）表达；
    「跨 pass 不重建」由 ``calls_to``（``drive_next_pass`` 内 ``materialize`` = 0）表达；
    「构造期一次性物化」由 ``calls_to`` + **测试侧**局部 AST helper（调用所属函数）表达；
    「零编排 / 零状态机写入」由 ``calls_to`` 的**禁区集合**表达；
    「零循环」由 **测试侧**局部 AST helper（``ast.While`` / ``ast.For`` 计数）表达；
    「零时钟 / import 边界」由 ``clock_call_hits`` / ``imported_module_tokens`` 表达；
    「不接受外部图」由 ``function_signature_shape`` 表达；
    「原样交回既有报告」由 ``function_return_annotation`` / ``function_return_shapes`` 表达
    —— 以上都**不需要**新的检测器谓词。

⚠️ **本文件不新增检测器**：既有集合（`_deny_wiring_locks` 的 **23** 个）里**没有**
   "取某次调用所属函数名"与"数循环节点"这两项 ⇒ 它们作为**测试侧局部 AST helper** 实现
   （与 `test_router_wrapper.py` 的 `_class_node` / `_statement_lines` 同一先例），
   **不**并入转发层、**不**扩充检测器集合。
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
