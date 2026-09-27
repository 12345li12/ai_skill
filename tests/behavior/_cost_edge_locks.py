"""``C1 成本观测边``（`[14] → [15b]/[15c]` 接线）的结构锁检测器（纯函数）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§103**（scope review）/ **§104**（冻结）。

⚠️ **一律复用，不抄第二套判定**：调用点 / keyword 形态 / 时钟 / import / 容器 / 属性赋值 /
签名 / 返回标注 / 守卫 / 赋值形态全部转发（见下方 import 块）。
本文件**只新增一类**真实需要的判定：

    ``none_coalescing_shapes`` —— ``x or 0`` / ``x if x is not None else 0`` /
    ``x if x else 0`` 形态的源码文本（**LCS-5** 的"``unknown → 0``"敏感面）；
    ``annotated_name_targets`` —— **任意深度**的 ``name: T = …``（``Name`` 目标）赋值点。

为什么必须有它：``INV-07`` 要求"没测到"与"测到 0"**永不等价**；而在源码层，
把未知折成零的写法**只有那几种形态**，用 AST 一次看全比逐处人工核对可靠
（``assignment_value_shapes`` 只看赋值右侧的整体形态，看不到表达式内部的折算）。

为什么还需要 ``annotated_name_targets``：既有的 ``annotated_assign_names`` 只覆盖
**模块级** ``AnnAssign``（它的文档口径如此）⇒ **函数内**的 ``x: T = …`` 在"绑定清单"里
**不可见**，那正是 §100.7 在 Phase B 补掉的那一类洞（"先算出来再传同一个名字"）。
本文件把它补成**同一条判定**的完整形态（任意深度），两条一起断言"绑定集合精确相等"。
"""

from __future__ import annotations

import ast

from _terminal_wiring_locks import (  # noqa: F401  (复用 Phase B 的检测器集合，不抄第二套)
    annotated_assign_names,
    assignment_value_shapes,
    attribute_assignment_targets,
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
    open_calls,
    referenced_names,
)

__all__ = [
    "annotated_assign_names",
    "annotated_name_targets",
    "assignment_value_shapes",
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


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def _is_zero(node: ast.AST) -> bool:
    """字面量 ``0`` / ``0.0``（**只认字面量**：``ZERO`` 这类名字不算，避免误报）。"""
    return isinstance(node, ast.Constant) and node.value == 0


def annotated_name_targets(source: str) -> tuple[str, ...]:
    """**绑定视图**里的 ``name: T = …`` 赋值点：**模块级 + 函数体内**，按出现顺序。

    与 ``annotated_assign_names``（**只模块级**）的关系：本函数是它的**超集** ——
    额外覆盖**函数体内**的标注赋值。两者一起用，才能让"绑定清单"没有盲区
    （LCS-5 / LCS-3：事实参数**不得**被"先算再传"式重绑定）。

    ⚠️ **类体字段声明不算绑定**：``@dataclass`` 的 ``branch_id: BranchId`` 是**类型声明**，
    不是运行期赋值 —— 把它算进来只会制造假红（``ExpectedUnit`` 的三个字段就是这种情形）。
    """
    found: list[str] = []
    for node in _tree(source).body:
        if isinstance(node, ast.ClassDef):
            continue
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found.append(node.target.id)
        elif isinstance(node, ast.FunctionDef):
            for child in ast.walk(node):
                if isinstance(child, ast.AnnAssign) and isinstance(
                    child.target, ast.Name
                ):
                    found.append(child.target.id)
    return tuple(found)


def none_coalescing_shapes(source: str) -> tuple[str, ...]:
    """把"未知折成零"的三种写法抓出来（源码形态，按出现顺序）。

    覆盖：

        ``x or 0``                      → ``ast.BoolOp(Or)``，最后一个操作数是 ``0``
        ``x if x is not None else 0``   → ``ast.IfExp``，``orelse`` 是 ``0``
        ``x if x else 0``               → 同上（条件真值化）

    ⚠️ 只认**字面量** ``0``；``x or DEFAULT_ZERO`` 不是本锁的靶子
    （那种写法需要另一条锁，本边不做"猜测式"扩展 —— 见 §103.7 的 LCS-5 冻结口径）。
    """
    found: list[str] = []
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            if node.values and _is_zero(node.values[-1]):
                found.append(ast.unparse(node))
        elif isinstance(node, ast.IfExp) and _is_zero(node.orelse):
            found.append(ast.unparse(node))
    return tuple(found)
