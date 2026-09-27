"""[test-hygiene] 行为测试共用 import 检测器的自测（合成源码 + 真实源码注入探针）。

背景：9 个行为测试文件原先各自抄了一份「只读 ``ImportFrom.module``」的朴素检测器。
``from . import session`` 这种写法的 ``node.module`` 是 ``None``，朴素实现整条跳过 ——
于是 ``assert "session" not in _imported_modules()`` 这种守卫**假绿**。

本文件给三类证据，**不触碰任何生产代码**：

  1. `TestImportFormCoverage` —— 各种 import 写法都被看见（合成源码）。
  2. `TestMonotonicity` —— 新检测器的输出是旧实现的**超集**（真实源码 + 合成源码），
     即本轮**只增不减**，不存在被放松的守卫。
  3. `TestRealGuardsGoRed` —— 把违规 import 注入**真实**模块源码，逐文件证明
     该文件里的守卫判据**真的会变红**（不是"检测器看起来更强"）。
  4. `TestNoReDivergence` —— 收口是**结构上**的：没有文件能再各写一份朴素实现，
     且保留的 wrapper 必须**真实委托**给共享函数（"只 import、不委托"会被判红）。

⚠️ 本文件含两份**朴素实现的复刻**（``_naive_imported_modules`` / ``_naive_imported_names``）。
   它们**故意保留旧缺陷**，只作负对照；`TestNoReDivergence` 的 ``.module`` 扫描
   因此只豁免**定义处**与**这两个函数体**（**不是**整份文件）。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from _import_detectors import imported_module_tokens, imported_names

BEHAVIOR_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = BEHAVIOR_DIR.parents[1]


def module_source(rel_path: str) -> str:
    """读**真实**被测模块源码（注入探针用）。"""
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 负对照：旧实现的复刻（保留缺陷）
# --------------------------------------------------------------------------


def _naive_imported_modules(source: str) -> set[str]:
    """**旧实现**的复刻：只读 ``ImportFrom.module``（``from . import X`` 整条跳过）。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _naive_imported_names(source: str) -> set[str]:
    """**旧实现**的复刻：``test_evaluator.py`` 原先内联的那一份（带同样前置条件）。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
    return found


# ==========================================================================
# 1. 形式覆盖
# ==========================================================================


class TestImportFormCoverage:
    @pytest.mark.parametrize(
        ("form", "expected"),
        [
            ("import adaptive_router.risk", "adaptive_router.risk"),
            ("import adaptive_router.risk as r", "adaptive_router.risk"),
            ("import os.path", "os"),
            ("from . import risk", "risk"),
            ("from .. import risk", "risk"),
            ("from .risk import requires_human_input", "risk"),
            ("from adaptive_router.risk import requires_human_input", "risk"),
            (
                "from adaptive_router.risk import requires_human_input",
                "adaptive_router.risk",
            ),
            ("from adaptive_router import risk", "risk"),
            ("from adaptive_router.contracts import RiskLevel", "contracts"),
            ("from collections.abc import Callable", "abc"),
        ],
        ids=[
            "plain",
            "plain-alias",
            "stdlib-dotted",
            "sibling-name",
            "parent-name",
            "sibling-module",
            "absolute-module-leaf",
            "absolute-module-full",
            "absolute-parent-name",
            "dotted-base-leaf",
            "stdlib-submodule-leaf",
        ],
    )
    def test_form_is_seen(self, form: str, expected: str) -> None:
        assert expected in imported_module_tokens(form + "\n"), form

    def test_star_import_does_not_break_the_detector(self) -> None:
        """``import *`` 不产模块 token（它是符号级未知，不是模块边）。"""
        assert imported_module_tokens("from .risk import *\n") == {"risk"}

    def test_dotted_base_alias_is_treated_as_a_symbol(self) -> None:
        """**有意**的边界：带点 base 的别名按**符号**处理，不进模块 token。

        理由有实测数据：``from .contracts import CostEstimate`` 若把
        ``CostEstimate`` 当模块 token，``CostEstimate`` 含有的子串 ``os``
        会命中 ``all("os" not in name ...)`` 这条守卫 —— **假红**。

        已知残留（有意）：``from pkg.sub import deeper_submodule``
        这种写法不会被记成模块 token。
        """
        tokens = imported_module_tokens("from .contracts import CostEstimate\n")
        assert tokens == {"contracts"}

    def test_no_symbol_name_leaks_into_module_tokens(self) -> None:
        """符号名不得漏进模块 token。"""
        tokens = imported_module_tokens(
            "from .contracts import CostEstimate\nfrom .estimator import CostEstimator\n"
        )
        assert tokens == {"contracts", "estimator"}, sorted(tokens)

    def test_the_module_that_triggered_the_false_red_stays_clean(self) -> None:
        """**实测回归**：``cost/wiring.py`` 的 token 里不得出现含 ``os`` 的项。

        本轮第一次实现把合成路径与符号名也当模块 token
        （``contracts.CostEstimate``、``CostEstimator``），于是该文件的
        ``all("os" not in name ...)`` 守卫**假红**。这条钉住那个教训。
        """
        tokens = imported_module_tokens(module_source("src/adaptive_router/cost/wiring.py"))
        offenders = sorted(name for name in tokens if "os" in name)
        assert offenders == [], offenders


# ==========================================================================
# 2. 盲点对照：本轮存在的理由
# ==========================================================================


BLIND_FORMS = [
    ("from . import session", "session"),
    ("from .. import session", "session"),
    ("from adaptive_router import session", "session"),
    ("from . import adapters", "adapters"),
]


class TestBlindSpot:
    @pytest.mark.parametrize(("form", "token"), BLIND_FORMS)
    def test_the_naive_replica_really_misses_it(self, form: str, token: str) -> None:
        """负对照：旧实现**确实**漏 —— 不是我们想象它会漏。"""
        assert token not in _naive_imported_modules(form + "\n"), form

    @pytest.mark.parametrize(("form", "token"), BLIND_FORMS)
    def test_the_shared_detector_sees_it(self, form: str, token: str) -> None:
        assert token in imported_module_tokens(form + "\n"), form


# ==========================================================================
# 3. 只增不减：守卫没有被放松
# ==========================================================================


REAL_MODULES = [
    "src/adaptive_router/policy.py",
    "src/adaptive_router/planner.py",
    "src/adaptive_router/model_router.py",
    "src/adaptive_router/routing_plan.py",
    "src/adaptive_router/cost/wiring.py",
    "src/adaptive_router/approval_judgment.py",
    "src/adaptive_router/approval_disclosure.py",
    "src/adaptive_router/project_context.py",
    "src/adaptive_router/router.py",
]


class TestMonotonicity:
    """新检测器的输出是旧实现的**超集** ⇒ 每条"禁止依赖"守卫只可能更严。"""

    @pytest.mark.parametrize("rel_path", REAL_MODULES)
    def test_real_module_new_is_a_superset_of_naive(self, rel_path: str) -> None:
        source = module_source(rel_path)
        naive = _naive_imported_modules(source)
        stricter = imported_module_tokens(source)
        assert naive <= stricter, sorted(naive - stricter)

    @pytest.mark.parametrize(
        "source",
        [
            "from . import a\n",
            "from .. import a\n",
            "from pkg import a\n",
            "from pkg.sub import a\n",
            "import pkg.sub\n",
            "from pkg.sub import a as b\n",
        ],
    )
    def test_synthetic_new_is_a_superset_of_naive(self, source: str) -> None:
        assert _naive_imported_modules(source) <= imported_module_tokens(source)


# ==========================================================================
# 4. 不误报：docstring / 注释 / 字符串字面量不算 import
# ==========================================================================


class TestNoFalsePositives:
    def test_docstring_mention_is_not_an_import(self) -> None:
        source = '"""说明：本模块不得依赖 session，也不得依赖 adapters。"""\n'
        assert imported_module_tokens(source) == set()
        assert imported_names(source) == set()

    def test_comment_mention_is_not_an_import(self) -> None:
        source = "# 禁止：from . import session\nx = 1\n"
        assert imported_module_tokens(source) == set()

    def test_string_literal_mention_is_not_an_import(self) -> None:
        source = 'NAME = "from . import session"\n'
        assert imported_module_tokens(source) == set()
        assert imported_names(source) == set()


# ==========================================================================
# 5. 真实守卫真的会红（逐文件注入探针）
# ==========================================================================


#: (归属测试文件, 被测模块, 该文件守卫的模块名)
GUARD_PROBES = [
    ("test_policy.py", "src/adaptive_router/policy.py", "session"),
    ("test_planner.py", "src/adaptive_router/planner.py", "risk"),
    ("test_model_router.py", "src/adaptive_router/model_router.py", "adapters"),
    ("test_routing_plan.py", "src/adaptive_router/routing_plan.py", "session"),
    ("test_cost_wiring.py", "src/adaptive_router/cost/wiring.py", "session"),
    ("test_approval_judgment.py", "src/adaptive_router/approval_judgment.py", "policy"),
    (
        "test_approval_disclosure.py",
        "src/adaptive_router/approval_disclosure.py",
        "approval_judgment",
    ),
    ("test_project_context.py", "src/adaptive_router/project_context.py", "session"),
    ("test_router.py", "src/adaptive_router/router.py", "session"),
]


class TestRealGuardsGoRed:
    """注入 probe：违规 import 一旦写进真实模块，该文件的守卫判据必然命中。

    守卫形态统一是 ``token not in _imported_modules()``（或等价的
    ``all(token not in name ...)``）；注入后 token 可见 ⇒ 守卫变红。
    """

    @pytest.mark.parametrize(
        ("owner", "rel_path", "token"),
        GUARD_PROBES,
        ids=[row[0] for row in GUARD_PROBES],
    )
    @pytest.mark.parametrize(
        "form",
        ["from . import {token}", "from adaptive_router import {token}"],
        ids=["relative", "absolute-parent"],
    )
    def test_injected_violation_is_visible(
        self, owner: str, rel_path: str, token: str, form: str
    ) -> None:
        source = module_source(rel_path) + "\n" + form.format(token=token) + "\n"
        seen = imported_module_tokens(source)
        assert token in seen, f"{owner}: 注入 {form.format(token=token)!r} 后仍不可见"
        # 负对照：旧实现对这个注入形式是瞎的
        assert token not in _naive_imported_modules(
            module_source(rel_path) + "\n" + form.format(token=token) + "\n"
        ), f"{owner}: 负对照失效 —— 旧实现本该漏掉这个形式"

    def test_the_evaluator_name_guard_gained_the_non_idiomatic_form(self) -> None:
        """`test_evaluator.py` 原先内联的检测器带 ``and node.module`` 前置条件。

        ⚠️ **诚实边界**：现实里的违规写法
        （``from .capabilities import CapabilityRegistry``）**旧实现也能看见**。
        本轮收口消除的是 ``from . import CapabilityRegistry`` 这种非惯用写法的
        静默漏判，以及"每个文件一份实现"的漂移 —— 不是"旧实现漏了现实违规"。
        """
        non_idiomatic = "from . import CapabilityRegistry\n"
        assert "CapabilityRegistry" not in _naive_imported_names(non_idiomatic)
        assert "CapabilityRegistry" in imported_names(non_idiomatic)

        realistic = "from .capabilities import CapabilityRegistry\n"
        assert "CapabilityRegistry" in _naive_imported_names(realistic)  # 旧实现也看得见
        assert "CapabilityRegistry" in imported_names(realistic)


# ==========================================================================
# 6. 收口是结构上的：不得再各写一份
# ==========================================================================


#: 允许出现 ``.module`` 属性读取的位置只有**定义处**（``_import_detectors.py``）
#: 与**负对照函数体**（见 `_NEGATIVE_CONTROL_FUNCTIONS`）；文件级豁免只有前者。
USES_SHARED_DETECTOR = [
    ("test_policy.py", {"imported_module_tokens", "imported_names"}),
    ("test_planner.py", {"imported_module_tokens", "imported_names"}),
    ("test_model_router.py", {"imported_module_tokens", "imported_names"}),
    ("test_routing_plan.py", {"imported_module_tokens"}),
    ("test_cost_wiring.py", {"imported_module_tokens", "imported_names"}),
    ("test_approval_judgment.py", {"imported_module_tokens", "imported_names"}),
    ("test_approval_disclosure.py", {"imported_module_tokens", "imported_names"}),
    ("test_project_context.py", {"imported_module_tokens", "imported_names"}),
    ("test_router.py", {"imported_module_tokens", "imported_names"}),
    ("test_evaluator.py", {"imported_module_tokens", "imported_names"}),
]


#: 负对照函数：**故意**保留旧读法，仅在这两个函数**体内**允许 ``X.module``。
_NEGATIVE_CONTROL_FUNCTIONS = ("_naive_imported_modules", "_naive_imported_names")


def _negative_control_node_ids(tree: ast.AST) -> set[int]:
    """负对照函数体内的全部节点 id（这些节点允许读 ``X.module``）。"""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in _NEGATIVE_CONTROL_FUNCTIONS:
            allowed.update(id(inner) for inner in ast.walk(node))
    return allowed


#: 保留的本地 wrapper **必须真实委托**给哪个共享函数。
#: ⚠️ 只声明"导入了什么"**不够**：``def _imported_modules(): return set()``
#:    配上保留下来的 ``from _import_detectors import imported_module_tokens``，
#:    能同时通过"import 存在"与"没有 ``.module`` 朴素实现"两条锁 ——
#:    但守卫已经变成**空的**。
WRAPPER_DELEGATION = [
    ("test_policy.py", "_modules_of", "imported_module_tokens"),
    ("test_policy.py", "_imported_names", "imported_names"),
    ("test_policy.py", "_imported_modules", "imported_module_tokens"),
    ("test_planner.py", "_imported_names", "imported_names"),
    ("test_planner.py", "_imported_modules", "imported_module_tokens"),
    ("test_model_router.py", "_imported_names", "imported_names"),
    ("test_model_router.py", "_imported_modules", "imported_module_tokens"),
    ("test_routing_plan.py", "_imported_modules", "imported_module_tokens"),
    ("test_cost_wiring.py", "_imported_modules", "imported_module_tokens"),
    ("test_cost_wiring.py", "_imported_names", "imported_names"),
    ("test_approval_judgment.py", "_imported_modules", "imported_module_tokens"),
    ("test_approval_judgment.py", "_imported_names", "imported_names"),
    ("test_approval_disclosure.py", "_imported_modules", "imported_module_tokens"),
    ("test_approval_disclosure.py", "_imported_names", "imported_names"),
    ("test_project_context.py", "_imported_modules", "imported_module_tokens"),
    ("test_project_context.py", "_imported_names", "imported_names"),
    ("test_router.py", "_imported_modules", "imported_module_tokens"),
    ("test_router.py", "_imported_names", "imported_names"),
]

#: **没有**本地 wrapper 的文件：只钉"共享函数真的被**调用**过"。
#: ⚠️ 强度声明：这条**弱于** wrapper 锁 —— 它不看返回值是从哪来的。
DIRECT_CALLS = [
    ("test_evaluator.py", {"imported_module_tokens", "imported_names"}),
]


def _returned_callees(source: str, function_name: str) -> list[str | None]:
    """``function_name`` 里**每个** ``return`` 直接返回的被调函数名（不是调用则 ``None``）。

    这就是"wrapper 是否**真实委托**"的判据 —— 比"函数体里提到过某个名字"强：
    返回表达式必须**就是**那个调用。

        from _import_detectors import x              → []（没有这个函数）
        def _imported_modules():
            return set()                             → [None]
        def _imported_modules():                     → ["imported_module_tokens"]
            \"\"\"docstring 不算语句。\"\"\"
            return imported_module_tokens(MODULE_SOURCE)
        def _imported_names():                       → [None, "imported_names"]
            if skip:
                return set()                         # 提前返回空集同样被看见
            return imported_names(MODULE_SOURCE)
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            returns = [stmt for stmt in ast.walk(node) if isinstance(stmt, ast.Return)]
            returns.sort(key=lambda stmt: stmt.lineno)  # 按**源码顺序**，不按 ast.walk 的 BFS 顺序
            callees: list[str | None] = []
            for stmt in returns:
                value = stmt.value
                func = value.func if isinstance(value, ast.Call) else None
                if isinstance(func, ast.Name):
                    callees.append(func.id)
                elif isinstance(func, ast.Attribute):
                    callees.append(func.attr)
                else:
                    callees.append(None)
            return callees
    return []


