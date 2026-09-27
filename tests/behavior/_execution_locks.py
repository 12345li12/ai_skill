"""P1（``execution.py``）的结构锁检测器（纯函数）。

用途与 `_adapter_locks.py` 相同：① 对**真实**生产源码断言；② 接受**合成变异源码**字符串做
probe，证明每条锁**会因目标语义变异而红**（变异证据永久留在 CI，不改真实文件）。

⚠️ **一律复用，不抄第二套判定**：import 检测走 `_import_detectors` / `_adapter_locks.modules_importing`；
时钟/映射/IO/签名/字段名走既有 detector。本文件**只新增四类**真实需要的判定：

    ① ``handled_exception_names``   —— 是否**捕获**了不该捕获的异常（L12-14）
    ② ``exception_subclass_names``  —— 是否**新增**了异常类型（L12-15）
    ③ ``tuple_unpacking_lines``     —— 二元解包（L12-16）
    ④ ``call_keyword_shapes``       —— 调用实参的**源码形态**（L12-3）

⚠️ **禁止子串扫描**：``execution.py`` 的 docstring 里**故意提及** ``AdapterError``
（说明"不在这里捕获"这条禁令），子串计数 ≥ 1 ⇒ L12-14 必须走 **AST**。
"""

from __future__ import annotations

import ast

from _adapter_locks import (  # noqa: F401  (re-export 供测试统一引用)
    clock_call_hits,
    dataclass_field_names,
    enum_member_names,
    forbidden_import_hits,
    imported_module_tokens,
    mapping_get_calls,
    modules_importing,
    open_calls,
    signature_shape,
)
from _context_locks import calls_to, class_method_node, keyword_only_defaults  # noqa: F401

__all__ = [
    "call_keyword_shapes",
    "calls_to",
    "class_method_node",
    "clock_call_hits",
    "dataclass_field_names",
    "enum_member_names",
    "exception_subclass_names",
    "forbidden_import_hits",
    "handled_exception_names",
    "imported_module_tokens",
    "keyword_only_defaults",
    "mapping_get_calls",
    "modules_importing",
    "open_calls",
    "signature_shape",
    "tuple_unpacking_lines",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def handled_exception_names(source: str) -> tuple[str, ...]:
    """源码里**被捕获**的异常类型名（按出现顺序），裸 ``except:`` 记为 ``"<bare>"``（L12-14）。

    AST 级判定：注释与 docstring 里提到 ``AdapterError`` **不算**捕获。
    """
    caught: list[str] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if handler.type is None:
                caught.append("<bare>")
            else:
                caught.append(ast.unparse(handler.type))
    return tuple(caught)


def exception_subclass_names(source: str) -> tuple[str, ...]:
    """**自定义异常类型**的类名（基类里出现 ``Exception`` / ``Error`` 的 ``ClassDef``，L12-15）。

    用于证明 ``execution.py`` **不新增异常类型**（未覆盖结局用内建 ``TypeError``）。
    """
    names: list[str] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = [ast.unparse(base) for base in node.bases]
        if any("Exception" in base or "Error" in base for base in bases):
            names.append(node.name)
    return tuple(names)


def tuple_unpacking_lines(source: str, func_name: str) -> tuple[int, ...]:
    """``a, b = func_name()`` 形式的**二元解包**行号（L12-16）。

    这正是"时间戳来自上游、不由 P1 生成"的机械形式：
    若有人改成 ``_ = control_facts_source()`` 再自己读时钟，本函数返回空 ⇒ 断言变红。
    """
    lines: list[int] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Tuple) or len(target.elts) != 2:
            continue
        if not all(isinstance(elt, ast.Name) for elt in target.elts):
            continue
        value = node.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == func_name
        ):
            lines.append(node.lineno)
    return tuple(lines)


def call_keyword_shapes(source: str, attribute: str) -> dict[str, tuple[str, ...]]:
    """``<something>.<attribute>(...)`` 调用里每个 keyword 的**源码形态**（L12-3）。

    返回 ``{keyword 名: (出现过的源码文本…)}``。L12-3 要求 ``commit_started`` 的
    ``handle`` / ``started_at`` 实参**必须**是 ``outcome.handle`` / ``outcome.started_at``
    这种**属性读取** —— 传整个 receipt 对象会立刻在这里显形。
    """
    shapes: dict[str, list[str]] = {}
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != attribute:
            continue
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            shapes.setdefault(keyword.arg, []).append(ast.unparse(keyword.value))
    return {name: tuple(values) for name, values in shapes.items()}
