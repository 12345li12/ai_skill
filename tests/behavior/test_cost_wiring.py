"""``[15a]`` 成本接线行为测试（Step 5B.3）。

架构依据：
  `docs/architecture.md` 2 节 ``[15a] Cost Estimator``
  —— 输入 = ``ModelSelection``（已定档位）、分支规模、并发放大系数、恢复重试余量；
  输出 = ``CostEstimate``；基数 = **每分支的每个执行 attempt 最多一次**。
  **INV-05**：``[15a]`` 只**读** ``[06]`` 的档位，不得反向改档位。

⚠️ **本文件不引入新的 Test ID** —— 冻结矩阵（架构第 8 节）里没有专门的
   "成本接线" Test ID，因此这里只断言接线不变式，不假装承载某个 Test ID。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.contracts import (
    BranchSpec,
    EffortLevel,
    ExecutionMode,
    ModelSelection,
    ModelTier,
    Plan,
)
from adaptive_router.cost import (
    CostEstimatorWiring,
    CostWiringError,
    PlannedScope,
    UnitCostEstimate,
)
from adaptive_router.cost.wiring import SCOPE_BY_MODE
from adaptive_router.routing_plan import RoutingPlanAssembler

from _import_detectors import imported_module_tokens, imported_names

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "src/adaptive_router/cost/wiring.py"
)
MODULE_SOURCE = MODULE_PATH.read_text(encoding="utf-8")
_MODULE_TREE = ast.parse(MODULE_SOURCE)


def _imported_modules() -> set[str]:
    """被测模块的 import token（保守检测器，见 `_import_detectors`）。"""
    return imported_module_tokens(MODULE_SOURCE)


def _imported_names() -> set[str]:
    return imported_names(MODULE_SOURCE)


def _identifier_names() -> set[str]:
    return {n.id for n in ast.walk(_MODULE_TREE) if isinstance(n, ast.Name)}


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def selection(tier: ModelTier = ModelTier.STRONG) -> ModelSelection:
    return ModelSelection(tier=tier, effort=EffortLevel.HIGH, reason="task-level")


def branch(branch_id: str) -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal=f"goal {branch_id}",
        read_only=True,
        completion_criteria=f"done {branch_id}",
    )


def plan_with(mode: ExecutionMode, *branches: BranchSpec) -> Plan:
    duty = "execute on the main thread"
    return Plan(mode=mode, main_thread_duty=duty, reason="test", branches=branches)


def routing_for(plan: Plan, *, model: ModelSelection | None = None):
    """用真实装配器产出真实 ``RoutingPlan``（不手搓冻结契约）。"""
    chosen = model if model is not None else selection()
    return RoutingPlanAssembler(
        run_id_factory=lambda: "run_fixed",
        main_thread_branch_id_factory=lambda: "br_main",
    ).assemble(
        plan,
        main_thread_model=chosen,
        branch_models=tuple((spec.branch_id, chosen) for spec in plan.branches),
    )


def single_unit_routing():
    return routing_for(plan_with(ExecutionMode.DIRECT))


def multi_unit_routing():
    return routing_for(
        plan_with(ExecutionMode.MULTI_AGENT, branch("br_a"), branch("br_b"))
    )


def wiring() -> CostEstimatorWiring:
    return CostEstimatorWiring()


# ==========================================================================
# 覆盖：每个执行单位都要有估算
# ==========================================================================


class TestUnitCoverage:
    @pytest.mark.parametrize(
        "plan_factory",
        [
            lambda: plan_with(ExecutionMode.DIRECT),
            lambda: plan_with(ExecutionMode.STAGED_SINGLE_AGENT),
        ],
    )
    def test_zero_branch_plan_has_exactly_the_main_thread(self, plan_factory) -> None:
        routing = routing_for(plan_factory())
        units = wiring().estimate_all_units(
            routing, max_concurrency=2, attempt=1, retry_allowance=0
        )
        assert [u.branch_id for u in units] == ["br_main"]
        assert units[0].is_main_thread is True

    def test_multi_agent_covers_main_thread_and_every_child(self) -> None:
        routing = multi_unit_routing()
        units = wiring().estimate_all_units(
            routing, max_concurrency=4, attempt=1, retry_allowance=0
        )
        assert [u.branch_id for u in units] == list(routing.execution_unit_ids)
        assert [u.is_main_thread for u in units] == [True, False, False]

    def test_order_follows_execution_unit_ids(self) -> None:
        routing = multi_unit_routing()
        units = wiring().estimate_all_units(
            routing, max_concurrency=4, attempt=1, retry_allowance=0
        )
        assert [u.branch_id for u in units] == ["br_main", "br_a", "br_b"]

    def test_every_unit_gets_a_frozen_estimate(self) -> None:
        from adaptive_router.contracts import CostEstimate

        for unit in wiring().estimate_all_units(
            multi_unit_routing(), max_concurrency=4, attempt=1, retry_allowance=0
        ):
            assert isinstance(unit, UnitCostEstimate)
            assert isinstance(unit.estimate, CostEstimate)

    def test_unknown_unit_is_refused(self) -> None:
        with pytest.raises(CostWiringError):
            wiring().estimate_for_unit(
                multi_unit_routing(),
                "br_nope",
                max_concurrency=4,
                attempt=1,
                retry_allowance=0,
            )


# ==========================================================================
# 计划形态 → PlannedScope 的显式映射
# ==========================================================================


class TestScopeMapping:
    def test_mapping_is_total_over_execution_mode(self) -> None:
        """新增执行方式时必须显式扩展映射表，不得静默 KeyError。"""
        assert set(SCOPE_BY_MODE) == set(ExecutionMode)

    def test_direct_maps_to_main_thread_not_direct(self) -> None:
        """⚠️ 两者**不同名** —— 这正是 5B.3 订正的那处假设。"""
        assert SCOPE_BY_MODE[ExecutionMode.DIRECT] is PlannedScope.MAIN_THREAD
        assert not hasattr(PlannedScope, "DIRECT")

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (ExecutionMode.DIRECT, PlannedScope.MAIN_THREAD),
            (ExecutionMode.STAGED_SINGLE_AGENT, PlannedScope.STAGED_SINGLE_AGENT),
            (ExecutionMode.MULTI_AGENT, PlannedScope.MULTI_AGENT),
        ],
    )
    def test_scope_reaches_the_drivers(self, mode, expected) -> None:
        branches = (
            (branch("br_a"), branch("br_b"))
            if mode is ExecutionMode.MULTI_AGENT
            else ()
        )
        routing = routing_for(plan_with(mode, *branches))
        units = wiring().estimate_all_units(
            routing, max_concurrency=4, attempt=1, retry_allowance=0
        )
        assert all(u.drivers.scope is expected for u in units)

    def test_branch_count_counts_children_not_the_main_thread(self) -> None:
        multi = wiring().estimate_all_units(
            multi_unit_routing(), max_concurrency=4, attempt=1, retry_allowance=0
        )
        assert all(u.drivers.planned_branch_count == 2 for u in multi)

        single = wiring().estimate_all_units(
            single_unit_routing(), max_concurrency=4, attempt=1, retry_allowance=0
        )
        assert single[0].drivers.planned_branch_count == 0


# ==========================================================================
# INV-05：只读档位，绝不改档
# ==========================================================================


class TestInv05ReadsTierNeverChangesIt:
    def test_each_unit_reads_its_own_selection_from_the_routing_plan(self) -> None:
        routing = multi_unit_routing()
        units = wiring().estimate_all_units(
            routing, max_concurrency=4, attempt=1, retry_allowance=0
        )
        for unit in units:
            expected = routing.model_for_branch(unit.branch_id)
            assert unit.drivers.tier is expected.tier
            assert unit.drivers.effort is expected.effort

    def test_routing_plan_is_not_mutated(self) -> None:
        routing = multi_unit_routing()
        before = dataclasses.asdict(routing.plan)
        wiring().estimate_all_units(
            routing, max_concurrency=4, attempt=1, retry_allowance=0
        )
        assert dataclasses.asdict(routing.plan) == before
        assert routing.main_thread_model.tier is ModelTier.STRONG

    def test_does_not_construct_a_model_selection(self) -> None:
        """**结构锁**：接线层不得构造/改写 ``ModelSelection``（INV-05）。"""
        assert "ModelSelection" not in _imported_names()
        assert "ModelSelection" not in _identifier_names()

    def test_does_not_import_model_router(self) -> None:
        """档位从已装配的 ``RoutingPlan`` 取 —— 不回头问 [06]、更不改它。"""
        modules = _imported_modules()
        assert all("model_router" not in name for name in modules)


# ==========================================================================
# 运行期事实：显式必需，不许猜
# ==========================================================================


class TestRuntimeFactsAreExplicit:
    @pytest.mark.parametrize(
        "missing", ["max_concurrency", "attempt", "retry_allowance"]
    )
    def test_each_runtime_fact_is_required(self, missing) -> None:
        """三个字段都**没有默认值** —— 漏传即 ``TypeError``，不得替调用方猜。"""
        kwargs = {"max_concurrency": 4, "attempt": 1, "retry_allowance": 0}
        del kwargs[missing]
        with pytest.raises(TypeError):
            wiring().estimate_all_units(single_unit_routing(), **kwargs)

    def test_signature_has_no_defaults_for_them(self) -> None:
        import inspect

        parameters = inspect.signature(
            CostEstimatorWiring.estimate_all_units
        ).parameters
        for name in ("max_concurrency", "attempt", "retry_allowance"):
            assert parameters[name].default is inspect.Parameter.empty, name
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, name

    def test_invalid_values_fail_explicitly(self) -> None:
        with pytest.raises(ValueError):
            wiring().estimate_all_units(
                single_unit_routing(), max_concurrency=0, attempt=1, retry_allowance=0
            )
        with pytest.raises(ValueError):
            wiring().estimate_all_units(
                single_unit_routing(), max_concurrency=2, attempt=0, retry_allowance=0
            )
        with pytest.raises((TypeError, ValueError)):
            wiring().estimate_all_units(
                single_unit_routing(), max_concurrency=2, attempt=1, retry_allowance=-1
            )

    def test_does_not_read_capability_snapshot(self) -> None:
        """不从 ``CapabilitySnapshot.concurrency_limit`` 反推并发。"""
        assert "CapabilitySnapshot" not in _imported_names()
        assert "CapabilitySnapshot" not in _identifier_names()

    def test_the_runtime_facts_reach_the_drivers(self) -> None:
        unit = wiring().estimate_all_units(
            single_unit_routing(), max_concurrency=7, attempt=3, retry_allowance=2
        )[0]
        assert unit.drivers.max_concurrency == 7
        assert unit.drivers.attempt == 3
        assert unit.drivers.retry_allowance == 2


# ==========================================================================
# reuse：只透传 previous，由 estimator 判定
# ==========================================================================


class TestReuseIsDelegatedNotDecided:
    def test_without_previous_the_estimate_is_fresh(self) -> None:
        units = wiring().estimate_all_units(
            multi_unit_routing(), max_concurrency=4, attempt=1, retry_allowance=0
        )
        assert all(u.outcome.reuse.value == "fresh" for u in units)

    def test_unchanged_drivers_reuse_the_previous_estimate(self) -> None:
        routing = multi_unit_routing()
        first = wiring().estimate_all_units(
            routing, max_concurrency=4, attempt=1, retry_allowance=0
        )
        previous = {u.branch_id: u.estimate for u in first}
        second = wiring().estimate_all_units(
            routing,
            max_concurrency=4,
            attempt=2,
            retry_allowance=0,
            previous=previous,
        )
        assert all(u.outcome.reuse.value == "reused" for u in second)
        for unit in second:
            assert unit.estimate is previous[unit.branch_id]

    def test_changed_drivers_force_a_fresh_estimate(self) -> None:
        routing = multi_unit_routing()
        first = wiring().estimate_all_units(
            routing, max_concurrency=4, attempt=1, retry_allowance=0
        )
        previous = {u.branch_id: u.estimate for u in first}
        second = wiring().estimate_all_units(
            routing,
            max_concurrency=8,  # 驱动变化：并发
            attempt=2,
            retry_allowance=0,
            previous=previous,
        )
        assert all(u.outcome.reuse.value == "fresh" for u in second)

    def test_previous_is_per_unit(self) -> None:
        """只给一个单位 previous 时，其余单位仍应是 ``FRESH``。"""
        routing = multi_unit_routing()
        first = wiring().estimate_all_units(
            routing, max_concurrency=4, attempt=1, retry_allowance=0
        )
        only_main = {"br_main": first[0].estimate}
        second = wiring().estimate_all_units(
            routing,
            max_concurrency=4,
            attempt=2,
            retry_allowance=0,
            previous=only_main,
        )
        by_id = {u.branch_id: u.outcome.reuse.value for u in second}
        assert by_id == {"br_main": "reused", "br_a": "fresh", "br_b": "fresh"}

    def test_wiring_never_names_the_reuse_policy(self) -> None:
        """**结构锁**：接线层**不决定**复用 —— 连 ``ReusePolicy`` 都不出现。"""
        assert "ReusePolicy" not in _imported_names()
        assert "ReusePolicy" not in _identifier_names()

    def test_rejects_a_non_mapping_previous(self) -> None:
        with pytest.raises(CostWiringError):
            wiring().estimate_all_units(
                multi_unit_routing(),
                max_concurrency=4,
                attempt=1,
                retry_allowance=0,
                previous=[("br_a", None)],  # type: ignore[arg-type]
            )


# ==========================================================================
# per-unit / per-attempt
# ==========================================================================


class TestPerUnitAttempts:
    def test_units_can_be_on_different_attempts(self) -> None:
        """"每分支的每个 attempt 最多一次"的字面含义：可以只有某条分支在重试。"""
        routing = multi_unit_routing()
        main = wiring().estimate_for_unit(
            routing, "br_main", max_concurrency=4, attempt=1, retry_allowance=0
        )
        retrying = wiring().estimate_for_unit(
            routing, "br_a", max_concurrency=4, attempt=2, retry_allowance=1
        )
        assert main.drivers.attempt == 1
        assert retrying.drivers.attempt == 2
        assert retrying.drivers.retry_allowance == 1
        assert main.drivers.retry_allowance == 0

    def test_the_estimator_is_injectable(self) -> None:
        calls: list[int] = []

        class Spy:
            def estimate(self, drivers, *, previous=None):
                calls.append(drivers.attempt)
                return CostEstimatorWiring().estimator.estimate(
                    drivers, previous=previous
                )

        spy_wiring = CostEstimatorWiring(estimator=Spy())  # type: ignore[arg-type]
        spy_wiring.estimate_all_units(
            multi_unit_routing(), max_concurrency=4, attempt=5, retry_allowance=0
        )
        assert calls == [5, 5, 5]

    def test_input_validation(self) -> None:
        with pytest.raises(CostWiringError):
            wiring().estimate_all_units(
                "nope",  # type: ignore[arg-type]
                max_concurrency=4,
                attempt=1,
                retry_allowance=0,
            )


# ==========================================================================
# 边界：不接 Ledger / Summary / Router Core
# ==========================================================================


class TestWiringBoundaries:
    def test_does_not_import_ledger_or_summary(self) -> None:
        """5B.3 **不记账、不汇总** —— 落账与汇聚是后续步骤。"""
        modules = _imported_modules()
        for forbidden in ("ledger", "summary"):
            assert all(forbidden not in name for name in modules), forbidden

    def test_does_not_import_planner_policy_or_risk(self) -> None:
        modules = _imported_modules()
        for forbidden in ("planner", "policy", "risk", "session"):
            assert all(forbidden not in name for name in modules), forbidden

    def test_does_not_read_files_or_environment(self) -> None:
        modules = _imported_modules()
        for forbidden in ("pathlib", "os", "subprocess", "json", "socket"):
            assert all(forbidden not in name for name in modules), forbidden

    def test_router_core_still_absent(self) -> None:
        """Router Core 属 5B.5 —— 本模块不与它耦合。"""
        assert "entrypoint" not in _imported_modules()
        assert "router" not in _imported_modules()
