"""``[12] Execution Results`` 的结构锁检测器（纯函数）。

用途与 `_execution_locks.py` 相同：① 对**真实**生产源码断言；② 接受**合成变异源码**字符串做
probe，证明每条锁**会因目标语义变异而红**（变异证据永久留在 CI，不改真实文件）。

⚠️ **一律复用，不抄第二套判定**：import / 时钟 / IO / 字段名 / 调用点 / 异常捕获 / 异常子类全部转发。
本文件**只新增五类**真实需要的判定：

    ① ``function_signature_shape``     —— **模块级函数**的签名形状（`[12]` 没有类，LR-2 / LR-12）
    ② ``branch_return_keywords``       —— ``BranchReturn(...)`` 的 keyword 集合（LR-7）
    ③ ``class_def_names``              —— 模块内类名（LR-5 无状态 / LR-10 无异常类型）
    ④ ``self_read_count``              —— ``self.`` 读取次数（LR-5）
    ⑤ ``module_level_container_assignments`` —— 模块级可变容器赋值（LR-5）

⚠️ **禁止子串扫描**：`execution_results.py` 的 docstring 里**故意提及** `RoutingPlan` /
`AdapterFailure` / `Validation` 等词（说明边界），子串计数会**假阳性** ⇒ 相关锁必须走 **AST**。
"""

from __future__ import annotations

import ast

from _adapter_locks import (  # noqa: F401  (re-export 供测试统一引用)
    dataclass_field_names,
    enum_member_names,
    forbidden_import_hits,
    imported_module_tokens,
    mapping_get_calls,
    modules_importing,
    open_calls,
    signature_shape,
)
from _context_locks import (  # noqa: F401
    attribute_reads,
    calls_to,
    function_node,
    keyword_only_defaults,
    resolved_symbols,
)
from _execution_locks import (  # noqa: F401
    clock_call_hits,
    exception_subclass_names,
    handled_exception_names,
)

__all__ = [
    "attribute_reads",
    "branch_return_keywords",
    "calls_to",
    "class_def_names",
    "clock_call_hits",
    "dataclass_field_names",
    "enum_member_names",
    "exception_subclass_names",
    "forbidden_import_hits",
    "function_call_keyword_values",
    "function_node",
    "function_signature_shape",
    "handled_exception_names",
    "imported_module_tokens",
    "keyword_only_defaults",
    "mapping_get_calls",
    "module_level_container_assignments",
    "modules_importing",
    "open_calls",
    "referenced_names",
    "resolved_symbols",
    "self_read_count",
    "signature_shape",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def function_signature_shape(source: str, name: str) -> dict[str, object]:
    """**模块级函数**的签名形状（与 `_scheduler_locks.signature_shape` 同格式）。

    `[12]` 是**纯函数**（没有类）⇒ 需要按模块级 `FunctionDef` 取形状。
    """
    fn = function_node(source, name)
    args = fn.args
    return {
        "positional": [a.arg for a in (*args.posonlyargs, *args.args)],
        "kwonly": [a.arg for a in args.kwonlyargs],
        "defaults": [default is not None for default in args.kw_defaults],
        "vararg": args.vararg.arg if args.vararg else None,
        "kwarg": args.kwarg.arg if args.kwarg else None,
    }


def branch_return_keywords(source: str) -> tuple[str, ...]:
    """``BranchReturn(...)`` 调用里出现的 keyword 名（LR-7：必须**恰好等于**十项白名单）。

    只收集 keyword（``**kwargs`` 的 ``arg is None`` 也算，记作 ``"**"``）——
    这样"用 ``**fields`` 绕过白名单"会被显形。
    """
    names: list[str] = []
    for node in ast.walk(_tree(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "BranchReturn"
        ):
            for keyword in node.keywords:
                names.append(keyword.arg if keyword.arg is not None else "**")
    return tuple(names)


def class_def_names(source: str) -> tuple[str, ...]:
    """模块内**全部** ``ClassDef`` 名（LR-5：`[12]` 必须为 0；LR-10：不得有异常子类）。"""
    return tuple(
        node.name for node in ast.walk(_tree(source)) if isinstance(node, ast.ClassDef)
    )


def self_read_count(source: str) -> int:
    """``self.<x>`` 形态的**属性读取**次数（LR-5：纯函数 ⇒ 必须为 0）。"""
    return sum(
        1
        for node in ast.walk(_tree(source))
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def function_call_keyword_values(
    source: str, func_name: str, keyword: str
) -> tuple[str, ...]:
    """**模块级函数**调用 ``func_name(...)`` 里某个 keyword 的实参**源码形态**（LR-8）。

    用于证明 ``actual=`` 的实参**逐一来自** ``handoff.actual_fact.*``
    （INV-04 的机械形式：计划值不得顶替实际值）。
    """
    values: list[str] = []
    for node in ast.walk(_tree(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == func_name
        ):
            for kw in node.keywords:
                if kw.arg == keyword:
                    values.append(ast.unparse(kw.value))
    return tuple(values)


def referenced_names(source: str) -> frozenset[str]:
    """源码里**被引用到的**符号名（`ImportFrom` 原名 ∪ `Name.id` ∪ `Attribute.attr`）。

    ⚠️ 与 `_context_locks.resolved_symbols` 的区别：后者**不含裸 `Name`**，
    因此 ``RoutingPlan(**kw)`` 这种"未 import 就直接调用"的形式**看不见**
    —— 本轮的真实教训：LR-4 的 probe 一度因此**假绿**。
    docstring / 注释不是 AST 节点，因此**提及不算引用**（本锁要的正是这个性质）。
    """
    names: set[str] = set()
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return frozenset(names)


def module_level_container_assignments(source: str) -> tuple[str, ...]:
    """模块级**可变容器**赋值的目标名（LR-5：纯函数 ⇒ 不得有模块级状态）。

    只统计 ``Name = [ ... ] / { ... } / { ... : ... }``（``list`` / ``dict`` / ``set`` 字面量）；
    ``tuple`` / ``frozenset`` 字面量**不算**（不可变），模块级**常量字符串**也不算。

    ⚠️ **``__all__`` 排除在外**：它是模块导出约定（每个模块都有），不是运行期状态
    —— 本轮的真实教训：不过滤 ``__all__`` 会让**每个**模块都判红。
    """
    mutable = (ast.List, ast.Dict, ast.Set)
    found: list[str] = []
    for node in _tree(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id == "__all__":
            continue
        if isinstance(node.value, mutable):
            found.append(target.id)
    return tuple(found)
