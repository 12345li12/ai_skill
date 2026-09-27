"""``[13] Validation`` 的结构锁检测器（纯函数）。

用途与 `_results_locks.py` 相同：① 对**真实**生产源码断言；② 接受**合成变异源码**字符串做
probe，证明每条锁**会因目标语义变异而红**（变异证据永久留在 CI，不改真实文件）。

⚠️ **一律复用，不抄第二套判定**：import / 时钟 / IO / 字段名 / 调用点 / 异常子类 / `self` 读取 /
方法签名 / 方法体定位全部转发。本文件**只新增四类**真实需要的判定：

    ① ``containment_checks``      —— ``ast.Compare`` 里的**成员包含**判断（LV-5 的锚定校验）
    ② ``call_positional_shapes``  —— 调用的**位置实参**源码形态（LV-4：producer 的第二个实参）
    ③ ``assignment_value_shapes`` —— ``名字 = 表达式`` 的右侧形态（LV-2 / LV-6：派生而非复制）
    ④ ``method_flow_shapes``      —— 方法体**流程骨架**（LV-4 裁定 ⑥ 的顺序 + LV-2/LV-6 的派生形态）
    ⑤ ``attribute_chains``        —— 属性链出现次数（LV-4：``bundle.completion_criteria`` 只准出现一次）

（`_results_locks.function_call_keyword_values` 只看 keyword；producer 调用用的是**位置实参**，
因此需要一个按位置看的检测器。``method_flow_shapes`` 只把
``_scheduler_locks._method_node`` 的定位能力接到 ``ast.unparse`` 上，不重写定位逻辑。）
"""

from __future__ import annotations

import ast

from _adapter_locks import (  # noqa: F401  (re-export 供测试统一引用)
    dataclass_field_names,
    forbidden_import_hits,
    imported_module_tokens,
    mapping_get_calls,
    open_calls,
)
from _agent_manager_locks import manager_self_attribute_reads  # noqa: F401
from _context_locks import calls_to  # noqa: F401
from _execution_locks import (  # noqa: F401
    clock_call_hits,
    exception_subclass_names,
    handled_exception_names,
)
from _results_locks import (  # noqa: F401
    class_def_names,
    function_call_keyword_values,
    function_signature_shape,
    module_level_container_assignments,
    referenced_names,
)
from _scheduler_locks import _method_node  # noqa: F401  (复用方法定位，不抄第二套)
from _scheduler_locks import signature_shape  # noqa: F401

__all__ = [
    "assignment_value_shapes",
    "attribute_chains",
    "call_positional_shapes",
    "calls_to",
    "class_def_names",
    "clock_call_hits",
    "containment_checks",
    "dataclass_field_names",
    "exception_subclass_names",
    "forbidden_import_hits",
    "function_call_keyword_values",
    "function_signature_shape",
    "handled_exception_names",
    "imported_module_tokens",
    "manager_self_attribute_reads",
    "mapping_get_calls",
    "method_flow_shapes",
    "module_level_container_assignments",
    "open_calls",
    "raise_guards",
    "referenced_names",
    "signature_shape",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def containment_checks(source: str) -> tuple[tuple[str, str], ...]:
    """源码里**成员包含**判断的 ``(左操作数源码, 右操作数源码)``（LV-5 的锚定校验）。

    只收单运算符的 ``x in y`` / ``x not in y``；多运算符比较被跳过（本项目不写那种形式）。
    """
    found: list[tuple[str, str]] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], (ast.In, ast.NotIn)):
            continue
        found.append((ast.unparse(node.left), ast.unparse(node.comparators[0])))
    return tuple(found)


def call_positional_shapes(source: str, attribute: str) -> tuple[tuple[str, ...], ...]:
    """``<something>.<attribute>(...)`` 调用的**位置实参**源码形态（LV-4）。

    用于证明 producer 的第二个实参是 **``spec.completion_criteria``**
    （而不是 ``bundle.completion_criteria`` —— 否则 bundle 会成为隐藏的判定来源）。
    """
    shapes: list[tuple[str, ...]] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == attribute:
            shapes.append(tuple(ast.unparse(arg) for arg in node.args))
    return tuple(shapes)


