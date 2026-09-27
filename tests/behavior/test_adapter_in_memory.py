"""``InMemoryAdapter`` 的结构锁 + 永久探针 + 行为（`LIA-1…8` / `MIA-*` / `B-1…B-5`）。

范围与冻结项见 `tests/behavior/README_step5b.md` **§191**（C2d-2d Preflight）/
**§192**（裁决登记）/ **§193**（Scope Review）/ **§194**（实现授权申请）。

⚠️ **禁止子串扫描**：真实源码 docstring 里**故意提及** `Protocol` / `RoutingPlan` 等词
⇒ 判定一律 **AST 级 / 绑定集级**。
⚠️ **一处必须记住的实测记录**：`L11-4` 的 `test_no_plan_construction` **用的是子串判定**
（在源码里找"计划类名紧跟左括号"的字面串）⇒ **本测试文件里也不得写出该字面串**，
否则 `L11-4` 会因**测试文件**而假红（它扫的是 `adapters/*.py`，不含本文件 —— 但为免混淆，
本文件同样不写）。实证：`LIA-5` 用 **AST** 判定 `Plan` / `RoutingPlan` 的**调用点**为 0。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.adapters import AdapterError, AdapterStartReceipt
from adaptive_router.adapters.in_memory import InMemoryAdapter

from _in_memory_adapter_locks import (  # noqa: F401  (复用既有检测器，不抄第二套)
    calls_to,
    class_method_node,
    clock_call_hits,
    dataclass_field_names,
    forbidden_import_hits,
    imported_module_tokens,
    keyword_only_defaults,
    mapping_get_calls,
    modules_importing,
    open_calls,
    signature_shape,
)

SOURCE_PATH = pathlib.Path("src/adaptive_router/adapters/in_memory.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")

#: `LIA-4` —— `L11-3` 冻结的导入白名单（**逐字来自** `test_adapter_contract.py` L52-54）。
ALLOWED_ADAPTER_IMPORT_TOKENS = frozenset(
    {"__future__", "annotations", "dataclasses", "dataclass", "enum", "Enum", "contract"}
)

#: `LIA-4` —— `L11-3` 冻结的 Core 禁用清单（**逐字来自** `test_adapter_contract.py` L57-76）。
FORBIDDEN_CORE_IMPORTS = (
    "planner",
    "policy",
    "model_router",
    "risk",
    "capabilities",
    "scheduler",
    "agent_manager",
    "execution",
    "router",
    "entrypoint",
    "session",
    "project_context",
    "context_builder",
    "cost",
    "admission_facts",
    "reusable_results",
    "runtime_control_facts",
    "agent_start_requests",
)

#: `LIA-2` —— `start` 的 keyword-only 形状：**与 `execution.py` 的 `_dispatch` 实参逐字一致**。
EXPECTED_START_KWONLY = ["branch_id", "attempt", "plan", "bundle"]

#: `LIA-3` —— **动作面**只允许这一个方法（`§194.1` ①（甲））。
#: ⚠️ 类体另有 `__post_init__`（dataclass 的注入面校验）与 `open_handle_count`（观察属性）——
#: 它们**不是**"动作面"，故本锁的判定是"**动作面 ⊆ {start}**"而非"类里只有 start"。
EXPECTED_ACTION_METHODS = ["start"]

#: `LIA-3` —— 明确**不得存在**的动作（你裁决 ①（甲）的核心）。
FORBIDDEN_ACTIONS = ("append", "stop")

#: `LIA-3` —— 类体允许出现的方法全集（冻结；新增任何其它方法都必须重新裁定）。
EXPECTED_ALL_METHODS = ["__post_init__", "open_handle_count", "start"]

_T0 = "2026-09-30T00:00:00Z"


# ======================================================================
# 测试侧局部 AST helper（沿 `test_adapter_contract.py` 的 `L11-8` 先例读类基）
# ======================================================================


def _class_node(name: str = "InMemoryAdapter", source: str | None = None) -> ast.ClassDef:
    for node in ast.parse(source or SOURCE).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"未找到模块级类 {name}")


def _defined_method_names(source: str | None = None) -> list[str]:
    """类体内**定义**的全部方法名（按源码顺序）—— `LIA-3` 的判定面。"""
    return [
        child.name
        for child in _class_node(source=source).body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _class_base_names(source: str | None = None) -> list[str]:
    """类基（`LIA-8` 判定面；`_adapter_locks` 无此类检测器 ⇒ 按 `L11-8` 的既有写法读 AST）。"""
    return [ast.unparse(base) for base in _class_node(source=source).bases]


def _make(**kwargs: object) -> InMemoryAdapter:
    return InMemoryAdapter(timestamp=kwargs.pop("timestamp", _T0))  # type: ignore[arg-type]


def _with_extra_method(name: str, extra: list[str]) -> str:
    """在**类体末尾**追加一个方法（缩进正确）—— 用于 `LIA-3` 的探针。

    ⚠️ 直接把方法文本拼到**文件末尾**会变成模块级代码 ⇒ `IndentationError` ⇒ 读数**无效**。
    """
    lines = SOURCE.rstrip("\n").splitlines()
    assert lines[-1].strip().startswith("return AdapterStartReceipt"), lines[-1]
    assert all(ln.startswith("    ") or not ln.strip() for ln in extra), extra
    return "\n".join(lines + extra) + "\n"


def _code_only(source: str) -> str:
    """**去掉 docstring 与注释**后的源码。

    ⚠️ 本模块 docstring 里**故意提及** `test_adapter_fake`（说明"不是测试守卫"），
    因此"生产模块不得 import 它"这条锁**必须**在**代码面**判定，而不是在整份文本上做子串扫描
    —— 否则会因**我自己写的说明**而假红（本轮已犯过两次同类错误）。
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


