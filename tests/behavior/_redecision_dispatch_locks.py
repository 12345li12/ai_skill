"""``redecision dispatch seam`` 的结构锁检测器（纯函数）。

范围、体检与授权见 `tests/behavior/README_step5b.md` **§204**（seam 裁定 Preflight）/
**§205**（裁决登记）/ **§206**（Scope Review）/ **§207**（实现授权申请）。

⚠️ **一律复用，不抄第二套判定**：按**实测可用名**从既有检测器集合转发。
本文件**不新增任何判定**（`§207.8` 的预计 = **0 类**）：

    「模块级对象恰 2」由 ``module_level_function_names`` + 测试侧的类枚举表达；
    「`recheck` 恰 1 处 / 零编排 / 零构造」由 ``calls_to`` 表达；
    「零时钟」由 ``clock_call_hits`` 表达；
    「零终态」由 ``referenced_names`` 表达；
    「导入面」由 ``imported_module_tokens`` / ``forbidden_import_hits`` / ``modules_importing`` 表达；
    「零循环」由 **测试侧** 的 AST 局部 helper 表达（既有集合里没有循环计数器 —— 沿 `§201` 先例）；
    「函数签名形状」由 **测试侧** 直接读 ``ast``（`signature_shape` 需类名+方法名，对模块级函数取不到）。
"""

from __future__ import annotations

from _adapter_locks import (  # noqa: F401  (复用 `[11]` 的检测器集合)
    clock_call_hits,
    forbidden_import_hits,
    imported_module_tokens,
    modules_importing,
)
from _composition_root_locks import (  # noqa: F401  (复用 AST 级判定集合)
    function_raise_guards,
    function_return_annotation,
    function_return_shapes,
    module_level_function_names,
    referenced_names,
)
from _pass_driver_locks import calls_to  # noqa: F401  (复用 C2d-2c 的转发面，含 calls_to)

__all__ = [
    "calls_to",
    "clock_call_hits",
    "forbidden_import_hits",
    "function_raise_guards",
    "function_return_annotation",
    "function_return_shapes",
    "imported_module_tokens",
    "module_level_function_names",
    "modules_importing",
    "referenced_names",
]
