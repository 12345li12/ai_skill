"""``C2d-2a 执行编排组合根`` —— 结构锁 **LEC-1 – LEC-12** + 永久 probe + 行为 **B1 – B9**。

架构依据：`docs/architecture.md` **1.4 / 6.4 / L47**（`[10]` 运行态唯一写者）·
**A13.2**（两阶段物化：dispatch licence → lifecycle commit）· **INV-03**（`RoutingPlan` 不可变）·
**A18.2**（终态唯一写口）· **A11.3c**（首轮 = `Plan.branches` 全部）。
范围、冻结与授权见 `tests/behavior/README_step5b.md` **§170** / **§171**。

**本模块要守住的六条主张：**

  1. **组合根只装配**：不跑 pass · 不写运行态 · 不算终态 · 不决定跑几轮（`LEC-5`）；
  2. **`AgentManager` 生产构造点唯一**（`LEC-2`，与 `LTW-1` 同型的不变量）；
  3. **不制造任何事实 seam**（`LEC-6`）：五项 seam 的生产者全在外部，本模块只**转交**；
  4. **缺失即显式失败**（`LEC-7`）：不为任何注入面编造默认值；
  5. **`run_id` 只生成一次**（`LEC-8`）并被 `routing` 与 `AgentManager` 共用；
  6. **零时钟 / 零 `try` / 不 import `adapters`**（`LEC-9` / `LEC-10` / `LEC-11`）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `run_pass` / `close_run` / `converge_run` /
`recovery` / `resume` 等词 ⇒ 所有锁一律 **AST 级 / 绑定集级**。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib
from typing import Mapping

import pytest

from adaptive_router.admission_facts import AdmissionFacts, RelationFact
from adaptive_router.capabilities import CapabilityRegistry
from adaptive_router.composition_root import (
    CompositionRoot,
    CompositionRootError,
    ExecutionGraph,
)
from adaptive_router.context_builder import ContextBuilder
from adaptive_router.contracts import ContextBundle, RoutingPlan
from adaptive_router.contracts.common import new_branch_id, new_run_id
from adaptive_router.contracts.decision import (
    BranchSpec,
    EffortLevel,
    ExecutionMode,
    ModelSelection,
    ModelTier,
    Plan,
)
from adaptive_router.execution_context_facts import ExecutionContextFacts
from adaptive_router.routing_plan import RoutingPlanAssembler
from adaptive_router.runtime_control_facts import RuntimeControlFacts

from _composition_root_locks import (  # noqa: F401  (复用既有检测器，不抄第二套)
    calls_to,
    clock_call_hits,
    imported_module_tokens,
    module_level_function_names,
    referenced_names,
)

SOURCE_PATH = pathlib.Path("src/adaptive_router/composition_root.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")

#: `§171` 冻结的模块级对象（**恰好 3 个**）。
EXPECTED_MODULE_OBJECTS = ["CompositionRoot", "CompositionRootError", "ExecutionGraph"]

#: `LEC-2` —— 唯一生产构造点（全仓不变量）。
UNIQUE_CONSTRUCTION_SITES = {
    "AgentManager": 1,
    "ExecutionCompositor": 1,
    "RoutingPlanAssembler": 1,
    "ContextBuilder": 1,
    "Scheduler": 1,
    "AdaptiveRouter": 1,
}

#: `LEC-6` —— 本模块**不得**构造任何事实 seam（生产者全在外部）。
FORBIDDEN_FACT_CONSTRUCTIONS = {
    "RuntimeControlFacts",
    "SchedulingRuntimeFacts",
    "AdmissionFacts",
    "ExecutionContextFacts",
    "ReusableResultRef",
}

#: `LEC-5` —— 本模块**不得**调用的东西（编排/写态/终态/观测）。
FORBIDDEN_CALLS = {
    "run_pass",
    "close_run",
    "converge_run",
    "record_outcome",
    "commit_started",
    "materialize",
    "apply_runtime_control",
    "settle_denied_run",
    "apply_resume_intent",
    "build_resume_intent",
    "route_failed_branch",
    "validate_branch",
    "record_observation",
    "build_run_summary",
}

#: `LEC-4` —— 零出边（运动/调度语义）。
FORBIDDEN_MOTION_NAMES = {
    "route_to",
    "next_node",
    "next_step",
    "dispatch",
    "dispatcher",
    "plan",
    "steps",
    "remaining",
}

#: `LEC-11` —— import 白名单（**精确相等**；**不得** import `adapters`）。
EXPECTED_IMPORT_TOKENS = {
    "__future__",
    "admission_facts",
    "agent_manager",
    "annotations",
    "capabilities",
    "common",
    "context_builder",
    "contracts",
    "contracts.common",
    "dataclass",
    "dataclasses",
    "execution",
    "execution_context_facts",
    "field",
    "reusable_results",
    "router",
    "routing_plan",
    "runtime_control_facts",
    "scheduler",
    "typing",
    "Callable",
    "Mapping",
}


# ----------------------------------------------------------------------
# 测试侧假件（**注入面**的替身；不制造生产事实）
# ----------------------------------------------------------------------


def blank_admission_facts() -> AdmissionFacts:
    """七槽全 `None`（显式 UNKNOWN）—— 不构成 veto（A11.3）。"""
    return AdmissionFacts(
        duplicate=None,
        context_duplicate=None,
        evidence_sufficient=None,
        insufficient_independent_value=None,
        remaining_small=None,
        dependency_too_strong=None,
        cost_exceeds_benefit=None,
    )


def _registry() -> CapabilityRegistry:
    """**不经探测**构造一个 registry（本测试不触发 snapshot）。"""
    return CapabilityRegistry.__new__(CapabilityRegistry)


def _plan() -> Plan:
    selection_note = "test"
    return Plan(
        mode=ExecutionMode.MULTI_AGENT,
        main_thread_duty="duty",
        reason=selection_note,
        branches=(
            BranchSpec(branch_id="br_lec_a", goal="ga", read_only=False, completion_criteria="ca"),
            BranchSpec(branch_id="br_lec_b", goal="gb", read_only=False, completion_criteria="cb"),
        ),
    )


class RunIdFactory:
    """记录调用次数的 `run_id_factory`（证明"只生成一次"）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"run_lec_{self.calls}"