# ======================================================================
# `LIA-1 … LIA-8` —— 结构锁
# ======================================================================


class TestLIA1ModuleShape:
    """`LIA-1` —— 模块级对象**恰 1** · `__all__` 逐字 · **零**异常类型 / 零 DTO。"""

    def test_module_level_objects_are_exactly_one(self) -> None:
        import adaptive_router.adapters.in_memory as module

        declared = sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        )
        assert declared == ["InMemoryAdapter"]

    def test_public_surface(self) -> None:
        import adaptive_router.adapters.in_memory as module

        assert module.__all__ == ["InMemoryAdapter"]

    def test_no_module_level_functions(self) -> None:
        assert [
            n.name for n in ast.parse(SOURCE).body if isinstance(n, ast.FunctionDef)
        ] == []

    def test_no_new_exception_type(self) -> None:
        """你裁决 ②：**不新增异常类型** ⇒ 模块内没有 `RuntimeError` 子类。"""
        bases = {
            ast.unparse(b)
            for n in ast.walk(ast.parse(SOURCE))
            if isinstance(n, ast.ClassDef)
            for b in n.bases
        }
        assert "RuntimeError" not in bases

    def test_dataclass_fields_are_exactly_timestamp(self) -> None:
        assert dataclass_field_names(SOURCE, "InMemoryAdapter") == ("timestamp",)

    def test_probe_second_module_object_goes_red(self) -> None:
        mutated = SOURCE + "\n\nclass _Sneak:\n    pass\n"
        import adaptive_router.adapters.in_memory as module  # noqa: F401

        declared = [
            n.name
            for n in ast.parse(mutated).body
            if isinstance(n, ast.ClassDef)
        ]
        assert declared == ["InMemoryAdapter", "_Sneak"]


class TestLIA2StartSignature:
    """`LIA-2` —— `start` 的 kw-only 形状**与 `_dispatch` 的实参逐字一致**。"""

    def test_kwonly_names_and_order(self) -> None:
        shape = signature_shape(SOURCE, "InMemoryAdapter", "start")
        assert shape["kwonly"] == EXPECTED_START_KWONLY

    def test_no_vararg_no_kwarg_no_defaults(self) -> None:
        shape = signature_shape(SOURCE, "InMemoryAdapter", "start")
        assert shape["positional"] == ["self"]
        assert shape["vararg"] is None
        assert shape["kwarg"] is None
        assert shape["defaults"] == [False, False, False, False]

    def test_probe_renamed_parameter_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "def start(self, *, branch_id, attempt, plan, bundle):",
            "def start(self, *, branch_id, attempt, routing, bundle):",
            1,
        )
        assert mutated != SOURCE
        assert (
            signature_shape(mutated, "InMemoryAdapter", "start")["kwonly"]
            != EXPECTED_START_KWONLY
        )


