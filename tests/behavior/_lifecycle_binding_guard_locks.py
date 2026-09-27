"""``lifecycle_binding_guard`` 的结构锁检测器（**纯转发 + 4 个本地 helper**）。

⚠️ **禁止子串扫描**：生产模块 docstring 里**故意提及** ``PassDriver`` / ``materialize`` /
   ``run_id`` / ``registry`` 等词 ⇒ 本模块**一律** AST / 绑定集级判据。

⚠️ **可复用的部分一律直接复用**（`_pass_driver_locks` 的检测器集合），⛔ 不再抄一份朴素判定
   —— 与 ``_execution_recovery_callback_locks.py`` / ``_redecision_dispatch_locks.py`` 同型。
   本文件**只新增四个**本地 helper：

     · ``module_level_shape``      —— 模块级 类 / 函数 / 赋值目标 三分（⛔ 不进嵌套作用域）
     · ``module_level_annassign``  —— 模块级 ``AnnAssign`` 目标（`§216` 的 ``AnnAssign == 0`` 同源纪律）
     · ``class_method_names``      —— 某类的**直接**方法名
     · ``slots_of``                —— 某类的 ``__slots__`` 字面量（若为 tuple/list 字面量）
"""

from __future__ import annotations

import ast

from _pass_driver_locks import (  # noqa: F401  (复用 `C2d-2c` 的完整 AST 判定集合)
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
    "class_method_names",
    "clock_call_hits",
    "exception_subclass_names",
    "forbidden_import_hits",
    "function_raise_guards",
    "function_return_annotation",
    "function_return_shapes",
    "function_signature_shape",
    "imported_module_tokens",
    "imported_names",
    "module_level_annassign",
    "module_level_container_assignments",
    "module_level_function_names",
    "module_level_shape",
    "none_coalescing_shapes",
    "open_calls",
    "referenced_names",
    "slots_of",
]


def module_level_shape(source: str) -> dict[str, list[str]]:
    """模块级形状三分：``classes`` / ``functions`` / ``assigns``（⛔ 不进嵌套作用域）。

    ⚠️ ``assigns`` 同时收集 ``Assign`` 与 ``AnnAssign`` 的目标名 —— 因为"模块级可变状态"
      两种写法都能表达（`§216` 的 ``MERC-5`` / ``MERC-5b`` 已证明这一点）。
    """
    classes: list[str] = []
    functions: list[str] = []
    assigns: list[str] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.Assign):
            assigns.extend(ast.unparse(t) for t in node.targets)
        elif isinstance(node, ast.AnnAssign):
            assigns.append(ast.unparse(node.target))
    return {"classes": classes, "functions": functions, "assigns": assigns}


def module_level_annassign(source: str) -> list[str]:
    """模块级 ``AnnAssign`` 的目标名（⛔ 只模块级 · `§216` 的 ``AnnAssign == 0`` 同源纪律）。"""
    return [
        ast.unparse(node.target)
        for node in ast.parse(source).body
        if isinstance(node, ast.AnnAssign)
    ]


def class_method_names(source: str, class_name: str) -> list[str]:
    """某个类的**直接**方法名（⛔ 不含嵌套函数 · 只取一级 ``body``）。"""
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return [
                member.name
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
    raise AssertionError(f"未找到类 {class_name!r}")


def slots_of(source: str, class_name: str) -> list[str] | None:
    """某类的 ``__slots__`` 字面量（tuple / list）；非字面量或缺席 ⇒ ``None``。"""
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for member in node.body:
                if (
                    isinstance(member, ast.Assign)
                    and any(ast.unparse(t) == "__slots__" for t in member.targets)
                    and isinstance(member.value, (ast.Tuple, ast.List))
                ):
                    return [ast.literal_eval(e) for e in member.value.elts]
    return None