class ControlRecorder:
    """控制 seam 的替身：返回 `(facts, ended_at)` 二元组。"""

    def __init__(self, *, timestamp: str = "2026-09-22T00:00:00Z") -> None:
        self.timestamp = timestamp
        self.calls = 0

    def __call__(self) -> tuple[RuntimeControlFacts, str]:
        self.calls += 1
        return RuntimeControlFacts(stop_branches_requested=False), self.timestamp


class FakeAdapter:
    """duck-typed adapter 替身（生产实现属 `C2d-2d`）。"""

    def __init__(self) -> None:
        self.started: list[object] = []

    def start(self, request: object) -> object:
        self.started.append(request)
        raise AssertionError("本测试不应启动任何分支（组合根不跑 pass）")

    def append(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("本测试不应追加输入")

    def stop(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("本测试不应停止分支")


def _facts_for(branch_id: str) -> ExecutionContextFacts:
    """`ExecutionContextFacts` 是**每 child** 的四项上下文事实（A10.3；外部 seam）。"""
    return ExecutionContextFacts(
        background=(f"bg-{branch_id}",),
        constraints=("c",),
        allowed_operations=("read",),
        forbidden_operations=("write",),
    )


def build_root(**overrides: object) -> CompositionRoot:
    """按 `§171` 冻结的注入面构造一个组合根（默认件全部是**替身**）。

    ⚠️ `execution_context_facts_by_branch` 必须**精确覆盖** `Plan.branches`（A10.2b 不多不少）
    —— 所以默认件按 :func:`_plan` 的两个 branch_id 造事实（**替身**，不是生产事实）。
    """
    branch_ids = [spec.branch_id for spec in _plan().branches]
    kwargs: dict[str, object] = {
        "facts_source": lambda task, context: None,
        "capabilities": _registry(),
        "adapter": FakeAdapter(),
        "execution_context_facts_by_branch": {b: _facts_for(b) for b in branch_ids},
        "confirmed_conclusions_by_branch": {b: () for b in branch_ids},
        "control_facts_source": ControlRecorder(),
        "admission_facts_source": lambda _branch_id: blank_admission_facts(),
        "reusable_result_source": lambda _branch_id: None,
        "continuation_source": lambda: None,
        "run_id_factory": RunIdFactory(),
    }
    kwargs.update(overrides)
    return CompositionRoot(**kwargs)  # type: ignore[arg-type]


def build_routing(root: CompositionRoot, plan: Plan | None = None) -> RoutingPlan:
    """用**同一个** `run_id` 造一个 `RoutingPlan`（模拟决策链产出）。

    ⚠️ `MULTI_AGENT` **必须**给每个 child 显式档位映射 —— 装配器**不**替调用方假设
    "全部同模型"（`routing_plan.py` 的 `_require_complete_coverage`）。
    """
    selection = ModelSelection(tier=ModelTier.BALANCED, effort=EffortLevel.MEDIUM, reason="test")
    resolved = plan if plan is not None else _plan()
    return RoutingPlanAssembler(run_id_factory=lambda: root.run_id).assemble(
        resolved,
        main_thread_model=selection,
        branch_models=tuple((spec.branch_id, selection) for spec in resolved.branches),
    )


def _construction_counts(source: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            counts[ast.unparse(node.func)] = counts.get(ast.unparse(node.func), 0) + 1
    return counts


def _try_count(source: str) -> int:
    return sum(1 for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Try))


# ======================================================================
# LEC-1 … LEC-12 —— 结构锁
# ======================================================================


class TestLEC1ModuleShape:
    def test_module_level_objects_are_exactly_three(self) -> None:
        import adaptive_router.composition_root as module

        declared = sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        )
        assert declared == EXPECTED_MODULE_OBJECTS

    def test_no_module_level_functions(self) -> None:
        assert module_level_function_names(SOURCE) == ()

    def test_public_surface(self) -> None:
        import adaptive_router.composition_root as module

        assert module.__all__ == EXPECTED_MODULE_OBJECTS

    def test_probe_fourth_module_object_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "class CompositionRootError(RuntimeError):",
            "def _extra_helper() -> None:\n    return None\n\n\n"
            "class CompositionRootError(RuntimeError):",
            1,
        )
        assert mutated != SOURCE
        assert module_level_function_names(mutated) == ("_extra_helper",)