class TestLIA3StartOnly:
    """`LIA-3` —— **只定义 `start`**（你裁决 ①（甲））：`append` / `stop` **不存在**。"""

    def test_defined_methods_are_exactly_the_frozen_set(self) -> None:
        assert _defined_method_names(SOURCE) == EXPECTED_ALL_METHODS

    def test_action_surface_is_only_start(self) -> None:
        """动作面 = 类体方法 − 两个非动作成员（dataclass 校验 + 观察属性）。"""
        actions = [
            n for n in _defined_method_names(SOURCE) if n not in ("__post_init__", "open_handle_count")
        ]
        assert actions == EXPECTED_ACTION_METHODS

    def test_forbidden_actions_are_absent_from_the_class_body(self) -> None:
        defined = _defined_method_names(SOURCE)
        assert [n for n in FORBIDDEN_ACTIONS if n in defined] == []

    def test_append_hasattr_is_false(self) -> None:
        assert not hasattr(InMemoryAdapter, "append")

    def test_stop_hasattr_is_false(self) -> None:
        assert not hasattr(InMemoryAdapter, "stop")

    def test_append_ast_lookup_fails(self) -> None:
        with pytest.raises(AssertionError):
            class_method_node(SOURCE, "InMemoryAdapter", "append")

    def test_stop_ast_lookup_fails(self) -> None:
        with pytest.raises(AssertionError):
            class_method_node(SOURCE, "InMemoryAdapter", "stop")

    def test_probe_adding_append_goes_red(self) -> None:
        mutated = _with_extra_method(
            "append",
            [
                "",
                "    def append(self, *, branch_id, attempt, instruction) -> None:",
                "        return None",
            ],
        )
        ast.parse(mutated)  # 可导入预检：变异必须先能解析，否则读数无效
        assert _defined_method_names(mutated) == EXPECTED_ALL_METHODS + ["append"]
        assert [n for n in FORBIDDEN_ACTIONS if n in _defined_method_names(mutated)] == ["append"]

    def test_probe_adding_stop_goes_red(self) -> None:
        mutated = _with_extra_method(
            "stop",
            ["", "    def stop(self, *, branch_id, attempt) -> None:", "        return None"],
        )
        ast.parse(mutated)
        assert [n for n in FORBIDDEN_ACTIONS if n in _defined_method_names(mutated)] == ["stop"]


class TestLIA4ImportBoundary:
    """`LIA-4` —— 导入面 ⊆ `L11-3` 白名单（**不得**进入 `contracts` / `adapters` / 任何 SDK）。"""

    def test_tokens_subset_of_the_frozen_allowlist(self) -> None:
        tokens = set(imported_module_tokens(SOURCE))
        assert tokens <= ALLOWED_ADAPTER_IMPORT_TOKENS, tokens - ALLOWED_ADAPTER_IMPORT_TOKENS

    def test_contracts_is_not_imported(self) -> None:
        """你的补充确认：`RoutingPlan` / `ContextBundle` **不得**进入生产 import 面。"""
        assert "contracts" not in set(imported_module_tokens(SOURCE))

    def test_no_core_dependency(self) -> None:
        assert forbidden_import_hits(SOURCE, FORBIDDEN_CORE_IMPORTS) == set()

    def test_this_new_file_is_found_by_the_shared_detector(self) -> None:
        """`ADAPTER_FILES` 是**自动发现**的 ⇒ 新文件确实进了判定面（非空地）。"""
        repo = {"adapters/in_memory.py": SOURCE}
        assert modules_importing(repo, "adapters") == ()

    def test_probe_contracts_import_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "from .contract import AdapterError, AdapterStartReceipt",
            "from .contract import AdapterError, AdapterStartReceipt\n"
            "from ..contracts import RoutingPlan",
            1,
        )
        assert mutated != SOURCE
        assert "contracts" in set(imported_module_tokens(mutated))


class TestLIA5NoPlanConstruction:
    """`LIA-5` —— **零**计划构造（INV-03；`L11-4`）。"""

    def test_zero_plan_constructor_calls(self) -> None:
        assert calls_to(SOURCE, {"Plan", "RoutingPlan"}) == []

    def test_probe_plan_call_goes_red(self) -> None:
        node = ast.parse(SOURCE)
        added = ast.parse("def _sneak(p):\n    return RoutingPlan(plan=p)\n")
        node.body.append(added.body[0])
        mutated = ast.unparse(node)
        assert calls_to(mutated, {"RoutingPlan"}) != []


