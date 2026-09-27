"""``completion wiring``（`外部 completion → 同一 lifecycle scope → 既有回程链`）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§236**（G3 Scope Review）/
**§237**（七项裁决）/ **§238**（范围稿）/ **§239**（实现授权申请）。

⚠️ **一律复用，不抄第二套判定**：模块级对象 / 调用点 / keyword 形态 / 绑定集合 / 导入面 /
循环 / 时钟 / 返回值 / 签名 / 守卫全部转发（见下方 import 块）。
本文件**不新增任何判定**：

    「本层守卫恰三类」由 ``module_level_function_names`` + ``function_raise_guards`` 表达；
    「顺序」由 **测试侧**局部 AST helper（调用点 ``lineno`` 比较）表达；
    「`γ′` friend read 恰一处」由 **测试侧**局部 AST helper（``Attribute(attr="_graph")`` 读取点）
    + 跨文件普查表达；
    「零自造」由 ``calls_to`` 的**禁区集合**表达；
    「exactly-once」由 ``calls_to`` 的计数表达；
    「二分形态」由 **测试侧**局部 AST helper（``If.test`` 形状）表达；
    「返回同一见证」由 ``function_return_shapes`` 表达；
    「零循环 / 零时钟 / 零 IO / 零模块级容器」由 **测试侧**局部 AST helper +
    ``clock_call_hits`` / ``open_calls`` / ``module_level_container_assignments`` 表达；
    「导入边界」由 ``imported_module_tokens`` / ``imported_names`` 表达
    —— 以上都**不需要**新的检测器谓词。

⚠️ **本文件不新增检测器**：既有集合里**没有**"取某次调用所属函数"·"取 ``If.test`` 形状"·
   "数 ``while`` / ``for``"·"取 ``_graph`` 读取点"这四项 ⇒ 它们作为**测试侧局部 AST helper**
   实现（与 `test_router_wrapper.py` 的 `_class_node` / `test_pass_driver.py` 的
   `_calls_ending_with` 同一先例），**不**并入转发层、**不**扩充检测器集合。
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
