"""``[08] Context Builder`` 的结构锁检测器（纯函数）。

用途有且只有两个：

1. 对**真实**生产源码 ``src/adaptive_router/context_builder.py`` 做 AST 断言；
2. 接受**合成变异源码**字符串做 probe，证明每条锁**会因目标语义变异而红**
   （变异证据因此永久留在 CI 里，不需要临时改真实文件）。

⚠️ **这些不是通用 Python 静态分析器。** 每条检测器只覆盖本项目真实写法与
`tests/behavior/README_step5b.md` §26 明确列出的绕法；证明边界写在各自 docstring 里。
"""

from __future__ import annotations

import ast
from typing import Iterable, Mapping

from _import_detectors import imported_module_tokens

__all__ = [
    "attribute_reads",
    "calls_to",
    "class_method_node",
    "construction_keyword_shapes",
    "context_bundle_construction_sites",
    "exact_keys_comparisons",
    "forbidden_import_hits",
    "function_node",
    "helper_call_count",
    "imported_symbol_bindings",
    "keyword_only_defaults",
    "mapping_get_calls",
    "open_calls",
    "output_format_expression",
    "resolved_symbols",
]


def _tree(source: str) -> ast.Module:
    return ast.parse(source)


def class_method_node(source: str, class_name: str, method_name: str) -> ast.FunctionDef:
    """**限定在类内**查找方法（L2 用）。

    ⚠️ 不限定类名会匹配到别处的同名函数（本项目已有过一次真实误配：
    `FactSource.__call__` 先于 `AdaptiveRouter.__call__` 出现 ⇒ 断言假绿）。
    """
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(f"未找到 {class_name}.{method_name}")


def function_node(source: str, name: str) -> ast.FunctionDef:
    """查找**模块级**函数（L3 的 chokepoint 必须是模块级、唯一）。"""
    for node in _tree(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到模块级函数 {name}")


def attribute_reads(source: str) -> set[str]:
    """模块内全部**属性读取名**（``x.y`` 里的 ``y``），用于 L1 的 allowlist。

    只收集 ``ast.Attribute.attr``：docstring / 注释里的字段名**不算**读取。
    """
    return {
        node.attr for node in ast.walk(_tree(source)) if isinstance(node, ast.Attribute)
    }


def keyword_only_defaults(
    source: str, class_name: str, method_name: str
) -> dict[str, bool]:
    """``method`` 的 **keyword-only** 参数 → 是否有默认值（L2）。

    返回值**恰好**列出 ``*`` 之后的参数；多一个参数、少一个参数、或任何一个带了默认值
    都会让锁定断言变红。
    """
    fn = class_method_node(source, class_name, method_name)
    return {
        arg.arg: default is not None
        for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults)
    }


def exact_keys_comparisons(
    source: str, helper_name: str = "_require_exact_keys"
) -> list[tuple[list[str], bool]]:
    """``helper_name`` 函数体内**每个比较**的 ``(运算符名列表, 两侧是否都是 set(...))``（L3）。

    L3 要求恰好一条、运算符是 ``Eq``、且两侧都是 ``set(...)`` ——
    因此"源码里有个 `==`"不算数，**必须是那次真正的 keyset 比较**。
    """
    fn = function_node(source, helper_name)
    results: list[tuple[list[str], bool]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare):
            continue
        ops = [type(op).__name__ for op in node.ops]
        sides = [node.left, *node.comparators]
        all_set = all(
            isinstance(side, ast.Call)
            and isinstance(side.func, ast.Name)
            and side.func.id == "set"
            for side in sides
        )
        results.append((ops, all_set))
    return results


