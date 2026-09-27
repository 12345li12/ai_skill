"""``host_wiring``（Host 侧薄装配模块）的结构锁检测器（纯函数）。

范围、体检与授权见 `tests/behavior/README_step5b.md` **§198**（范围申请）/ **§199**（Preflight 结果）/
**§200**（实现授权申请，含 `方案 (i) 只装配不驱动` 的裁决）。

⚠️ **一律复用，不抄第二套判定**：按**实测可用名**从既有检测器集合转发
（`_adapter_locks` 的 19 项 + `_composition_root_locks` 的 AST 级判定集合 + `_pass_driver_locks`）。
本文件**不新增任何判定**（`§200.9` 的预计 = **0 类**）：

    「模块级对象恰 1」由 ``module_level_function_names`` + 测试侧的模块对象枚举表达；
    「构造点恰 1」由 ``calls_to`` 表达；
    「零 adapters」由 ``imported_module_tokens`` / ``forbidden_import_hits`` / ``modules_importing`` 表达；
    「零编排 / 不驱动 pass」由 ``calls_to`` 的**禁区集合**表达；
    「零终态」由 ``referenced_names`` 表达；
    「零时钟 / 零 `.get` / 零 `open`」由 ``clock_call_hits`` / ``mapping_get_calls`` / ``open_calls`` 表达；
    「返回既有类型」由 ``function_return_annotation`` 表达；
    「零循环」由 **测试侧** 的 AST 局部 helper 表达（既有集合里没有循环计数器 ——
    `_pass_driver_locks` 的实测也是这么处理的）；
    「`start`/工厂的 kw-only 形状」由 **测试侧**直接读 ``ast`` 表达
    （`signature_shape` 需要**类名 + 方法名**，对**模块级函数**取不到 ⇒ 见 `§201` 的实现记录）。
"""

from __future__ import annotations

from _adapter_locks import (  # noqa: F401  (复用 `[11]` 的检测器集合，不抄第二套)
    clock_call_hits,
    dataclass_field_names,
    enum_member_names,
    forbidden_import_hits,
    imported_module_tokens,
    keyword_only_defaults,
    mapping_get_calls,
    modules_importing,
    open_calls,
    signature_shape,
)
from _composition_root_locks import (  # noqa: F401  (复用 AST 级判定集合)
    function_raise_guards,
    function_return_annotation,
    function_return_shapes,
    module_level_function_names,
    referenced_names,
)
from _pass_driver_locks import (  # noqa: F401  (复用 C2d-2c 的转发面，含 calls_to)
    calls_to,
)

__all__ = [
    "calls_to",
    "clock_call_hits",
    "dataclass_field_names",
    "enum_member_names",
    "forbidden_import_hits",
    "function_raise_guards",
    "function_return_annotation",
    "function_return_shapes",
    "imported_module_tokens",
    "keyword_only_defaults",
    "mapping_get_calls",
    "module_level_function_names",
    "modules_importing",
    "open_calls",
    "referenced_names",
    "signature_shape",
]
