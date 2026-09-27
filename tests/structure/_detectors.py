"""Step 4A.1 — 结构不变量检测器（纯函数，可对合成源码单测）。

架构依据：`docs/architecture.md` v1.0 FROZEN 第 7.2 节（INV-S3 / S4 / S5）

设计原则：

1. **不依赖仓库当前状态。** 检测器的输入是源码字符串与模块身份，
   输出是绝对模块名/边集合。这样禁止依赖边在目标模块**尚不存在**时
   依然可判 —— 不会因为 `scheduler.py` 今天不存在就天然得到空集合。
2. **可对合成源码单测。** "变异证据"因此能永久留在 CI 里，
   不需要临时修改真实仓库文件。
3. **不做字符串猜类型。** 需要真实类型对象时用 ``typing.get_type_hints``。

⚠️ 本模块只服务于结构检查，不含任何生产逻辑。
"""

from __future__ import annotations

import ast
from typing import Iterable

__all__ = [
    "module_name_for",
    "imports_from_source",
    "forbidden_edges",
    "defining_modules",
    "resolve_type_hints",
]


def module_name_for(rel_path: str, package: str) -> tuple[str, bool]:
    """把相对路径转成 ``(绝对模块名, 是否为包)``。

    ``cost/__init__.py`` → ``("adaptive_router.cost", True)``
    ``cost/ledger.py``   → ``("adaptive_router.cost.ledger", False)``
    """
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    if not parts or not parts[-1].endswith(".py"):
        raise ValueError(f"不是 Python 文件路径：{rel_path!r}")
    stem = parts[-1][:-3]
    if stem == "__init__":
        parts = parts[:-1]
        is_package = True
    else:
        parts = parts[:-1] + [stem]
        is_package = False
    return ".".join([package] + parts) if parts else package, is_package


def _resolve_relative(module_name: str, is_package: bool, level: int, module: str | None) -> str:
    """解析相对导入的基准包。

    ``level`` 是 ``import`` 语句里的点数（``from .x`` 为 1，``from ..x`` 为 2）。

    规则：以**承载该语句的包的 ``__package__``** 为基准向上退 ``level - 1`` 级。

        - 普通模块 ``a.b.c``：``__package__ == "a.b"``
        - 包 ``a.b``（``__init__.py``）：``__package__ == "a.b"``

    这样 ``cost/__init__.py`` 里的 ``from .estimator import X``
    正确解析为 ``adaptive_router.cost.estimator``，
    而不会被多退一级。
    """
    if level < 1:
        raise ValueError("相对导入的 level 至少为 1")
    container = module_name if is_package else module_name.rsplit(".", 1)[0] if "." in module_name else ""
    segments = container.split(".") if container else []
    up = level - 1
    if up > len(segments):
        # 超出顶层：Python 会报 ImportError。检测器保守地截断到顶层。
        base: list[str] = []
    else:
        base = segments[: len(segments) - up]
    if module:
        base = base + module.split(".")
    return ".".join([s for s in base if s])


def imports_from_source(
    source: str,
    *,
    module_name: str,
    is_package: bool,
) -> set[str]:
    """从源码中提取**全部显式 import 的绝对模块名**。

    必须机械覆盖以下形式（第 4A.1 节要求）：

        import adaptive_router.scheduler
        import adaptive_router.scheduler as s

        from adaptive_router import scheduler
        from adaptive_router.scheduler import Scheduler

        from .. import scheduler
        from ..scheduler import Scheduler

        from . import sibling
        from .sibling import Thing

        包 __init__.py 中的相对 import

    对 ``from X import Y`` **保守地同时产生** ``X`` 与 ``X.Y``：

        Y 是普通类/函数时 → 多出的 ``X.Y`` 不会匹配任何已知模块边；
        Y 真的是子模块时 → 不会漏掉。

    只解析语法节点，**文档字符串或注释里出现的模块名不会被当成 import**。
    """
    found: set[str] = set()

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(module_name, is_package, node.level, node.module)
            else:
                base = node.module or ""
            if not base:
                continue
            found.add(base)
            for alias in node.names:
                if alias.name == "*":
                    continue
                found.add(f"{base}.{alias.name}")
    return found


def forbidden_edges(
    imports: Iterable[str],
    forbidden_prefixes: Iterable[str],
) -> set[str]:
    """返回命中禁止前缀的 import 目标。

    直接比较**字符串前缀**，**不要求目标模块当前存在**：

        target == forbidden            → 命中
        target.startswith(forbidden+".") → 命中

    因此 ``from .. import scheduler`` 在 `scheduler.py` 尚不存在时
    依然会被判为违规依赖。
    """
    prefixes = tuple(forbidden_prefixes)
    hits: set[str] = set()
    for target in imports:
        for prefix in prefixes:
            if target == prefix or target.startswith(prefix + "."):
                hits.add(target)
                break
    return hits


def defining_modules(
    sources: dict[str, str],
    class_name: str,
) -> list[str]:
    """返回**定义**（而非导入）该顶层类的模块名，按字典序排序。

    ``sources`` 是 ``{模块名: 源码}``。
    """
    definers: list[str] = []
    for module_name, source in sources.items():
        tree = ast.parse(source)
        if any(
            isinstance(node, ast.ClassDef) and node.name == class_name
            for node in tree.body
        ):
            definers.append(module_name)
    return sorted(definers)


def resolve_type_hints(dataclass_type: type) -> dict[str, object]:
    """用 ``typing.get_type_hints`` 解析**真实类型对象**。

    ⚠️ 不做字符串匹配 —— 字符串里出现某个名字不等于该字段真的用了那个类型。
    """
    import typing

    return typing.get_type_hints(dataclass_type)