def helper_call_count(source: str, helper_name: str = "_require_exact_keys") -> int:
    """模块里 ``helper_name(...)`` 被调用的次数（L3：两张映射各一次 ⇒ 2）。"""
    return sum(
        1
        for node in ast.walk(_tree(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == helper_name
    )


def _dotted(node: ast.AST) -> str:
    """把 ``a.b.c`` 形式的 ``Name`` / ``Attribute`` 链渲染成点路径（不是该形态则返回 ``""``）。"""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return ""
    parts.append(current.id)
    return ".".join(reversed(parts))


def _context_bundle_bindings(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """模块内本地 binding（L4 用）：``(名字 → ContextBundle, 名字绑定到 contracts 模块)``。

    ① ``ImportFrom`` 的 ``ContextBundle [as X]`` ⇒ ``X`` 绑定到符号 ``ContextBundle``；
    ② **只有确认绑定到 contracts 模块**的名字才进模块集合：

       - ``import pkg.contracts [as c]``（``alias.name`` 为 ``contracts`` 或以 ``.contracts`` 结尾）
       - ``from ... import contracts [as c]``（``alias.name == "contracts"``）

       ⚠️ **不得**把普通 ``ImportFrom`` 的任意符号名当模块 ——
       否则 ``import json as c`` / ``from .contracts import ModelSelection as c``
       之后的 ``c.ContextBundle()`` 会被**误报**（复审指出的假阳性）。
    ③ 模块级简单别名赋值 ``Bundle = ContextBundle`` / ``c2 = contracts`` ⇒ **一趟**传播。
    """
    symbols: dict[str, str] = {}
    contracts_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "contracts" or alias.name.endswith(".contracts"):
                    contracts_modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local = alias.asname or alias.name
                if alias.name == "ContextBundle":
                    symbols[local] = "ContextBundle"
                elif alias.name == "contracts":
                    contracts_modules.add(local)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Name)
        ):
            target = node.targets[0].id
            source_name = node.value.id
            if symbols.get(source_name) == "ContextBundle":
                symbols[target] = "ContextBundle"
            if source_name in contracts_modules:
                contracts_modules.add(target)
    return symbols, contracts_modules


def context_bundle_construction_sites(
    sources: Mapping[str, str],
) -> dict[str, list[int]]:
    """返回 ``{路径: [行号…]}``：**解析到** ``ContextBundle`` 的构造调用站点（L4）。

    只匹配 ``Call(func=Name("ContextBundle"))`` 会被两种真实绕法逃掉：

        from .contracts import ContextBundle as Bundle
        Bundle(...)                     # 名字变了

        from . import contracts
        contracts.ContextBundle(...)    # 变成属性调用

    因此这里解析**模块内本地 binding**（见 `_context_bundle_bindings`），并据此外判调用点：

    - ``Name(id)`` → 查符号表；
    - ``Attribute(…, attr="ContextBundle")`` → 要求**点路径的头名**确为
      **绑定到 contracts 模块**的名字（``contracts`` / ``c`` / ``adaptive_router`` …）。

    ⚠️ **证明边界**：**不**跟随 ``importlib`` / ``getattr`` / 跨模块 re-export；
    这不是通用静态分析器，只覆盖本项目真实写法与 README §26 列出的绕法。
    """
    found: dict[str, list[int]] = {}
    for path, source in sources.items():
        tree = _tree(source)
        symbols, contracts_modules = _context_bundle_bindings(tree)
        lines: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in symbols and symbols[func.id] == "ContextBundle":
                    lines.append(node.lineno)
            elif isinstance(func, ast.Attribute) and func.attr == "ContextBundle":
                head = _dotted(func.value).split(".")[0]
                if head and head in contracts_modules:
                    lines.append(node.lineno)
        if lines:
            found[path] = sorted(lines)
    return found


def output_format_expression(source: str) -> str:
    """``ContextBundle(...)`` 里 ``output_format=`` 值的**规范形态**（L5）。

    只识别"常量分隔符上的 ``.join(某个 Name)``"，返回例如
    ``' / '.join(BRANCH_RETURN_FIELDS)``；识别不了就返回该节点的 AST dump
    （于是锁定断言必然变红）。
    """
    for node in ast.walk(_tree(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ContextBundle"
        ):
            for keyword in node.keywords:
                if keyword.arg != "output_format":
                    continue
                value = keyword.value
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "join"
                    and isinstance(value.func.value, ast.Constant)
                    and isinstance(value.func.value.value, str)
                    and len(value.args) == 1
                    and isinstance(value.args[0], ast.Name)
                ):
                    return f"{value.func.value.value!r}.join({value.args[0].id})"
                return ast.dump(value)
    raise AssertionError("未找到 ContextBundle(...) 的 output_format= 参数")


def construction_keyword_shapes(source: str) -> dict[str, str]:
    """``ContextBundle(...)`` 每个 keyword 的**源码形态**（L6 的字面量锁）。"""
    shapes: dict[str, str] = {}
    for node in ast.walk(_tree(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ContextBundle"
        ):
            for keyword in node.keywords:
                if keyword.arg is not None:
                    shapes[keyword.arg] = ast.unparse(keyword.value)
    return shapes


def imported_symbol_bindings(source: str) -> dict[str, str]:
    """本地名 → **原始符号名**（只来自 ``ImportFrom``；``import x.y as z`` 记模块、不记符号）。"""
    bindings: dict[str, str] = {}
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bindings[alias.asname or alias.name] = alias.name
    return bindings


def resolved_symbols(source: str) -> set[str]:
    """源码里出现的**规范符号名**集合（``ImportFrom`` 的**原始名** + ``Attribute`` 末段）。

    ⚠️ 与 ``_import_detectors.imported_names()`` 的区别：后者返回的是**本地绑定名**
    （``from .contracts import Plan as P`` 只给出 ``P``），因此**看不见别名绕法** ——
    L7 必须用本函数，否则 `P(...)` 会**假绿**。
    """
    names: set[str] = set()
    for node in ast.walk(_tree(source)):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def calls_to(source: str, symbols: Iterable[str]) -> list[tuple[str, int]]:
    """**解析到**给定符号的调用站点 ``[(符号, 行号)…]``（L7）。

    覆盖 `Symbol(...)`、`Symbol as Alias` 后的 `Alias(...)`，以及 `m.Symbol(...)`。
    """
    wanted = set(symbols)
    bindings = imported_symbol_bindings(source)
    hits: list[tuple[str, int]] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            canonical = bindings[func.id] if func.id in bindings else func.id
            if canonical in wanted:
                hits.append((canonical, node.lineno))
        elif isinstance(func, ast.Attribute) and func.attr in wanted:
            hits.append((func.attr, node.lineno))
    return hits


def forbidden_import_hits(source: str, forbidden: Iterable[str]) -> set[str]:
    """命中禁用清单的 import token（L8）。

    复用行为测试共用的 import 检测器（含 ``from . import X`` 这类
    ``node.module is None`` 形式 —— 它的敏感性已由 §19 的 probe 证明）。
    """
    hits: set[str] = set()
    for token in imported_module_tokens(source):
        for bad in forbidden:
            if (
                token == bad
                or token.startswith(bad + ".")
                or token.endswith("." + bad)
                or f".{bad}." in token
            ):
                hits.add(bad)
    return hits


def mapping_get_calls(source: str) -> list[int]:
    """所有 ``.get(...)`` 调用的行号（L9：``ContextBuilder`` **一律禁止** ``Mapping.get``）。"""
    return [
        node.lineno
        for node in ast.walk(_tree(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
    ]


def open_calls(source: str) -> list[int]:
    """所有 ``open(...)`` 调用（``Name`` 或 ``.open(...)``）的行号（L10）。"""
    lines: list[int] = []
    for node in ast.walk(_tree(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open":
            lines.append(node.lineno)
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            lines.append(node.lineno)
    return lines