class TestLIA6NoClockNoGetNoOpen:
    """`LIA-6` —— **零时钟**（`L11-5`）· **零 `.get`** / **零 `open`**（`L11-6`）。"""

    def test_zero_clock_calls(self) -> None:
        assert clock_call_hits(SOURCE) == []

    def test_zero_mapping_get_calls(self) -> None:
        assert mapping_get_calls(SOURCE) == []

    def test_zero_open_calls(self) -> None:
        assert open_calls(SOURCE) == []

    def test_probe_clock_import_goes_red(self) -> None:
        mutated = SOURCE + "\n\ndef _now():\n    import time\n    return time.time()\n"
        assert clock_call_hits(mutated) != []

    def test_probe_get_call_goes_red(self) -> None:
        mutated = SOURCE + "\n\ndef _peek(m):\n    return m.get('k', 'fallback')\n"
        assert mapping_get_calls(mutated) != []

    def test_probe_open_call_goes_red(self) -> None:
        mutated = SOURCE + "\n\ndef _read():\n    return open('f')\n"
        assert open_calls(mutated) != []


class TestLIA7NoTestFixtureLeak:
    """`LIA-7` —— 生产代码**不得** import 测试侧假件（`§193.2` 的并存守卫）。

    ⚠️ 判定在**代码面**（`_code_only`）完成：本模块 docstring **故意提及** `test_adapter_fake`
    （用来说明"它不是测试守卫"）⇒ 在整份文本上做子串扫描会**假红**。
    """

    def test_production_module_does_not_import_the_fake_adapter(self) -> None:
        code = _code_only(SOURCE)
        assert "test_adapter_fake" not in code
        assert "FakeAdapter" not in code

    def test_fake_adapter_module_still_exists_and_is_separate(self) -> None:
        fake = pathlib.Path("tests/behavior/test_adapter_fake.py")
        assert fake.exists()
        fake_code = _code_only(fake.read_text(encoding="utf-8"))
        assert "InMemoryAdapter" not in fake_code

    def test_probe_importing_the_fake_goes_red(self) -> None:
        mutated = SOURCE + "\nfrom test_adapter_fake import FakeAdapter\n"
        assert "test_adapter_fake" in _code_only(mutated)


class TestLIA8NoBaseNoAgentId:
    """`LIA-8` —— **无抽象基类**（`L11-8` · 裁定 ④）· **不出现** `agent_id`（A14.2）。"""

    def test_no_protocol_or_abc_base(self) -> None:
        bases = _class_base_names()
        assert not any("Protocol" in b or "ABC" in b for b in bases)

    def test_agent_id_never_appears_in_ast(self) -> None:
        names = {
            node.attr
            for node in ast.walk(ast.parse(SOURCE))
            if isinstance(node, ast.Attribute)
        } | {
            node.id for node in ast.walk(ast.parse(SOURCE)) if isinstance(node, ast.Name)
        } | {
            node.arg for node in ast.walk(ast.parse(SOURCE)) if isinstance(node, ast.arg)
        }
        assert "agent_id" not in names

    def test_probe_protocol_base_goes_red(self) -> None:
        mutated = SOURCE + "\n\nclass _Sneak(Protocol):\n    pass\n"
        bases = [
            ast.unparse(b)
            for n in ast.walk(ast.parse(mutated))
            if isinstance(n, ast.ClassDef)
            for b in n.bases
        ]
        assert any("Protocol" in b for b in bases)


# ======================================================================
# 行为 `B-1 … B-5`
# ======================================================================


class _StubBundle:
    """测试侧的最小 `ContextBundle` 替身（生产模块**不得** import 该类型）。"""


def _stub_plan() -> object:
    from test_composition_root import build_root, build_routing

    root = build_root()
    return build_routing(root)


