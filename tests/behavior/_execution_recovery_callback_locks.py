"""``execution_recovery_callback`` 的结构锁检测器（纯函数）。

范围、体检与授权见 `tests/behavior/README_step5b.md` **§210**（Preflight）/ **§211**（Scope Review）/
**§212**（裁决登记）/ **§213**（实现授权申请）/ **§213.10** 的 **C 修正**（`LERC-6` 双层证明）。

⚠️ **一律复用，不抄第二套判定**：按**实测可用名**从既有检测器集合转发。
本文件**不新增任何判定**（`§213.7` 的预计 = **0 类**）：

    「模块级对象恰 2」由 ``module_level_function_names`` + 测试侧的类枚举表达；
    「调用点计数 / 零越界 / 零循环驱动」由 ``calls_to`` 表达；
    「零时钟」由 ``clock_call_hits`` 表达；
    「零终态」由 ``referenced_names`` 表达；
    「导入面」由 ``imported_module_tokens`` / ``forbidden_import_hits`` 表达；
    「错误 provenance（不被 catch）」「零跨调用状态（无模块级容器）」由测试侧读 AST 表达
      —— 既有集合里没有这两项检测器（沿 `LERC-6` 的"双层证明"与 `§201` 的局部 helper 先例）。
"""

from __future__ import annotations

from _adapter_locks import (  # noqa: F401  (复用 `[11]` 的检测器集合)
    clock_call_hits,
    forbidden_import_hits,
    imported_module_tokens,
)
from _composition_root_locks import (  # noqa: F401  (复用 AST 级判定集合)
    assignment_value_shapes,
    function_raise_guards,
    function_return_annotation,
    function_return_shapes,
    module_level_container_assignments,
    module_level_function_names,
    referenced_names,
)
from _pass_driver_locks import calls_to  # noqa: F401  (复用 C2d-2c 的转发面，含 calls_to)

__all__ = [
    "assignment_value_shapes",
    "calls_to",
    "clock_call_hits",
    "forbidden_import_hits",
    "function_raise_guards",
    "function_return_annotation",
    "function_return_shapes",
    "imported_module_tokens",
    "module_level_container_assignments",
    "module_level_function_names",
    "referenced_names",
]
