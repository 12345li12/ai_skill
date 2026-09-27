"""[test-hygiene] 行为测试共用的 import 检测器（纯函数，可对合成源码单测）。

**为什么要收口。**
    9 个行为测试文件原先各自抄了一份「只读 ``ImportFrom.module``」的朴素检测器。
    ``from . import session`` 这种写法的 ``node.module`` 是 ``None``
    （模块名在 ``node.names`` 里），朴素实现**整条语句跳过**，依赖边被静默漏掉：

        assert "session" not in _imported_modules()   # 真违规，却**假绿**

    假绿比假红危险 —— 它让"禁止依赖"这类守卫在真正的违规写法下失效。

**纪律**（与 `tests/structure/_detectors.py` 同一套）：

    1. **只用 AST**，不做子串扫描 —— docstring / 注释 / 字符串字面量里
       出现模块名**不算** import。
    2. **保守多产** —— 宁可多给 token（可能假红），绝不漏给（假绿）。
    3. **只增不减** —— 输出是旧朴素实现的**超集**；单调性证明见
       `tests/behavior/test_import_detectors.py::TestMonotonicity`。
       因此本轮**不存在被放松的守卫**。

⚠️ 文件名**不能**叫 ``_detectors.py``：顶层模块名 ``_detectors``
   已被 ``tests/structure/_detectors.py`` 占用（该目录的 conftest 把它加进了
   ``sys.path``）。同名会让两层拿到**同一个** ``sys.modules`` 条目而串味。
"""

from __future__ import annotations

import ast

__all__ = ["imported_module_tokens", "imported_names"]


def _tree_of(source: str | ast.AST) -> ast.AST:
    """允许直接传**已解析的树**（`test_policy` 要在两棵树上做同一判定）。"""
    return source if isinstance(source, ast.AST) else ast.parse(source)


def _segments(path: str) -> list[str]:
    """模块路径的每一段都是路径上的一个包/模块。"""
    return [part for part in path.split(".") if part]


def imported_module_tokens(source: str | ast.AST) -> set[str]:
    """源码中每个 import 语句贡献的**模块路径 token** 集合。

    **只产"模块路径"与"路径段"，不产被导入的符号名。** 这条边界是本模块
    能被复用的前提 —— 守卫普遍长这样：

        assert all("os" not in name for name in _imported_modules())

    若把符号名也塞进来，``from .contracts import CostEstimate`` 会让
    ``CostEstimate`` 含有的子串 ``os`` 命中，守卫假红。因此：

        from .contracts import CostEstimate → ``contracts``（base 的尾段）
                                              —— **不产** ``CostEstimate``

    覆盖形式（每一条都有合成源码测试）：

        import a.b                     → ``a.b``、``a``、``b``
        import a.b as c                → 同上
        from . import risk             → ``risk``        ← 朴素实现漏掉的形式
        from .. import risk            → ``risk``
        from .risk import X            → ``risk``
        from adaptive_router.risk import X → ``adaptive_router.risk``、``adaptive_router``、``risk``
        from adaptive_router import risk   → ``adaptive_router``、``risk``
        from adaptive_router.contracts import X → ``adaptive_router.contracts``、
                                              ``adaptive_router``、``contracts``

    ``from A import B`` 里的 ``B`` 既可能是**符号**也可能是**子模块**，
    语法同形、静态分不清。这里按 base 的形状分两种处理：

    - base 为空（``from . import B``）或 base **不带点**（``from pkg import B``）：
      ``B`` 更可能就是子模块，**记** ``B``（这正是 ``from . import session``
      必须被看见的原因）。
    - base 带点（``from pkg.sub import B``）：``B`` 按**符号**处理，**不记**。
      已知残留：``from pkg.sub import deeper_submodule`` 这种写法不会被记成
      模块 token。这是**有意**的取舍 —— 本仓库的守卫问的都是顶层模块名
      （``risk`` / ``policy`` / ``session`` / ``cost`` …），而带点 base 的别名
      绝大多数是契约类名；把它们记进来只会制造假红（见上）。

    相对 import 的 ``level``（点数）被**有意丢弃**：守卫问的是"依赖了哪个模块"，
    而 ``from .risk`` / ``from ..risk`` 依赖的都是 ``risk``。
    """
    tokens: set[str] = set()

    for node in ast.walk(_tree_of(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                tokens.add(alias.name)
                tokens.update(_segments(alias.name))
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""  # `from . import X` 时 module is None
            if base:
                tokens.add(base)
                tokens.update(_segments(base))
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not base:
                    tokens.add(alias.name)
                elif node.level == 0 and "." not in base:
                    tokens.add(alias.name)
    return tokens


def imported_names(source: str | ast.AST) -> set[str]:
    """源码中**真实 import 进来**的标识符（不含 docstring / 字符串里的提及）。

    ``import a.b`` 绑定的是 ``a``（或用 ``as`` 指定的别名），
    ``from X import Y`` 绑定的是 ``Y`` —— 因此 ``from . import Y``
    （``node.module is None``）同样必须被看见，这里**不读** ``node.module``。
    """
    found: set[str] = set()

    for node in ast.walk(_tree_of(source)):
        if isinstance(node, ast.Import):
            found.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.update(a.asname or a.name for a in node.names)
    return found
