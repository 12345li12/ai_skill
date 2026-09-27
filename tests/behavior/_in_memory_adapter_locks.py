"""``InMemoryAdapter``（`[11]` 的生产替身）的结构锁检测器（纯函数）。

范围、裁决与冻结项见 `tests/behavior/README_step5b.md` **§191**（C2d-2d Preflight）/
**§192**（裁决登记）/ **§193**（Scope Review）/ **§194**（实现授权申请）。

⚠️ **一律复用，不抄第二套判定**：把 `[11]` 既有的检测器集合按**实测可用名**转发
（`_adapter_locks` 的 19 个可用名全量转发，本层不做筛选判断）。
本文件**不新增任何判定**（`§193.7` 的预计 = **0 类**）：

    「只做 `start`」由 ``class_method_node``（**按名取方法，取不到即失败**）+ 测试侧的
    "已定义方法全集"枚举表达；
    「零计划构造」由 ``calls_to`` 表达；
    「零时钟 / 零 `.get` / 零 `open`」由 ``clock_call_hits`` / ``mapping_get_calls`` /
    ``open_calls`` 表达；
    「导入面 ⊆ `L11-3` 白名单」由 ``imported_module_tokens`` + ``forbidden_import_hits`` 表达；
    「`start` 的形参形状」由 ``signature_shape`` + ``keyword_only_defaults`` 表达；
    「无抽象基类」由 **AST 级的类基读取** 表达 —— `_adapter_locks` **没有**类基检测器，
    而 `test_adapter_contract.py` 的 `L11-8` 已有**同一写法**
    ⇒ 本层**不**复制它（避免第二套判定），由测试文件直接读 AST。

⚠️ **一处实测记录**：本层第一稿凭想象引用了 `call_keyword_shapes` / `assignment_value_shapes` /
   `class_base_names` / `module_level_class_names` 等**并不存在**于 `_adapter_locks` 的名字
   ⇒ `ImportError`。现改为**严格按实测可用名**转发（下方 19 项，逐项来自 `dir(_adapter_locks)`）。
"""

from __future__ import annotations

from _adapter_locks import (  # noqa: F401  (复用 `[11]` 的检测器集合，不抄第二套)
    Mapping,
    annotations,
    calls_to,
    class_method_node,
    clock_call_hits,
    dataclass_field_names,
    enum_member_names,
    exact_keys_comparisons,
    forbidden_import_hits,
    helper_call_count,
    imported_module_tokens,
    keyword_only_defaults,
    manager_data_attribute_reads,
    mapping_get_calls,
    module_constant_expression,
    modules_importing,
    open_calls,
    resolved_symbols,
    signature_shape,
)

__all__ = [
    "Mapping",
    "annotations",
    "calls_to",
    "class_method_node",
    "clock_call_hits",
    "dataclass_field_names",
    "enum_member_names",
    "exact_keys_comparisons",
    "forbidden_import_hits",
    "helper_call_count",
    "imported_module_tokens",
    "keyword_only_defaults",
    "manager_data_attribute_reads",
    "mapping_get_calls",
    "module_constant_expression",
    "modules_importing",
    "open_calls",
    "resolved_symbols",
    "signature_shape",
]