class TestLEC2UniqueConstructionSites:
    def test_every_object_graph_node_is_constructed_exactly_once(self) -> None:
        counts = _construction_counts(SOURCE)
        for name, expected in UNIQUE_CONSTRUCTION_SITES.items():
            assert counts.get(name, 0) == expected, f"{name} 构造点 = {counts.get(name, 0)}"

    def test_the_class_constructs_no_second_agent_manager(self) -> None:
        # ⚠️ 必须用 **AST 计数**（`SOURCE.count` 会把 docstring 里的提及也数进去 —— 假读数）。
        assert _construction_counts(SOURCE).get("AgentManager", 0) == 1

    def test_the_two_textual_mentions_are_docstring_only(self) -> None:
        """文本出现 2 次、AST 调用 1 次 ⇒ 另一次必须是 docstring 提及（可复核）。"""
        assert SOURCE.count("AgentManager(") == 2
        assert "**唯一** `AgentManager(` 生产构造点" in SOURCE

    def test_probe_second_agent_manager_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        compositor = ExecutionCompositor(",
            "        _second = AgentManager(\n"
            "            run_id=self._run_id, branch_order=(), total_attempt_limit=2\n"
            "        )\n"
            "        compositor = ExecutionCompositor(",
            1,
        )
        assert mutated != SOURCE
        assert _construction_counts(mutated).get("AgentManager", 0) == 2


