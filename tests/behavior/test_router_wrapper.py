"""``C2d-2b RouterCore wrapper`` —— 结构锁 **LRW2-1 – LRW2-11** + 永久 probe + 行为 **B1 – B8**。

架构依据：`docs/architecture.md` **4.1**（冻结主流程）· **1.4 / 6.4 / L47**（`[10]` 运行态唯一写者）·
**A13.2**（两阶段物化）· **INV-03**（`RoutingPlan` 不可变）· 以及 `entrypoint.py` 的
``RouterCore`` 协议形状（`__call__(task, context) -> Any`）。
范围、冻结与授权见 `tests/behavior/README_step5b.md` **§174 – §176**。

**本模块要守住的六条主张：**

  1. **包装边 ≠ 执行边**（`§176.1` A 裁定）：⛔ 不跑 pass · 不管 attempt · 不判成功失败；
  2. **零 DTO**（A 裁定）：模块级**不得**出现 `@dataclass`，复用 `RouterOutcome`；
  3. **组合根成员访问白名单**：恰好 `{decision_router, materialize}`（集合相等）；
  4. **无状态**：只持有两个注入面，**不缓存**任何执行结果；
  5. **不 import `entrypoint`**（裁定 ④）⇒ 靠**结构匹配**满足 `RouterCore`；
  6. **零时钟 / 零 `try` / 不制造事实 / 不包装**（硬监控点 1 / 2 的机械表达）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `run_pass` / `scheduler` / `entrypoint` /
`adapters` 等词 ⇒ 所有锁一律 **AST 级 / 绑定集级**。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
from typing import Any

import pytest

from adaptive_router.admission_facts import AdmissionFacts
from adaptive_router.approval_judgment import ApprovalJudgment
from adaptive_router.capabilities import CapabilityRegistry
from adaptive_router.composition_root import (
    CompositionRoot,
    CompositionRootError,
    ExecutionGraph,
)
from adaptive_router.contracts import BranchState, ProjectContext, RoutingPlan
from adaptive_router.contracts.common import new_branch_id
from adaptive_router.contracts.decision import (
    BranchSpec,
    EffortLevel,
    ExecutionMode,
    ModelSelection,
    ModelTier,
    Plan,
)
from adaptive_router.execution_context_facts import ExecutionContextFacts
from adaptive_router.router import RouterOutcome
from adaptive_router.router_wrapper import RouterWrapper, RouterWrapperError
from adaptive_router.routing_plan import RoutingPlanAssembler
from adaptive_router.runtime_control_facts import RuntimeControlFacts

from _router_wrapper_locks import (  # noqa: F401  (复用既有检测器，不抄第二套)
    calls_to,
    clock_call_hits,
    imported_module_tokens,
    module_level_function_names,
    referenced_names,
)

SOURCE_PATH = pathlib.Path("src/adaptive_router/router_wrapper.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

#: `§176.3` 冻结的模块级对象（**恰好 2 个**：异常 + 类；**0 个函数**；**0 个 DTO**）。
EXPECTED_MODULE_OBJECTS = ["RouterWrapper", "RouterWrapperError"]

#: `LRW2-5` 禁区：pass / 写态 / 终态 / 恢复 一律不得调用。
FORBIDDEN_CALLS = {
    "run_pass",
    "close_run",
    "converge_run",
    "record_outcome",
    "commit_started",
    "apply_runtime_control",
    "settle_denied_run",
    "apply_resume_intent",
    "build_resume_intent",
    "route_failed_branch",
    "validate_branch",
    "record_observation",
    "build_run_summary",
}

#: `LRW2-7` 硬监控点 2：不得构造的事实 seam / 不得包装的东西。
FORBIDDEN_FACT_CONSTRUCTIONS = {
    "RuntimeControlFacts",
    "SchedulingRuntimeFacts",
    "AdmissionFacts",
    "ExecutionContextFacts",
    "ReusableResultRef",
}

FORBIDDEN_WRAPPER_NAMES = {
    "scheduler",
    "adapter",
    "facts_source",
    "runtime_facts",
    "scheduling_runtime_facts",
    "capabilities",
}

#: `LRW2-6` 不得触碰的 attempt / 终态语义。
FORBIDDEN_TERMINAL_NAMES = {"attempt", "terminal", "is_terminal", "control_flow_converged"}

#: `LRW2-10` import 白名单（**精确相等**；按实测补齐 —— `decision` 是 `contracts.decision` 的模块 token）。
EXPECTED_IMPORT_TOKENS = {
    "__future__",
    "annotations",
    "composition_root",
    "contracts",
    "contracts.decision",
    "decision",
    "router",
    "typing",
    "Any",
    "Callable",
    "ProjectContext",
    "RoutingPlan",
    "RouterOutcome",
    "CompositionRoot",
}


# ----------------------------------------------------------------------
# 测试侧局部 AST 助手
# ----------------------------------------------------------------------


def _class_node(name: str, source: str | None = None) -> ast.ClassDef:
    """从（可变异后的）源码里取模块级类 —— **不得靠 `body[i]` 索引**（易错）。"""
    for node in ast.parse(source or SOURCE).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"未找到模块级类 {name}")


def _statement_lines(method: ast.FunctionDef) -> list[str]:
    """方法体的**语句**源码（**剥掉 docstring**）—— 用于"顺序"断言。"""
    body = method.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return [ast.unparse(node) for node in body]


def _method(node: ast.ClassDef, name: str) -> ast.FunctionDef:
    for child in node.body:
        if isinstance(child, ast.FunctionDef) and child.name == name:
            return child
    raise AssertionError(f"未找到方法 {node.name}.{name}")


def _decoration_names(node: ast.ClassDef) -> list[str]:
    return [ast.unparse(d) for d in node.decorator_list]


def _kwonly(method: ast.FunctionDef) -> tuple[list[str], list[object]]:
    return (
        [a.arg for a in method.args.kwonlyargs],
        list(method.args.kw_defaults),
    )


def _return_shapes(method: ast.FunctionDef) -> list[str]:
    return [ast.unparse(n.value) for n in ast.walk(method) if isinstance(n, ast.Return)]


def _call_names(source: str | None = None) -> list[str]:
    return [ast.unparse(n.func) for n in ast.walk(ast.parse(source or SOURCE)) if isinstance(n, ast.Call)]


def _root_member_access(source: str | None = None) -> set[str]:
    """`self._composition_root.<attr>` 的**全部**成员访问名（集合相等锁的判定面）。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source or SOURCE)):
        if not isinstance(node, ast.Attribute):
            continue
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "_composition_root":
            found.add(node.attr)
    return found


