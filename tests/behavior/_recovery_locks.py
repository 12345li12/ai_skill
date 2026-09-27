"""``[14] Recovery`` 的结构锁检测器（纯函数）。

用途与 `_validation_wiring_locks.py` 相同：① 对**真实**生产源码断言；
② 接受**合成变异源码**字符串做 probe，证明每条锁**会因目标语义变异而红**。

⚠️ **一律复用，不抄第二套判定**：import / 类名 / 容器 / 调用点 / 字段名 / 时钟 IO /
方法级守卫 / 模块级函数定位与形状 / 返回形态 / 属性链全部转发。
本文件**只新增两类**真实需要的判定：

    ① ``assigned_literal_items`` —— 模块级**字面量容器**的逐项形态（L14-4「表 == 契约」）
    ② ``comparison_sites``       —— 某个函数里**全部比较点**的 (左, 运算符, 右)（L14-5 / L14-13）
"""

from __future__ import annotations

import ast

from _validation_wiring_locks import (  # noqa: F401  (re-export 供测试统一引用)
    annotated_assign_names,
    attribute_chains,
    call_keyword_shapes,
    calls_to,
    class_def_names,
    clock_call_hits,
    dataclass_field_names,
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
    "attribute_chains",
    "call_keyword_shapes",
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
    "mapping_get_calls",
    "module_level_container_assignments",
    "module_level_function_names",
    "open_calls",
    "raise_guards",
    "referenced_names",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def declared_public_names(source: str) -> tuple[str, ...]:
    """模块 ``__all__`` 里**声明**的名字（按顺序，取真实字符串值）。

    ⚠️ 加这条的原因（复审 P1 发现的**真缺陷**）：`recovery.py` 的 `__all__` 曾残留一个
    已删除的 `REASON_BY_CLASS` ⇒ `from … import *` 会抛 `AttributeError`，
    而当时**没有任何锁**覆盖"`__all__` 与真实定义一致"。
    本检测器 + L14-15 的两条断言把这类"陈旧导出 / 漏导出"变成必红。
    """
    for node in _tree(source).body:
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id == "__all__":
                value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "__all__":
                value = node.value
        if value is None:
            continue
        return tuple(str(item) for item in ast.literal_eval(value))
    raise AssertionError("未找到模块级 __all__")


def _literal_items(value: ast.expr) -> list[str]:
    """取"字面量容器"的**逐项源码形态**（dict 项 = ``"键: 值"``）。

    覆盖 ``{...}`` / ``[...]`` / ``(...)``，并穿透 ``frozenset({...})`` 这类单参包装调用。
    """
    if isinstance(value, ast.Dict):
        return [
            f"{ast.unparse(key)}: {ast.unparse(item)}"
            for key, item in zip(value.keys, value.values)
            if key is not None
        ]
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return [ast.unparse(item) for item in value.elts]
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"frozenset", "tuple", "list", "set"}
        and len(value.args) == 1
    ):
        return _literal_items(value.args[0])
    return []


def assigned_literal_items(source: str, name: str) -> tuple[str, ...]:
    """模块级 ``name = <字面量容器>`` 的**逐项源码形态**（L14-4：表 == 契约）。

    dict 的元素形态为 ``"键: 值"``（``ast.unparse`` 的结果），因此
    "少一行 / 多一行 / 改一行"都能被逐项断言抓到。

    ⚠️ 同时覆盖 ``Assign`` 与 **``AnnAssign``**（本项目的表都带类型标注，
    如 ``FAILURE_KIND_TO_CLASS: dict[str, FailureClass] = {...}``）。
    """
    for node in _tree(source).body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if isinstance(target, ast.Name) and target.id == name and value is not None:
            return tuple(_literal_items(value))
    raise AssertionError(f"未找到模块级字面量赋值 {name}")


def comparison_sites(
    source: str, func_name: str
) -> tuple[tuple[str, str, str], ...]:
    """模块级函数内**全部比较点**的 ``(左, 运算符, 右)`` 源码形态（L14-5 / L14-13）。

    用于证明"规则只在表里"（决策函数**不得**对 ``failure_kind`` / ``failure_class``
    做等值比较）与"审计字段不参与判定"（``message`` / ``timestamp`` 不得出现在比较里）。
    """
    found: list[tuple[str, str, str]] = []
    for node in ast.walk(function_node(source, func_name)):
        if not isinstance(node, ast.Compare):
            continue
        left = ast.unparse(node.left)
        right = ast.unparse(node.comparators[0]) if node.comparators else "<none>"
        found.append((left, type(node.ops[0]).__name__, right))
    return tuple(found)