class TestLEC3InjectionSurfaceIsKeywordOnly:
    def test_constructor_is_keyword_only(self) -> None:
        fields = [f.name for f in dataclasses.fields(CompositionRoot)]
        for required in (
            "facts_source",
            "capabilities",
            "adapter",
            "execution_context_facts_by_branch",
            "confirmed_conclusions_by_branch",
            "control_facts_source",
            "admission_facts_source",
            "reusable_result_source",
            "continuation_source",
            "run_id_factory",
            "total_attempt_limit",
        ):
            assert required in fields, f"缺注入面 {required}"

    def test_no_config_object_container(self) -> None:
        """`§171` 待裁 2 = 甲：**不得**把注入面打包成配置对象。"""
        import adaptive_router.composition_root as module

        declared = {name for name in vars(module) if not name.startswith("__")}
        assert "CompositionConfig" not in declared
        assert "Config" not in declared
        assert not any("Config" in name for name in EXPECTED_MODULE_OBJECTS)


class TestLEC4ZeroOutboundEdges:
    def test_no_motion_identifier_is_bound(self) -> None:
        bound = referenced_names(SOURCE)
        assert bound & FORBIDDEN_MOTION_NAMES == frozenset() or bound & FORBIDDEN_MOTION_NAMES <= {"plan"}

    def test_only_the_router_owns_the_decision_chain(self) -> None:
        """组合根**不**自己跑决策链节点 —— 只装配 `AdaptiveRouter` 并把节点交给它。"""
        counts = _construction_counts(SOURCE)
        for node in ("Policy", "Evaluator", "Planner", "ModelRouter", "ApprovalJudge", "RiskEvaluator"):
            assert counts.get(node, 0) == 0, f"组合根不得自行构造 {node}"


class TestLEC5NoOrchestration:
    def test_no_orchestration_or_write_entry_is_called(self) -> None:
        assert calls_to(SOURCE, FORBIDDEN_CALLS) == []

    def test_run_pass_is_never_called(self) -> None:
        assert calls_to(SOURCE, {"run_pass"}) == []
        assert "run_pass" not in SOURCE.split('"""')[-1]

    def test_probe_run_pass_call_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        return ExecutionGraph(",
            "        self.compositor.run_pass(\n"
            "            routing=routing, bundles=bundles, capability=None,\n"
            "            runtime_facts=None, control_facts_source=self.control_facts_source,\n"
            "            first_pass=True,\n"
            "        )\n"
            "        return ExecutionGraph(",
            1,
        )
        assert mutated != SOURCE
        assert calls_to(mutated, {"run_pass"}) != []


class TestLEC6NoFactManufacturing:
    def test_no_fact_seam_is_constructed(self) -> None:
        assert calls_to(SOURCE, FORBIDDEN_FACT_CONSTRUCTIONS) == []

    def test_fact_seams_only_appear_as_parameters_or_annotations(self) -> None:
        tree = ast.parse(SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                assert ast.unparse(node.func) not in FORBIDDEN_FACT_CONSTRUCTIONS
        assert "RuntimeControlFacts" in SOURCE
        assert "AdmissionFacts" in SOURCE

    def test_probe_fabricated_fact_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        self._scheduler = Scheduler()",
            "        _fake = RuntimeControlFacts(stop_branches_requested=False)\n"
            "        self._scheduler = Scheduler()",
            1,
        )
        assert mutated != SOURCE
        assert calls_to(mutated, {"RuntimeControlFacts"}) != []


class TestLEC7ExplicitFailureOverDefaults:
    def test_guard_set_is_present(self) -> None:
        guards = sum(
            1
            for node in ast.walk(ast.parse(SOURCE))
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and ast.unparse(node.exc.func) == "CompositionRootError"
        )
        assert guards >= 9, f"守卫数 = {guards}（缺省即真不可接受）"

    def test_probe_removed_guard_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        if self.adapter is None:",
            "        if False:",
            1,
        )
        assert mutated != SOURCE
        assert "if self.adapter is None:" not in mutated