def assignment_value_shapes(source: str) -> dict[str, tuple[str, ...]]:
    """``名字 = 表达式`` 赋值里，**每个名字对应的右侧源码形态**（LV-2 / LV-6）。

    用于证明 ``passed`` / ``failed_criteria`` / ``evidence`` 是**从 verdicts 派生**的，
    而不是从分支自报状态复制的 —— 只锁"右侧长什么样"，不锁它出现在哪一行。

    ⚠️ 只收单目标 ``Name`` 赋值；同一名字多处赋值时按出现顺序**全部保留**
    （这样"多了一处赋值"本身也会让锁变红）。
    """
    found: dict[str, list[str]] = {}
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name):
            found.setdefault(target.id, []).append(ast.unparse(node.value))
    return {name: tuple(values) for name, values in found.items()}


def attribute_chains(source: str) -> tuple[str, ...]:
    """源码里**每一条属性链**的源码形态（含嵌套链的内层），按出现顺序（LV-4）。

    用于证明 ``bundle.completion_criteria`` 在 `[13]` 里**只出现一次**（一致性核对那一处）。
    注释与 docstring 不是 ``ast.Attribute`` ⇒ **天然不参与**计数。
    """
    return tuple(
        ast.unparse(node)
        for node in ast.walk(_tree(source))
        if isinstance(node, ast.Attribute)
    )


def method_flow_shapes(
    source: str, class_name: str, method_name: str
) -> tuple[tuple[str, ...], ...]:
    """方法体的**流程骨架**：每条顶层语句 → 紧凑形态元组（LV-4 裁定 ⑥ / LV-2 / LV-6）。

    形态：``("if", 条件)`` / ``("for", 目标, 迭代对象)`` / ``("assign", 名字, 右侧)`` /
    ``("return", 右侧)``。docstring 跳过（它不是流程）。

    ⚠️ **未识别的语句形态直接 AssertionError** —— 绝不静默跳过：
    否则"新增一条语句"会让顺序锁**看起来仍然通过**（这正是本项目禁止的假绿）。

    流程顺序本身是架构条款（§72.5 九步、裁定 ⑥ 的"身份 → 标准 → 取判定依据"），
    不是实现细节 ⇒ 它必须是显式的、可机械检查的。
    """
    shapes: list[tuple[str, ...]] = []
    for statement in _method_node(source, class_name, method_name).body:
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, ast.If):
            shapes.append(("if", ast.unparse(statement.test)))
        elif isinstance(statement, ast.For):
            shapes.append(
                ("for", ast.unparse(statement.target), ast.unparse(statement.iter))
            )
        elif (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            shapes.append(
                ("assign", statement.targets[0].id, ast.unparse(statement.value))
            )
        elif isinstance(statement, ast.Return):
            shapes.append(("return", ast.unparse(statement.value)))
        elif isinstance(statement, ast.Expr):
            shapes.append(("expr", ast.unparse(statement.value)))
        else:
            raise AssertionError(
                f"未识别的方法体语句形态（不得静默跳过）：{ast.unparse(statement)}"
            )
    return tuple(shapes)


def raise_guards(
    source: str, class_name: str, method_name: str
) -> tuple[tuple[str, str], ...]:
    """方法体内每个 ``raise`` 的**最近守卫条件**与异常类型名（按出现顺序，LV-4 / LV-5）。

    "显式失败"必须可机械检查：不只是"抛了 ``ContractError``"，而是**在哪个条件下**抛。
    守卫取最近一层 ``if`` 的条件 / ``for`` 的迭代对象；无守卫时记为 ``"<unconditional>"``。

    ⚠️ 没有这条锁，"把 ``raise`` 换成 ``pass``"这类**静默降级**是看不见的
    —— 包含判断还在、`for` 循环还在，但失败点没了。
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

    walk(_method_node(source, class_name, method_name), "<unconditional>")
    return tuple(found)
