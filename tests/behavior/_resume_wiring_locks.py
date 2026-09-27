"""``C2c 恢复写路径``（`恢复意图 → 至多一次运行态写入 + 写入事实报告`）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§155**（Scope Review）/ **§156**（范围稿冻结）
/ **§157**（实现授权）/ **§158**（Lock Amendment）。

⚠️ **一律复用，不抄第二套判定**：调用点 / keyword 形态 / 绑定集合 / 时钟 / import / 容器 /
属性取值 / 签名 / 返回标注 / 返回形态 / 守卫全部转发（见下方 import 块）。
本文件**不新增任何判定**（§157.13 ③ 的预计 = **0 类**）：

    「不推进 attempt」由 ``calls_to`` 的**调用点计数**表达（`commit_started` 命中 = 0）；
    「唯一写入点」由 ``calls_to`` 的**白名单集合相等**表达；
    「零出边」由 ``referenced_names`` 的交集为空表达；
    「零时钟」由 ``clock_call_hits`` + ``imported_module_tokens`` 表达；
    「`WAITING_INPUT → RUNNING` 写点唯一」由 ``call_keyword_shapes_by_caller`` 的
    **按调用者分组**能力表达（实测：`_terminal_wiring_locks.py:93` 已提供）——
    以上都不需要新的 AST 判定。
"""

from __future__ import annotations

from _resume_intent_locks import (  # noqa: F401  (复用 C2b-2c-3 的检测器集合，不抄第二套)
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