class TestLEC8SingleRunIdGeneration:
    def test_run_id_factory_is_called_exactly_once(self) -> None:
        factory = RunIdFactory()
        root = build_root(run_id_factory=factory)
        assert factory.calls == 1
        assert root.run_id == "run_lec_1"

    def test_assembler_shares_the_same_run_id_source(self) -> None:
        """`LEC-8`：决策半边用的 assembler 与 `AgentManager` **共用同一个** `run_id`。

        ⚠️ 该共享是"**记忆化一次**"实现的：`assemble` 不传 `run_id`，而注入的
        `run_id_factory` 是一个**只返回已生成值**的闭包 ⇒ 全链只有**一个** `run_id`。
        """
        factory = RunIdFactory()
        root = build_root(run_id_factory=factory)
        routing = build_routing(root)
        assert routing.run_id == root.run_id
        assert factory.calls == 1

    def test_probe_second_generation_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        self._run_id = self.run_id_factory()",
            "        self._run_id = self.run_id_factory()\n        self._run_id = self.run_id_factory()",
            1,
        )
        assert mutated != SOURCE
        assert mutated.count("self._run_id = self.run_id_factory()") == 2


class TestLEC9NoClock:
    def test_zero_clock_calls(self) -> None:
        assert clock_call_hits(SOURCE) == []

    def test_no_clock_module_imported(self) -> None:
        assert imported_module_tokens(SOURCE) & {"datetime", "time", "zoneinfo"} == set()


class TestLEC10NoSwallow:
    def test_zero_try_nodes(self) -> None:
        assert _try_count(SOURCE) == 0

    def test_error_type_is_not_contract_error(self) -> None:
        from adaptive_router.contracts import ContractError

        assert issubclass(CompositionRootError, RuntimeError)
        assert not issubclass(CompositionRootError, ContractError)

    def test_probe_try_except_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "        self._run_id = self.run_id_factory()",
            "        try:\n            pass\n        except Exception:\n            pass\n"
            "        self._run_id = self.run_id_factory()",
            1,
        )
        assert mutated != SOURCE
        assert _try_count(mutated) == 1


class TestLEC11ImportBoundary:
    def test_import_tokens_are_exactly_the_whitelist(self) -> None:
        assert imported_module_tokens(SOURCE) == EXPECTED_IMPORT_TOKENS

    def test_adapters_is_never_imported(self) -> None:
        assert "adapters" not in imported_module_tokens(SOURCE)
        assert "from .adapters" not in SOURCE

    def test_no_boundary_module_is_imported(self) -> None:
        assert imported_module_tokens(SOURCE) & {
            "entrypoint",
            "session",
            "terminal_wiring",
            "deny_wiring",
            "resume_wiring",
            "recovery_wiring",
            "validation_wiring",
            "cost",
            "ledger_wiring",
        } == set()


class TestLEC12GraphShape:
    def test_execution_graph_fields(self) -> None:
        """`C2d-2a Graph Carrier Extension`：**4 → 5 字段**。

        ⚠️ 第 5 字段 `bundles` **追加在 `routing` 之后**，既有 4 字段的**名字与顺序逐字不变**
           ⇒ 本断言是本次 extension 的**唯一**签名级改动（其余 11 个 `LEC` 锁全部保持原样）。
        """
        assert [f.name for f in dataclasses.fields(ExecutionGraph)] == [
            "run_id",
            "agent_manager",
            "compositor",
            "routing",
            "bundles",
        ]

    def test_graph_is_frozen(self) -> None:
        assert ExecutionGraph.__dataclass_params__.frozen is True

    def test_graph_carries_no_motion_field(self) -> None:
        assert {f.name for f in dataclasses.fields(ExecutionGraph)} & FORBIDDEN_MOTION_NAMES == set()