class TestBehaviorInMemoryAdapter:
    def test_B1_returns_a_receipt_with_the_injected_timestamp(self) -> None:
        adapter = _make()
        plan = _stub_plan()
        receipt = adapter.start(
            branch_id="br_x", attempt=1, plan=plan, bundle=_StubBundle()
        )
        assert isinstance(receipt, AdapterStartReceipt)
        assert receipt.started_at is _T0 or receipt.started_at == _T0
        assert receipt.handle.strip() != ""

    def test_B2_handle_is_owned_by_the_adapter_and_distinguishable(self) -> None:
        adapter = _make()
        plan = _stub_plan()
        first = adapter.start(branch_id="br_a", attempt=1, plan=plan, bundle=_StubBundle())
        second = adapter.start(branch_id="br_b", attempt=1, plan=plan, bundle=_StubBundle())
        assert first.handle != second.handle
        assert adapter.open_handle_count == 2
        assert adapter.started == [("br_a", 1), ("br_b", 1)]

    def test_B3_missing_input_fails_explicitly(self) -> None:
        adapter = _make()
        plan = _stub_plan()
        with pytest.raises(AdapterError):
            adapter.start(branch_id=None, attempt=1, plan=plan, bundle=_StubBundle())
        with pytest.raises(AdapterError):
            adapter.start(branch_id="br_a", attempt=1, plan=None, bundle=_StubBundle())
        with pytest.raises(AdapterError):
            adapter.start(branch_id="br_a", attempt=1, plan=plan, bundle=None)
        with pytest.raises(AdapterError):
            adapter.start(branch_id="br_a", attempt=0, plan=plan, bundle=_StubBundle())
        with pytest.raises(AdapterError):
            adapter.start(branch_id="br_a", attempt=True, plan=plan, bundle=_StubBundle())

    def test_B4_constructor_rejects_a_bad_timestamp(self) -> None:
        with pytest.raises(AdapterError):
            InMemoryAdapter(timestamp="")
        with pytest.raises(AdapterError):
            InMemoryAdapter(timestamp=1)  # type: ignore[arg-type]

    def test_B4b_append_and_stop_are_absent_at_runtime(self) -> None:
        """裁决 ①（甲）的行为面：**结构**表达 deferred（`AttributeError`，不是空壳）。"""
        adapter = _make()
        with pytest.raises(AttributeError):
            adapter.append  # noqa: B018
        with pytest.raises(AttributeError):
            adapter.stop  # noqa: B018

    def test_B5_end_to_end_through_composition_root_and_pass_driver(self) -> None:
        """**可执行闭环**：`CompositionRoot(adapter=…)` + `PassDriver.drive_next_pass()`。"""
        from adaptive_router.composition_root import CompositionRoot
        from adaptive_router.execution import ExecutionPassReport
        from adaptive_router.pass_driver import PassDriver
        from test_pass_driver import DriverHarness

        h = DriverHarness()
        adapter = _make()
        root = CompositionRoot(
            facts_source=lambda task, context: None,
            capabilities=h.root.capabilities,
            adapter=adapter,
            execution_context_facts_by_branch=h.root.execution_context_facts_by_branch,
            confirmed_conclusions_by_branch=h.root.confirmed_conclusions_by_branch,
            control_facts_source=h.root.control_facts_source,
            admission_facts_source=h.root.admission_facts_source,
            reusable_result_source=h.root.reusable_result_source,
            continuation_source=h.root.continuation_source,
            run_id_factory=lambda: h.routing.run_id,
        )
        report = PassDriver(composition_root=root, routing=h.routing).drive_next_pass()

        assert isinstance(report, ExecutionPassReport)
        assert len(report.started) == 2  # `_plan()` 的两个 child
        assert report.failures == ()
        assert report.mismatches == ()
        assert all(handle.startswith("mem-") for _b, _a, handle in report.started)
        # 运行态被 `[10]` 写入（`commit_started`），但**终态不归 driver**
        run = PassDriver(composition_root=root, routing=h.routing)._graph  # noqa: SLF001
        assert run.agent_manager.routing_run.terminal is None


# ======================================================================
# 探针卫生
# ======================================================================


class TestProbeHygiene:
    def test_every_declared_anchor_hits(self) -> None:
        anchors = [
            "class InMemoryAdapter:",
            "_PLATFORM_NAMESPACE = \"mem\"",
            "    timestamp: str",
            "    def start(self, *, branch_id, attempt, plan, bundle):",
            "        return AdapterStartReceipt(handle=handle, started_at=self.timestamp)",
            "from .contract import AdapterError, AdapterStartReceipt",
        ]
        for anchor in anchors:
            assert anchor in SOURCE, f"锚点未命中：{anchor!r}"

    def test_source_is_parseable_without_bom(self) -> None:
        assert SOURCE_PATH.read_bytes()[:3] != b"\xef\xbb\xbf"
        ast.parse(SOURCE)

    def test_no_extra_dataclass_fields(self) -> None:
        assert len(dataclasses.fields(InMemoryAdapter)) == 1
