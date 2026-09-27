"""``[12] → [13] Wiring`` 的结构锁检测器（纯函数）。

用途与 `_validation_locks.py` 相同：① 对**真实**生产源码断言；
② 接受**合成变异源码**字符串做 probe，证明每条锁**会因目标语义变异而红**。

⚠️ **一律复用，不抄第二套判定**：import / 类名 / 调用点 / 字段名 / 容器 / 时钟 / IO /
异常子类 / 属性链 / 方法级守卫与流程全部转发。
本文件**只新增四类**真实需要的判定 —— 全部是"**模块级函数**"版本：

    ① ``module_level_function_names``  —— 证明入口是**模块级函数**（无实例 ⇒ 无状态）
    ② ``function_return_shapes``       —— return 的**源码形态**（LW-6：唯一 return 且返回配对载体）
    ③ ``function_return_annotation``   —— 返回**标注**（LW-6：必须标注配对载体）
    ④ ``function_raise_guards``        —— 模块级函数的**显式失败点**（LW-3）

（`_validation_locks.raise_guards` / `method_flow_shapes` 是 `[13]` 轮的封闭交付、
按 class+method 定位；本轮**不改它**，而是在这里提供同一套形态词汇的模块级版本。
`function_node` 直接复用 `_results_locks` 的定位实现。）
"""

from __future__ import annotations

import ast

from _execution_locks import call_keyword_shapes  # noqa: F401  (复用属性调用 keyword 形态)
from _results_locks import function_node  # noqa: F401  (复用模块级函数定位)
from _results_locks import function_signature_shape  # noqa: F401
from _validation_locks import (  # noqa: F401  (re-export 供测试统一引用)
    attribute_chains,
    calls_to,
    class_def_names,
    clock_call_hits,
    dataclass_field_names,
    exception_subclass_names,
    forbidden_import_hits,
    function_call_keyword_values,
    handled_exception_names,
    imported_module_tokens,
    mapping_get_calls,
    module_level_container_assignments,
    open_calls,
    raise_guards,
    referenced_names,
)

__all__ = [
    "annotated_assign_names",
    "attribute_chains",
    "call_keyword_shapes",
    "calls_to",
    "class_def_names",
    "clock_call_hits",
    "dataclass_field_names",
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


def module_level_function_names(source: str) -> tuple[str, ...]:
    """模块**顶层** ``FunctionDef`` 名（按顺序）。

    用于证明入口是**模块级纯函数**：没有实例 ⇒ 结构上不可能有实例状态 / 缓存。
    （类体里的方法**不算** —— 那正是本检测器与 `class_def_names` 的分工。）
    """
    return tuple(
        node.name
        for node in _tree(source).body
        if isinstance(node, ast.FunctionDef)
    )


def annotated_assign_names(source: str) -> tuple[str, ...]:
    """带**注解**的模块级赋值目标名（``_CACHE: dict = {}`` 这类）。

    ⚠️ 为什么需要它：`module_level_container_assignments`（`[12]` 轮的共享 detector）
    **只覆盖** ``ast.Assign``；本轮 probe 实测发现"模块级缓存写成 ``AnnAssign``"会**漏检**
    ⇒ 这里补一条同义词锁，两条一起断言为 ``()``。
    """
    found: list[str] = []
    for node in _tree(source).body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.append(node.target.id)
    return tuple(found)


def function_return_shapes(source: str, name: str) -> tuple[str, ...]:
    """模块级函数里每个 ``return`` 的**右侧源码形态**（按出现顺序，LW-6）。

    ⚠️ 没有这条锁，"把返回值从配对载体换成裸 ``ValidationRecord``"或"多一条旁路 return"
    只能靠人眼发现。
    """
    fn = function_node(source, name)
    return tuple(
        ast.unparse(node.value) if node.value is not None else "<bare>"
        for node in ast.walk(fn)
        if isinstance(node, ast.Return)
    )


def function_return_annotation(source: str, name: str) -> str:
    """模块级函数的返回**标注**源码形态（LW-6：唯一合法返回类型）。"""
    returns = function_node(source, name).returns
    if returns is None:
        return "<missing>"
    return ast.unparse(returns)


def function_raise_guards(
    source: str, name: str
) -> tuple[tuple[str, str], ...]:
    """模块级函数内每个 ``raise`` 的**最近守卫条件**与异常类型名（LW-3）。

    与 `_validation_locks.raise_guards` **同一套形态词汇**，只是定位模块级函数：
    守卫取最近一层 ``if`` 的条件 / ``for`` 的迭代对象；无守卫时记 ``"<unconditional>"``。
    """
    found: list[tuple[str, str]] = []

    def walk(node: ast.AST, guard: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If):
                walk(child, ast.unparse(child.test))
            elif isinstance(child, ast.For):
                walk(child, ast.unparse(child.iter))
            elif isinstance(child, ast.Raise):
                exc = child.exc
                if exc is None:
                    found.append((guard, "<bare>"))
                elif isinstance(exc, ast.Call):
                    found.append((guard, ast.unparse(exc.func)))
                else:
                    found.append((guard, ast.unparse(exc)))
            else:
                walk(child, guard)

    walk(function_node(source, name), "<unconditional>")
    return tuple(found)