# ======================================================================
# 行为 B1 – B9
# ======================================================================


class TestBehaviorCompositionRoot:
    def test_B1_root_is_constructible_and_holds_the_decision_router(self) -> None:
        from adaptive_router.router import AdaptiveRouter

        root = build_root()
        assert isinstance(root.decision_router, AdaptiveRouter)

    def test_B2_materialize_returns_the_complete_graph(self) -> None:
        root = build_root()
        graph = root.materialize(build_routing(root))
        assert isinstance(graph, ExecutionGraph)
        from adaptive_router.agent_manager import AgentManager
        from adaptive_router.execution import ExecutionCompositor

        assert isinstance(graph.agent_manager, AgentManager)
        assert isinstance(graph.compositor, ExecutionCompositor)

    def test_B3_branch_order_comes_from_plan_branches(self) -> None:
        root = build_root()
        routing = build_routing(root)
        graph = root.materialize(routing)
        assert graph.agent_manager.pending_admission_branch_ids == ()  # 未物化任何 WAITING
        assert graph.agent_manager.active_subagent_count == 0

    # ------------------------------------------------------------------
    # `C2d-2a Graph Carrier Extension`（`5B.6.35.1b`）—— 第 5 字段 `bundles`
    # ------------------------------------------------------------------

    def test_B10_graph_carries_bundles_covering_every_branch(self) -> None:
        """extension 的**核心行为**（A10.4 的第一个基数情形）。

        `bundles` 必须**精确覆盖** `routing.plan.branches` 全集（**不是**"只给被派发的分支"）
        —— 这正是 `run_pass` 的 `bundles` 入参所要求的（`Scheduler.schedule` 对它做覆盖校验）。
        """
        root = build_root()
        routing = build_routing(root)
        graph = root.materialize(routing)

        expected = tuple(spec.branch_id for spec in routing.plan.branches)
        assert len(expected) == 2  # `_plan()` 的两个 child
        assert set(graph.bundles) == set(expected)
        assert all(isinstance(bundle, ContextBundle) for bundle in graph.bundles.values())

    def test_B10b_zero_child_plan_yields_empty_bundles(self) -> None:
        """A10.4 的**第二个基数情形**：0 child ⇒ `{}`（空映射，**不是** `None`）。

        ⚠️ 声明 0 child 就必须同时给**空**的两项事实映射 —— `ContextBuilder` 按 A10.2b
           做**精确覆盖**（不多不少），这是既有纪律，不是本次 extension 的放宽。
        """
        root = build_root(
            execution_context_facts_by_branch={},
            confirmed_conclusions_by_branch={},
        )
        zero_plan = Plan(
            mode=ExecutionMode.DIRECT,
            main_thread_duty="duty",
            reason="零 child",
            branches=(),
        )
        routing = build_routing(root, zero_plan)
        graph = root.materialize(routing)
        assert graph.bundles == {}
        assert isinstance(graph.bundles, Mapping)

    def test_B11_bundles_are_the_very_object_context_builder_produced(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ **本 extension 的核心正确性**：装入的是**同一个对象**，不是"再算一遍"。

        判据 = **同一性**（`is`）+ **调用次数** + **参数逐字相等**。
        若实现改成"在 `return` 前再调一次 `ContextBuilder.build(...)`"，`build` 会被调用
        **2** 次 ⇒ 本测试**语义变红**（且**不是**集合期错误造成的假红 —— `MW2-1` 教训的落地）。
        """
        seen: list[tuple[object, object, object]] = []
        produced: list[object] = []
        original = ContextBuilder.build

        def spy(self: object, **kwargs: object) -> object:
            seen.append(
                (
                    kwargs["plan"],
                    kwargs["facts_by_branch"],
                    kwargs["confirmed_conclusions_by_branch"],
                )
            )
            result = original(self, **kwargs)  # type: ignore[arg-type]
            produced.append(result)
            return result

        monkeypatch.setattr(ContextBuilder, "build", spy)

        root = build_root()
        routing = build_routing(root)
        graph = root.materialize(routing)

        # ① 一次 materialize 生命周期内 `build` **恰好一次**
        assert len(seen) == 1, f"build 被调用了 {len(seen)} 次（应为 1）"
        assert len(produced) == 1
        # ② 装入的**就是**那一次产出的对象（同一性，不是"等价的另一份"）
        assert graph.bundles is produced[0]
        # ③ 传入参数逐字等于组合根持有的两项事实映射（**不重算、不换源**）
        assert seen[0] == (
            routing.plan,
            root.execution_context_facts_by_branch,
            root.confirmed_conclusions_by_branch,
        )

    def test_B4_run_id_is_shared_by_routing_and_manager(self) -> None:
        root = build_root()
        routing = build_routing(root)
        graph = root.materialize(routing)
        assert routing.run_id == root.run_id == graph.run_id
        assert graph.agent_manager.routing_run.run_id == root.run_id

    def test_B5_run_id_mismatch_fails_explicitly(self) -> None:
        root = build_root()
        selection = ModelSelection(
            tier=ModelTier.BALANCED, effort=EffortLevel.MEDIUM, reason="test"
        )
        plan = _plan()
        foreign = RoutingPlanAssembler(run_id_factory=lambda: "run_foreign").assemble(
            plan,
            main_thread_model=selection,
            branch_models=tuple((spec.branch_id, selection) for spec in plan.branches),
        )
        assert foreign.run_id != root.run_id
        with pytest.raises(CompositionRootError):
            root.materialize(foreign)

    def test_B6_non_routing_plan_fails_explicitly(self) -> None:
        root = build_root()
        with pytest.raises(CompositionRootError):
            root.materialize(object())

    def test_B7_missing_injection_surface_fails_explicitly(self) -> None:
        with pytest.raises(CompositionRootError):
            build_root(adapter=None)
        with pytest.raises(CompositionRootError):
            build_root(capabilities=None)
        with pytest.raises(CompositionRootError):
            build_root(facts_source=None)
        with pytest.raises(CompositionRootError):
            build_root(control_facts_source=None)
        with pytest.raises(CompositionRootError):
            build_root(admission_facts_source=None)
        with pytest.raises(CompositionRootError):
            build_root(reusable_result_source=None)
        with pytest.raises(CompositionRootError):
            build_root(total_attempt_limit=0)
        with pytest.raises(CompositionRootError):
            build_root(total_attempt_limit=True)
        with pytest.raises(CompositionRootError):
            build_root(execution_context_facts_by_branch=())

    def test_B8_materialize_does_not_run_a_pass(self) -> None:
        root = build_root()
        graph = root.materialize(build_routing(root))
        run = graph.agent_manager.routing_run
        assert run.branches == ()
        assert run.terminal is None
        assert run.control_flow_converged is False
        assert run.attempt == 1

    def test_B9_no_default_facts_are_generated_anywhere(self) -> None:
        """`§171` 原则：缺事实即显式失败 —— 组合根**不**补默认值。"""
        root = build_root()
        routing = build_routing(root)
        graph = root.materialize(routing)
        assert graph.routing is routing
        assert graph.agent_manager.routing_run.branches == ()


class TestProbeHygiene:
    def test_every_declared_anchor_hits(self) -> None:
        anchors = [
            "class CompositionRootError(RuntimeError):",
            "        self._run_id = self.run_id_factory()",
            "        self._scheduler = Scheduler()",
            "        if self.adapter is None:",
            "        compositor = ExecutionCompositor(",
            "        return ExecutionGraph(",
            "class CompositionRoot:",
        ]
        for anchor in anchors:
            assert anchor in SOURCE, f"锚点未命中：{anchor!r}"

    def test_source_is_parseable_without_bom(self) -> None:
        assert SOURCE_PATH.read_bytes()[:3] != b"\xef\xbb\xbf"
        ast.parse(SOURCE)