def _try_count(source: str | None = None) -> int:
    return sum(1 for n in ast.walk(ast.parse(source or SOURCE)) if isinstance(n, ast.Try))


def _mutate(old: str, new: str) -> str:
    assert old in SOURCE, f"变异锚点未命中（⇒ 读数作废）：{old!r}"
    return SOURCE.replace(old, new, 1)


# ----------------------------------------------------------------------
# 装配夹具（`C2d-2a` 的组合根 + 决策链**替身**）
# ----------------------------------------------------------------------


class _InnerRouterDouble:
    """`AdaptiveRouter` 的**测试替身**：只提供 `__call__(task, context) -> RouterOutcome`。

    ⚠️ 本替身让 `C2d-2b` 的测试**不必启动真实决策链**（那需要完整的 `RouterFacts`），
       而 `C2d-2b` 的断言面**只关心**"wrapper 如何编排既有两步 + 转交"。
       ⇒ 用真实的 `RouterOutcome`（frozen dataclass）保证 `materialize` 走**真实实现**。
    """

    def __init__(self, outcome: RouterOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    def __call__(self, task: Any, context: Any) -> RouterOutcome:
        self.calls += 1
        return self._outcome


def _plan() -> Plan:
    return Plan(
        mode=ExecutionMode.MULTI_AGENT,
        main_thread_duty="duty",
        reason="test",
        branches=(
            BranchSpec(branch_id="br_lrw2_a", goal="ga", read_only=False, completion_criteria="ca"),
            BranchSpec(branch_id="br_lrw2_b", goal="gb", read_only=False, completion_criteria="cb"),
        ),
    )


def _facts(branch_id: str) -> ExecutionContextFacts:
    return ExecutionContextFacts(
        background=(f"bg-{branch_id}",),
        constraints=("c",),
        allowed_operations=("read",),
        forbidden_operations=("write",),
    )


def _blank_admission() -> AdmissionFacts:
    return AdmissionFacts(
        duplicate=None,
        context_duplicate=None,
        evidence_sufficient=None,
        insufficient_independent_value=None,
        remaining_small=None,
        dependency_too_strong=None,
        cost_exceeds_benefit=None,
    )


class _RunIdFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"run_lrw2_{self.calls}"


class _ControlRecorder:
    def __call__(self) -> tuple[RuntimeControlFacts, str]:
        return RuntimeControlFacts(stop_branches_requested=False), "2026-09-22T00:00:00Z"


class _FakeAdapter:
    """duck-typed adapter 替身（**不得**被 wrapper 触碰 —— 本替身一旦被调用即断言失败）。"""

    def start(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("wrapper 不得触碰 adapter")

    def append(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("wrapper 不得触碰 adapter")

    def stop(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("wrapper 不得触碰 adapter")


class Harness:
    """真实 `CompositionRoot` + 决策链替身 + 回调记录器。"""

    def __init__(self, *, with_callback: bool = True, run_id_factory=None) -> None:
        branch_ids = [spec.branch_id for spec in _plan().branches]
        self.factory = run_id_factory if run_id_factory is not None else _RunIdFactory()
        self.root = CompositionRoot(
            facts_source=lambda task, context: None,
            capabilities=CapabilityRegistry.__new__(CapabilityRegistry),
            adapter=_FakeAdapter(),
            execution_context_facts_by_branch={b: _facts(b) for b in branch_ids},
            confirmed_conclusions_by_branch={b: () for b in branch_ids},
            control_facts_source=_ControlRecorder(),
            admission_facts_source=lambda _b: _blank_admission(),
            reusable_result_source=lambda _b: None,
            continuation_source=lambda: None,
            run_id_factory=self.factory,
        )
        selection = ModelSelection(tier=ModelTier.BALANCED, effort=EffortLevel.MEDIUM, reason="t")
        self.routing = RoutingPlanAssembler(run_id_factory=lambda: self.root.run_id).assemble(
            _plan(),
            main_thread_model=selection,
            branch_models=tuple((b, selection) for b in branch_ids),
        )
        self.outcome = RouterOutcome(
            plan=self.routing.plan,
            routing=self.routing,
            costs=(),
            approval=ApprovalJudgment(
                needs_approval=False,
                proposed_action="a",
                impact_scope="b",
                main_risk="c",
                recovery_method="d",
            ),
        )
        self.inner = _InnerRouterDouble(self.outcome)
        object.__setattr__(self.root, "_router", self.inner)
        self.seen: list[tuple[object, object]] = []

        def collect(root: CompositionRoot, routing: RoutingPlan) -> None:
            self.seen.append((root, routing))

        self.collector = collect
        self.wrapper = RouterWrapper(
            composition_root=self.root,
            on_execution=collect if with_callback else None,
        )


# ======================================================================
# LRW2-1 … LRW2-11 —— 结构锁
# ======================================================================


class TestLRW21ModuleShape:
    def test_module_level_objects_are_exactly_two(self) -> None:
        import adaptive_router.router_wrapper as module

        declared = sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        )
        assert declared == EXPECTED_MODULE_OBJECTS

    def test_no_module_level_functions(self) -> None:
        assert module_level_function_names(SOURCE) == ()

    def test_public_surface(self) -> None:
        import adaptive_router.router_wrapper as module

        assert module.__all__ == EXPECTED_MODULE_OBJECTS


class TestLRW22ZeroDTO:
    def test_no_dataclass_decorator_at_module_level(self) -> None:
        for node in TREE.body:
            if isinstance(node, ast.ClassDef):
                assert "dataclass" not in " ".join(_decoration_names(node)), (
                    f"{node.name} 带 @dataclass ⇒ 引入新 DTO（硬监控点 1）"
                )

    def test_dataclasses_module_is_not_even_imported(self) -> None:
        assert "dataclasses" not in imported_module_tokens(SOURCE)
        assert "dataclass" not in imported_module_tokens(SOURCE)

    def test_probe_new_dto_goes_red(self) -> None:
        mutated = _mutate(
            "class RouterWrapperError(RuntimeError):",
            "@dataclass(frozen=True)\nclass _ProbeDTO:\n    x: int\n\n\n"
            "class RouterWrapperError(RuntimeError):",
        )
        found = [
            node.name
            for node in ast.parse(mutated).body
            if isinstance(node, ast.ClassDef) and "dataclass" in " ".join(_decoration_names(node))
        ]
        assert found == ["_ProbeDTO"]


class TestLRW23SingleReturn:
    def test_call_has_exactly_one_return_of_outcome(self) -> None:
        assert _return_shapes(_method(_class_node("RouterWrapper"), "__call__")) == ["outcome"]

    def test_no_other_return_in_the_class(self) -> None:
        wrapper = _class_node("RouterWrapper")
        for child in wrapper.body:
            if isinstance(child, ast.FunctionDef) and child.name != "__call__":
                assert _return_shapes(child) == []

    def test_probe_return_graph_goes_red(self) -> None:
        mutated = _mutate("        return outcome", "        return graph")
        assert _return_shapes(_method(_class_node("RouterWrapper"), "__call__")) == ["outcome"]
        assert _return_shapes(_method(_class_node("RouterWrapper", mutated), "__call__")) == ["graph"]


class TestLRW24SignatureShape:
    def test_call_signature_matches_the_protocol_shape(self) -> None:
        call = _method(_class_node("RouterWrapper"), "__call__")
        assert [a.arg for a in call.args.args] == ["self", "task", "context"]
        assert call.args.kwonlyargs == []
        assert ast.unparse(call.returns) == "RouterOutcome"

    def test_init_has_exactly_the_two_injection_faces(self) -> None:
        init = _method(_class_node("RouterWrapper"), "__init__")
        names, defaults = _kwonly(init)
        assert names == ["composition_root", "on_execution"]
        assert defaults[0] is None
        assert ast.unparse(defaults[1]) == "None"  # on_execution=None

    def test_probe_extra_injection_face_goes_red(self) -> None:
        mutated = _mutate(
            "        on_execution: Callable[[CompositionRoot, RoutingPlan], None] | None = None,",
            "        on_execution: Callable[[CompositionRoot, RoutingPlan], None] | None = None,\n"
            "        scheduler: object = None,",
        )
        init = _method(_class_node("RouterWrapper", mutated), "__init__")
        assert _kwonly(init)[0] == ["composition_root", "on_execution", "scheduler"]


class TestLRW25NoPassExecution:
    def test_no_orchestration_entry_is_called(self) -> None:
        assert calls_to(SOURCE, FORBIDDEN_CALLS) == []

    def test_run_pass_is_never_called(self) -> None:
        assert calls_to(SOURCE, {"run_pass"}) == []

    def test_probe_run_pass_goes_red(self) -> None:
        mutated = _mutate(
            "        return outcome",
            "        self._composition_root.materialize(outcome.routing).compositor.run_pass(\n"
            "            routing=outcome.routing, bundles={}, capability=None,\n"
            "            runtime_facts=None, control_facts_source=None, first_pass=True,\n"
            "        )\n"
            "        return outcome",
        )
        assert calls_to(mutated, {"run_pass"}) != []


class TestLRW26NoAttemptOrTerminal:
    def test_no_terminal_or_attempt_name_is_bound(self) -> None:
        assert referenced_names(SOURCE) & FORBIDDEN_TERMINAL_NAMES == frozenset()

    def test_no_terminal_module_is_imported(self) -> None:
        assert imported_module_tokens(SOURCE) & {
            "agent_manager",
            "terminal_wiring",
            "deny_wiring",
            "resume_wiring",
            "recovery_wiring",
            "validation",
            "validation_wiring",
            "cost",
            "ledger_wiring",
            "scheduler",
            "execution",
            "entrypoint",
            "session",
        } == set()


class TestLRW27NoFactManufacturingOrWrapping:
    def test_no_fact_seam_is_constructed(self) -> None:
        assert calls_to(SOURCE, FORBIDDEN_FACT_CONSTRUCTIONS) == []

    def test_no_wrapper_name_is_bound(self) -> None:
        assert referenced_names(SOURCE) & FORBIDDEN_WRAPPER_NAMES == frozenset()

    def test_adapters_is_never_imported(self) -> None:
        assert "adapters" not in imported_module_tokens(SOURCE)

    def test_probe_fact_construction_goes_red(self) -> None:
        mutated = _mutate(
            "        outcome = self._composition_root.decision_router(task, context)",
            "        _wrapped = RuntimeControlFacts(stop_branches_requested=False)\n"
            "        outcome = self._composition_root.decision_router(task, context)",
        )
        assert calls_to(mutated, {"RuntimeControlFacts"}) != []


class TestLRW28RootMemberWhitelist:
    def test_root_member_access_is_exactly_the_whitelist(self) -> None:
        assert _root_member_access() == {"decision_router", "materialize"}

    def test_probe_extra_root_access_goes_red(self) -> None:
        mutated = _mutate(
            "        return outcome",
            "        _leak = self._composition_root.control_facts_source\n        return outcome",
        )
        assert _root_member_access(mutated) == {
            "decision_router",
            "materialize",
            "control_facts_source",
        }


class TestLRW29CallCounts:
    def test_materialize_called_once_and_decision_router_accessed_once(self) -> None:
        names = _call_names()
        assert names.count("self._composition_root.materialize") == 1
        assert names.count("self._composition_root.decision_router") == 1

    def test_order_is_decision_then_materialize_then_callback(self) -> None:
        statements = _statement_lines(_method(_class_node("RouterWrapper"), "__call__"))
        assert statements[0] == "outcome = self._composition_root.decision_router(task, context)"
        assert statements[1] == "self._composition_root.materialize(outcome.routing)"
        assert statements[2].startswith("if self._on_execution is not None:")
        assert statements[3] == "return outcome"


class TestLRW210ImportBoundary:
    def test_import_tokens_are_a_subset_of_the_whitelist_and_contain_no_forbidden(self) -> None:
        tokens = imported_module_tokens(SOURCE)
        assert tokens <= EXPECTED_IMPORT_TOKENS, f"越界 token：{tokens - EXPECTED_IMPORT_TOKENS}"
        assert tokens & {"entrypoint", "adapters", "composition_root.entrypoint"} == set()

    def test_entrypoint_is_not_imported(self) -> None:
        assert "entrypoint" not in imported_module_tokens(SOURCE)
        assert "from .entrypoint" not in SOURCE


class TestLRW211Hygiene:
    def test_zero_clock_and_zero_try(self) -> None:
        assert clock_call_hits(SOURCE) == []
        assert _try_count() == 0

    def test_probe_try_goes_red(self) -> None:
        mutated = _mutate(
            "        return outcome",
            "        try:\n            pass\n        except Exception:\n            pass\n"
            "        return outcome",
        )
        assert _try_count(mutated) == 1


# ======================================================================
# 行为 B1 – B8
# ======================================================================


class TestBehaviorRouterWrapper:
    def test_B1_returns_the_same_router_outcome_object(self) -> None:
        harness = Harness()
        result = harness.wrapper("task", object())
        assert result is harness.outcome
        assert harness.inner.calls == 1

    def test_B2_callback_called_once_with_root_and_routing(self) -> None:
        harness = Harness()
        harness.wrapper("task", object())
        assert len(harness.seen) == 1
        root, routing = harness.seen[0]
        assert root is harness.root
        assert routing == harness.routing
        assert routing.run_id == harness.root.run_id

    def test_B3_none_callback_still_materializes_and_returns(self) -> None:
        harness = Harness(with_callback=False)
        result = harness.wrapper("task", object())
        assert result is harness.outcome
        assert harness.seen == []

    def test_B4_missing_or_wrong_composition_root_fails_explicitly(self) -> None:
        with pytest.raises(RouterWrapperError):
            RouterWrapper(composition_root=None)  # type: ignore[arg-type]
        with pytest.raises(RouterWrapperError):
            RouterWrapper(composition_root=object())  # type: ignore[arg-type]

    def test_B5_non_callable_callback_fails_explicitly(self) -> None:
        harness = Harness()
        with pytest.raises(RouterWrapperError):
            RouterWrapper(composition_root=harness.root, on_execution=42)  # type: ignore[arg-type]

    def test_B6_two_calls_hit_callback_twice_and_leave_no_state(self) -> None:
        harness = Harness()
        harness.wrapper("t1", object())
        harness.wrapper("t2", object())
        assert len(harness.seen) == 2
        assert harness.inner.calls == 2
        assert set(vars(harness.wrapper)) == {"_composition_root", "_on_execution"}

    def test_B7_structurally_satisfies_the_router_core_shape(self) -> None:
        harness = Harness()
        core = harness.wrapper
        assert callable(core)
        import inspect

        signature = inspect.signature(core.__call__)
        assert list(signature.parameters) == ["task", "context"]

    def test_B8_root_guard_is_not_swallowed(self) -> None:
        harness = Harness()
        selection = ModelSelection(tier=ModelTier.BALANCED, effort=EffortLevel.MEDIUM, reason="t")
        foreign = RoutingPlanAssembler(run_id_factory=lambda: "run_foreign").assemble(
            _plan(),
            main_thread_model=selection,
            branch_models=tuple((s.branch_id, selection) for s in _plan().branches),
        )
        harness.outcome = RouterOutcome(
            plan=foreign.plan, routing=foreign, costs=(), approval=harness.outcome.approval
        )
        harness.inner = _InnerRouterDouble(harness.outcome)
        object.__setattr__(harness.root, "_router", harness.inner)

        def collector(root: CompositionRoot, routing: RoutingPlan) -> None:
            harness.seen.append((root, routing))

        harness.wrapper = RouterWrapper(composition_root=harness.root, on_execution=collector)
        with pytest.raises(CompositionRootError):
            harness.wrapper("task", object())
        assert harness.seen == []


class TestBehaviorMaterializeSemantic:
    def test_materialize_produces_a_graph_internally_without_exposing_it(self) -> None:
        """`materialize` = **装配证明 + 顺序转交**（**≠ 执行 pass**）。"""
        harness = Harness()
        calls: list[ExecutionGraph] = []
        original = harness.root.materialize

        def spy(routing: RoutingPlan) -> ExecutionGraph:
            graph = original(routing)
            calls.append(graph)
            return graph

        object.__setattr__(harness.root, "materialize", spy)
        harness.wrapper("task", object())
        assert len(calls) == 1
        graph = calls[0]
        assert graph.routing == harness.routing
        assert graph.agent_manager.routing_run.branches == ()
        assert graph.agent_manager.routing_run.terminal is None

    def test_wrapper_does_not_advance_any_runtime_state(self) -> None:
        harness = Harness()
        harness.wrapper("task", object())
        run = harness.root.materialize(harness.routing).agent_manager.routing_run
        assert run.attempt == 1
        assert run.terminal is None
        assert run.control_flow_converged is False
        assert run.branches == ()


class TestProbeHygiene:
    def test_every_declared_anchor_hits(self) -> None:
        anchors = [
            "class RouterWrapperError(RuntimeError):",
            "class RouterWrapper:",
            "        outcome = self._composition_root.decision_router(task, context)",
            "        self._composition_root.materialize(outcome.routing)",
            "        if self._on_execution is not None:",
            "        return outcome",
            "        on_execution: Callable[[CompositionRoot, RoutingPlan], None] | None = None,",
        ]
        for anchor in anchors:
            assert anchor in SOURCE, f"锚点未命中：{anchor!r}"

    def test_source_is_parseable_without_bom(self) -> None:
        assert SOURCE_PATH.read_bytes()[:3] != b"\xef\xbb\xbf"
        ast.parse(SOURCE)
