"""``[13] → [14] Wiring`` 的结构锁检测器（纯函数）。

⚠️ **一律复用，不抄第二套判定**：import / 类名 / 容器 / 调用点 / 字段名 / 时钟 IO /
函数定位与形状 / 返回形态 / 属性链 / `__all__` 一致性全部转发。
本文件**只新增两类**真实需要的判定：

    ① ``property_names``            —— 类里的 ``@property`` 名（LRW-7：派生只读属性不是字段）
    ② ``attribute_chain_reads``     —— ``x.y.z`` 形态的**读取**（LRW-10：duck-typed 只读三个属性）
"""

from __future__ import annotations

import ast

from _recovery_locks import (  # noqa: F401  (re-export 供测试统一引用)
    annotated_assign_names,
    assigned_literal_items,
    attribute_chains,
    calls_to,
    class_def_names,
    clock_call_hits,
    comparison_sites,
    dataclass_field_names,
    declared_public_names,
    exception_subclass_names,
    forbidden_import_hits,
    function_call_keyword_values,
    function_node,
    function_raise_guards,
    function_return_annotation,
    function_return_shapes,
    function_signature_shape,
    handled_exception_names,
    imported_module_tokens,
    mapping_get_calls,
    module_level_container_assignments,
    module_level_function_names,
    open_calls,
    raise_guards,
    referenced_names,
)

__all__ = [
    "annotated_assign_names",
    "assigned_literal_items",
    "attribute_chain_reads",
    "attribute_chains",
    "calls_to",
    "class_def_names",
    "clock_call_hits",
    "comparison_sites",
    "dataclass_field_names",
    "declared_public_names",
    "exception_subclass_names",
    "forbidden_import_hits",
    "function_call_keyword_values",
    "function_node",
    "function_raise_guards",
    "function_return_annotation",
    "function_return_shapes",
    "function_signature_shape",
    "handled_exception_names",
    "imported_module_tokens",
    "literal_call_arguments",
    "mapping_get_calls",
    "module_level_container_assignments",
    "module_level_function_names",
    "open_calls",
    "property_names",
    "raise_guards",
    "referenced_names",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def property_names(source: str, class_name: str) -> tuple[str, ...]:
    """类里被 ``@property`` 装饰的方法名（按顺序）。

    用于证明"派生量是**只读属性**、不是存储字段"（LRW-7）：
    字段锁看 ``__dataclass_fields__``/注解，属性锁看这里，两者**不得混淆**。
    """
    for node in _tree(source).body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        found: list[str] = []
        for item in node.body:
            if not isinstance(item, ast.FunctionDef):
                continue
            for decorator in item.decorator_list:
                if isinstance(decorator, ast.Name) and decorator.id == "property":
                    found.append(item.name)
        return tuple(found)
    raise AssertionError(f"未找到类 {class_name}")


def literal_call_arguments(source: str, func_name: str) -> tuple[str, ...]:
    """调用 ``func_name(...)`` 时传入的**字符串字面量**实参（LRW-10）。

    用于锁定"duck-typed 的失败对象只准读哪三个属性"：属性名是通过
    ``_field(failure, "failure_kind")`` / ``_text_field(failure, "message")``
    这类**字面量**传进去的 —— 多读一个属性就会在这里显形。
    """
    found: list[str] = []
    for node in ast.walk(_tree(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == func_name
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append(arg.value)
    return tuple(found)


def attribute_chain_reads(source: str) -> tuple[str, ...]:
    """源码里**被读取**的属性链（``x.y`` / ``x.y.z``），按出现顺序。

    用于 LRW-10：duck-typed 的失败对象**只准读三个属性**
    （``failure.failure_kind`` / ``failure.message`` / ``failure.timestamp``，
    其中 ``failure_kind`` 还要再取一层 ``.value``）——多读一个都会显形。
    """
    found: list[str] = []
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.Attribute) and not isinstance(
            node.ctx, (ast.Store, ast.Del)
        ):
            found.append(ast.unparse(node))
    return tuple(found)