class TestNoReDivergence:
    def test_no_behavior_file_reads_the_naive_attribute_anymore(self) -> None:
        """全目录扫描：``X.module`` 这种朴素读法只允许出现在**定义处与负对照函数体内**。

        ⚠️ 这是"收口"的结构证据 —— 只要有人再抄一份朴素实现，这条就会变红
        （``ast.Attribute`` 级判定：docstring 里的提及不算）。
        豁免是**按函数体**给的、不是按文件名整份豁免 ——
        负对照文件里的其余位置同样受约束。

        非平凡性已实测：往本目录临时放一份朴素实现后，这条以
        ``['zz_probe_naive2.py:9: node.module', 'zz_probe_naive2.py:10: node.module']``
        **断言级**变红（探针文件已删）。第一次探针因写入带 BOM 而以 ``SyntaxError``
        变红 —— 那不算断言级证据，故重做。
        """
        offenders: list[str] = []
        for path in sorted(BEHAVIOR_DIR.glob("*.py")):
            if path.name == "_import_detectors.py":
                continue  # 定义处：读 ``node.module`` 正是它的实现
            tree = ast.parse(path.read_text(encoding="utf-8"))
            allowed = _negative_control_node_ids(tree)
            for node in ast.walk(tree):
                if id(node) in allowed:
                    continue
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "module"
                    and isinstance(node.value, ast.Name)
                ):
                    offenders.append(f"{path.name}:{node.lineno}: {node.value.id}.module")
        assert offenders == [], offenders

    @pytest.mark.parametrize(
        ("owner", "expected"),
        USES_SHARED_DETECTOR,
        ids=[row[0] for row in USES_SHARED_DETECTOR],
    )
    def test_each_file_imports_the_shared_detector(
        self, owner: str, expected: set[str]
    ) -> None:
        """⚠️ 本方法**自己用共用检测器**判定（顺带证明它能用于真实测试文件）。"""
        source = (BEHAVIOR_DIR / owner).read_text(encoding="utf-8")
        assert "_import_detectors" in imported_module_tokens(source), owner
        used = imported_names(source)
        assert expected <= used, (owner, sorted(expected - used))

    def test_the_naive_replica_is_a_real_replica(self) -> None:
        """负对照本身也要被钉住：它必须**保留**旧缺陷，否则第 2 节的对照是空的。"""
        assert _naive_imported_modules("from . import session\n") == set()
        assert _naive_imported_names("from . import CapabilityRegistry\n") == set()

    @pytest.mark.parametrize(
        ("owner", "wrapper", "callee"),
        WRAPPER_DELEGATION,
        ids=[f"{row[0]}::{row[1]}" for row in WRAPPER_DELEGATION],
    )
    def test_wrapper_really_delegates_to_the_shared_detector(
        self, owner: str, wrapper: str, callee: str
    ) -> None:
        """wrapper 必须**真实委托**：每个 ``return`` 都直接返回所需共享函数的结果。

        ⚠️ 这条堵的正是"只 import、不委托"的漏洞 ——
        `test_each_file_imports_the_shared_detector` 只证明 import 存在，
        ``.module`` 扫描只证明没有抄旧实现，两者都**不**能发现
        ``def _imported_modules(): return set()``（守卫会变成空的）。
        """
        source = (BEHAVIOR_DIR / owner).read_text(encoding="utf-8")
        returns = _returned_callees(source, wrapper)
        assert returns == [callee], f"{owner}::{wrapper} 的 return 实为 {returns!r}"

    def test_the_wrapper_lock_is_not_vacuous(self) -> None:
        """合成变异：用户指出的漏洞形态（只 import / ``return set()``）必须被判红。

        ⚠️ ``return set()`` **本身也是一个调用**，所以这里诚实地拿到 ``"set"`` ——
        判据是它与所需 callee **不等**，因此照样红（不是靠"看起来像调用"蒙过去）。
        """
        good = (
            "def _imported_modules():\n"
            '    """委派。"""\n'
            "    return imported_module_tokens(MODULE_SOURCE)\n"
        )
        assert _returned_callees(good, "_imported_modules") == ["imported_module_tokens"]

        # ① 只 import、没有 wrapper
        only_import = "from _import_detectors import imported_module_tokens\n"
        assert _returned_callees(only_import, "_imported_modules") == []

        # ② import 保留、wrapper 变成空集（用户指出的形态）
        empty = 'def _imported_modules():\n    """doc"""\n    return set()\n'
        assert _returned_callees(empty, "_imported_modules") == ["set"]
        assert _returned_callees(empty, "_imported_modules") != ["imported_module_tokens"]

        # ③ 返回非调用表达式
        non_call = "def _imported_modules():\n    return _CACHED\n"
        assert _returned_callees(non_call, "_imported_modules") == [None]

        # ④ 提前返回空集也躲不过（每个 return 都被检查）
        early_exit = (
            "def _imported_names():\n"
            "    if skip:\n"
            "        return set()\n"
            "    return imported_names(MODULE_SOURCE)\n"
        )
        assert _returned_callees(early_exit, "_imported_names") == ["set", "imported_names"]

    def test_files_without_wrappers_still_call_the_shared_detector(self) -> None:
        """⚠️ 强度声明：这条只证明"调用过"，**弱于** wrapper 锁。"""
        offenders: list[str] = []
        for owner, required in DIRECT_CALLS:
            source = (BEHAVIOR_DIR / owner).read_text(encoding="utf-8")
            called = {
                node.func.id
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            }
            missing = sorted(required - called)
            if missing:
                offenders.append(f"{owner}: 未调用 {missing}")
        assert offenders == [], offenders
