"""``[09] Scheduler`` —— 行为锁 B1–B20 + 结构锁 L1–L12（Step 5B.6.3 实现）。

架构依据：`docs/architecture.md` 2 节 ``[09] Scheduler``（**Amendment A11**，FROZEN）。
范围、锁清单与 probe 计划：`tests/behavior/README_step5b.md` §34–§37。

⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** ``src/`` 里的真实文件。
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib

import pytest

from adaptive_router.admission_facts import AdmissionFacts, RelationFact
from adaptive_router.contracts import (
    BranchReturn,
    BranchSpec,
    CapabilitySnapshot,
    Confidence,
    ConfirmationSource,
    ContextBundle,
    EffortLevel,
    ExecutionMode,
    ExecutionValue,
    ModelTier,
    Plan,
    UnavailableReason,
    ValidationRecord,
)
from adaptive_router.dispatch_decisions import DispatchAction, DispatchDecision
from adaptive_router.reusable_results import ReusableResultRef
from adaptive_router.scheduler import Scheduler, SchedulerError
from adaptive_router.scheduling_runtime_facts import SchedulingRuntimeFacts

from _context_locks import (
    calls_to,
    exact_keys_comparisons,
    forbidden_import_hits,
    helper_call_count,
    imported_symbol_bindings,
    mapping_get_calls,
    open_calls,
    resolved_symbols,
)
from _import_detectors import imported_names
from _scheduler_locks import (
    dataclass_field_names,
    enum_member_names,
    helper_call_operands,
    scheduler_data_attribute_reads,
    signature_shape,
    subscripted_names,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
SCHEDULER_PATH = SRC / "scheduler.py"
SCHEDULER_SOURCE = SCHEDULER_PATH.read_text(encoding="utf-8")
ADMISSION_SOURCE = (SRC / "admission_facts.py").read_text(encoding="utf-8")
REUSABLE_SOURCE = (SRC / "reusable_results.py").read_text(encoding="utf-8")
DISPATCH_SOURCE = (SRC / "dispatch_decisions.py").read_text(encoding="utf-8")
RUNTIME_FACTS_SOURCE = (SRC / "scheduling_runtime_facts.py").read_text(encoding="utf-8")

#: L1 —— `scheduler.py` 的**数据属性读取 allowlist**（README §34.7，收窄后的 13 项）。
ALLOWED_ATTRIBUTE_READS = frozenset(
    {
        "branches",
        "branch_id",
        "duplicate",
        "context_duplicate",
        "status",
        "evidence_sufficient",
        "insufficient_independent_value",
        "remaining_small",
        "dependency_too_strong",
        "cost_exceeds_benefit",
        "subagents",
        "concurrency_limit",
        "active_subagent_count",
    }
)

#: L5 —— import 禁用清单（含**成本层**：A11.9 不读 raw 成本）。
FORBIDDEN_IMPORTS = (
    "planner",
    "model_router",
    "adapters",
    "execution",
    "session",
    "entrypoint",
    "project_context",
    "pathlib",
    "os",
    "subprocess",
    "cost",
)

#: L4 / L10 —— 禁止构造的名字（`Plan` 允许作为**类型** import，但不得构造）。
CONSTRUCT_FORBIDDEN = ("Plan", "ModelSelection", "BranchState", "AgentRun", "RoutingRun")
IMPORT_FORBIDDEN = ("ModelSelection", "BranchState", "AgentRun", "RoutingRun")
SOURCE_NAMES = ("AdmissionFactsSource", "ReusableResultSource")

#: L11 —— `Scheduler().schedule` 的精确参数形状。
EXPECTED_KWONLY = (
    "plan",
    "branches_under_admission",
    "bundles",
    "admission_facts_by_branch",
    "reusable_results_by_branch",
    "capability",
    "runtime",
)


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def branch(branch_id: str, *, goal: str | None = None) -> BranchSpec:
    return BranchSpec(
        branch_id=branch_id,
        goal=goal or f"goal {branch_id}",
        read_only=True,
        completion_criteria=f"done {branch_id}",
    )


def multi_plan(*branch_ids: str) -> Plan:
    return Plan(
        mode=ExecutionMode.MULTI_AGENT,
        main_thread_duty="execute on the main thread",
        reason="test",
        branches=tuple(branch(bid) for bid in branch_ids),
    )


def plan_of_mode(mode: ExecutionMode) -> Plan:
    return Plan(mode=mode, main_thread_duty="duty", reason="test")


def capability(
    *,
    subagents: bool | None = True,
    concurrency_limit: int | None = 4,
) -> CapabilitySnapshot:
    """构造一个合法快照；任一维度为 ``None``（unknown）时必须给出 ``unavailable_reason``。"""
    unknown = subagents is None or concurrency_limit is None
    return CapabilitySnapshot(
        subagents=subagents,
        async_background=True,
        concurrency_limit=concurrency_limit,
        available_models=("provider-model-x",),
        available_effort_levels=(EffortLevel.HIGH,),
        state_wait_append_stop=True,
        hot_switch=False,
        reliable_cost_data=True,
        probed_dimensions=("subagents",),
        unavailable_reason=UnavailableReason.NOT_PROBED if unknown else None,
        probed_at="2026-09-18T00:00:00Z",
    )


def relation(status: bool | None = None, related: str | None = None) -> RelationFact:
    return RelationFact(status=status, related_branch_id=related)


def facts(
    *,
    duplicate: RelationFact | None = None,
    context_duplicate: RelationFact | None = None,
    evidence_sufficient: bool | None = None,
    insufficient_independent_value: bool | None = None,
    remaining_small: bool | None = None,
    dependency_too_strong: bool | None = None,
    cost_exceeds_benefit: bool | None = None,
) -> AdmissionFacts:
    """默认七槽全 ``None``（= 显式 UNKNOWN）。"""
    return AdmissionFacts(
        duplicate=duplicate if duplicate is not None else relation(),
        context_duplicate=context_duplicate if context_duplicate is not None else relation(),
        evidence_sufficient=evidence_sufficient,
        insufficient_independent_value=insufficient_independent_value,
        remaining_small=remaining_small,
        dependency_too_strong=dependency_too_strong,
        cost_exceeds_benefit=cost_exceeds_benefit,
    )


def runtime(active: int = 0) -> SchedulingRuntimeFacts:
    return SchedulingRuntimeFacts(active_subagent_count=active)


def runtime_facts_ok() -> SchedulingRuntimeFacts:
    return runtime(0)


def execution_value(planned, requested, actual=None) -> ExecutionValue:
    return ExecutionValue(
        planned=planned,
        planned_source=ConfirmationSource.PLANNER,
        requested=requested,
        requested_source=ConfirmationSource.REQUEST,
        actual=actual,
        actual_source=(
            ConfirmationSource.PROVIDER_API
            if actual is not None
            else ConfirmationSource.UNAVAILABLE
        ),
        observed=actual is not None,
        confidence=Confidence.LOW,
    )


def branch_return(branch_id: str, *, agent_id: str = "ag_1") -> BranchReturn:
    return BranchReturn(
        branch_id=branch_id,
        agent_id=agent_id,
        attempt=1,
        model=execution_value(ModelTier.STRONG, "provider-model-x"),
        effort=execution_value(EffortLevel.HIGH, EffortLevel.HIGH),
        conclusion_summary="结论",
        key_evidence=(),
        validation_state="pass",
        residual_risks=(),
        main_agent_todo=(),
    )


def validation(branch_id: str, *, passed: bool = True) -> ValidationRecord:
    return ValidationRecord(
        branch_id=branch_id,
        passed=passed,
        evidence=("e",),
        failed_criteria=() if passed else ("criterion",),
        detail="detail",
    )


def reusable_ref(branch_id: str) -> ReusableResultRef:
    return ReusableResultRef(
        source_branch_id=branch_id,
        branch_return=branch_return(branch_id),
        validation=validation(branch_id),
    )


def bundle(branch_id: str) -> ContextBundle:
    return ContextBundle(
        goal=f"goal {branch_id}",
        background=(),
        constraints=(),
        allowed_operations=(),
        forbidden_operations=(),
        output_format="f",
        completion_criteria=f"done {branch_id}",
        confirmed_conclusions=(),
        excluded=(),
        includes_history=False,
    )


def bundles_for(*branch_ids: str) -> dict[str, ContextBundle]:
    return {bid: bundle(bid) for bid in branch_ids}


def schedule(
    plan: Plan,
    *,
    admission: tuple[str, ...] | None = None,
    bundles: dict | None = None,
    facts_map: dict | None = None,
    reusable: dict | None = None,
    cap: CapabilitySnapshot | None = None,
    run: SchedulingRuntimeFacts | None = None,
):
    ids = tuple(spec.branch_id for spec in plan.branches)
    return Scheduler().schedule(
        plan=plan,
        branches_under_admission=ids if admission is None else admission,
        bundles=bundles_for(*ids) if bundles is None else bundles,
        admission_facts_by_branch=(
            {bid: facts() for bid in ids} if facts_map is None else facts_map
        ),
        reusable_results_by_branch={} if reusable is None else reusable,
        capability=capability() if cap is None else cap,
        runtime=runtime_facts_ok() if run is None else run,
    )


def actions(decisions) -> list[DispatchAction]:
    return [decision.action for decision in decisions]


# ==========================================================================
# B1 / B2 —— 顺序与基数
# ==========================================================================


class TestOrderAndCardinality:
    def test_b1_output_order_projects_plan_order(self) -> None:
        plan = multi_plan("br_a", "br_b", "br_c")
        decisions = schedule(plan, admission=("br_c", "br_a", "br_b"))
        assert [d.branch_id for d in decisions] == ["br_a", "br_b", "br_c"]
        assert len(decisions) == 3

    @pytest.mark.parametrize(
        "mode", [ExecutionMode.DIRECT, ExecutionMode.STAGED_SINGLE_AGENT]
    )
    def test_b2_zero_child_returns_empty_tuple(self, mode: ExecutionMode) -> None:
        plan = plan_of_mode(mode)
        assert schedule(plan, admission=(), bundles={}, facts_map={}, reusable={}) == ()


# ==========================================================================
# B3 —— 精确覆盖
# ==========================================================================


class TestExactCoverage:
    def test_b3_missing_facts_key_fails(self) -> None:
        plan = multi_plan("br_a", "br_b")
        with pytest.raises(SchedulerError, match="精确覆盖"):
            schedule(plan, facts_map={"br_a": facts()})

    def test_b3_extra_facts_key_is_another_legal_plan_child(self) -> None:
        """多出来的那个 key 本身就是**合法的 Plan child** —— 证明覆盖面绑的是**当前 admission set**。"""
        plan = multi_plan("br_a", "br_b", "br_c")
        with pytest.raises(SchedulerError, match="精确覆盖"):
            schedule(
                plan,
                admission=("br_a", "br_b"),
                facts_map={"br_a": facts(), "br_b": facts(), "br_c": facts()},
            )

    def test_b3_empty_admission_with_non_empty_facts_fails(self) -> None:
        plan = plan_of_mode(ExecutionMode.DIRECT)
        with pytest.raises(SchedulerError, match="精确覆盖"):
            schedule(plan, admission=(), bundles={}, facts_map={"br_a": facts()}, reusable={})

    def test_b3_empty_admission_with_empty_facts_passes(self) -> None:
        plan = plan_of_mode(ExecutionMode.STAGED_SINGLE_AGENT)
        assert schedule(plan, admission=(), bundles={}, facts_map={}, reusable={}) == ()


# ==========================================================================
# B4 / B5 —— UNKNOWN 与 blocker 极性
# ==========================================================================


class TestUnknownAndBlockerPolarity:
    def test_b4_all_unknown_starts_when_capability_clearly_supports(self) -> None:
        """七槽全 UNKNOWN + 无 reusable + `subagents is True` + 已知容量可用 ⇒ START。

        ⚠️ 不是"全 None 就 START"：若 `subagents is None` 或容量为 0，A11.8 后续仍会 QUEUE（见 B9）。
        """
        plan = multi_plan("br_a", "br_b")
        assert actions(schedule(plan)) == [DispatchAction.START] * 2

    ALL_UNKNOWN_IS_NOT_REJECT = True

    @pytest.mark.parametrize(
        "slot",
        [
            "evidence_sufficient",
            "insufficient_independent_value",
            "remaining_small",
            "dependency_too_strong",
            "cost_exceeds_benefit",
        ],
    )
    def test_b5_true_is_a_blocker(self, slot: str) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan,
            facts_map={"br_a": facts(**{slot: True}), "br_b": facts()},
        )
        assert actions(decisions) == [DispatchAction.REJECT, DispatchAction.START]

    @pytest.mark.parametrize(
        "slot",
        [
            "evidence_sufficient",
            "insufficient_independent_value",
            "remaining_small",
            "dependency_too_strong",
            "cost_exceeds_benefit",
        ],
    )
    def test_b5_false_is_not_a_veto(self, slot: str) -> None:
        """``False`` **只**证明"不构成 veto"，不是无条件 START（仍受 REUSE/QUEUE 前置条件约束）。"""
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan,
            facts_map={"br_a": facts(**{slot: False}), "br_b": facts()},
        )
        assert actions(decisions) == [DispatchAction.START] * 2

    def test_b5_relation_true_is_a_blocker(self) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan,
            facts_map={
                "br_a": facts(duplicate=relation(True, "br_b")),
                "br_b": facts(context_duplicate=relation(True, "br_a")),
            },
        )
        assert actions(decisions) == [DispatchAction.REJECT, DispatchAction.REJECT]

    def test_b5_relation_false_is_not_a_veto(self) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan,
            facts_map={
                "br_a": facts(duplicate=relation(False)),
                "br_b": facts(context_duplicate=relation(False)),
            },
        )
        assert actions(decisions) == [DispatchAction.START] * 2


# ==========================================================================
# B6 —— RelationFact 严格构造
# ==========================================================================


class TestRelationFactStrictConstructor:
    @pytest.mark.parametrize("bad", [1, 0, "", "x", 1.0, []])
    def test_b6_non_bool_status_is_rejected(self, bad: object) -> None:
        with pytest.raises(TypeError, match="status 必须是 None 或 bool"):
            RelationFact(status=bad)  # type: ignore[arg-type]

    def test_b6_true_requires_related_branch_id(self) -> None:
        with pytest.raises(ValueError, match="必须给出 related_branch_id"):
            RelationFact(status=True)

    def test_b6_true_rejects_an_illegal_branch_id(self) -> None:
        with pytest.raises(ValueError):
            RelationFact(status=True, related_branch_id="not-a-branch-id")

    @pytest.mark.parametrize("status", [False, None])
    def test_b6_false_or_none_must_not_carry_an_id(self, status: bool | None) -> None:
        with pytest.raises(ValueError, match="不得携带 related_branch_id"):
            RelationFact(status=status, related_branch_id="br_a")


# ==========================================================================
# B7 / B8 —— 四值优先级的交叉条件
# ==========================================================================


class TestPriorityCrossConditions:
    def test_b7_reuse_beats_capability_reject(self) -> None:
        """reusable + `subagents=False`（capability 派生 REJECT）+ veto ⇒ 仍是 **REUSE**。"""
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan,
            facts_map={"br_a": facts(remaining_small=True), "br_b": facts()},
            reusable={"br_a": reusable_ref("br_a")},
            cap=capability(subagents=False, concurrency_limit=None),
        )
        assert actions(decisions) == [DispatchAction.REUSE, DispatchAction.REJECT]

    def test_b7_reuse_beats_capability_queue(self) -> None:
        """reusable + `subagents=None`（capability 派生 QUEUE）⇒ 仍是 **REUSE**。"""
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan,
            reusable={"br_a": reusable_ref("br_a")},
            cap=capability(subagents=None, concurrency_limit=None),
        )
        assert actions(decisions) == [DispatchAction.REUSE, DispatchAction.QUEUE]

    def test_b7_reuse_beats_zero_capacity_and_veto(self) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan,
            facts_map={"br_a": facts(cost_exceeds_benefit=True), "br_b": facts()},
            reusable={"br_a": reusable_ref("br_a")},
            cap=capability(concurrency_limit=0),
        )
        assert actions(decisions) == [DispatchAction.REUSE, DispatchAction.QUEUE]

    def test_b8_reject_beats_capability_queue(self) -> None:
        """`veto + subagents=None` ⇒ **REJECT**（REJECT 高于 capability 派生的 QUEUE）。"""
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan,
            facts_map={"br_a": facts(evidence_sufficient=True), "br_b": facts()},
            cap=capability(subagents=None, concurrency_limit=None),
        )
        assert actions(decisions) == [DispatchAction.REJECT, DispatchAction.QUEUE]

    def test_b8_reject_beats_known_capacity_zero(self) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan,
            facts_map={"br_a": facts(dependency_too_strong=True), "br_b": facts()},
            cap=capability(concurrency_limit=0),
        )
        assert actions(decisions) == [DispatchAction.REJECT, DispatchAction.QUEUE]


# ==========================================================================
# B9 —— 能力 / 容量三态
# ==========================================================================


class TestCapabilityAndCapacity:
    def test_b9_subagents_false_rejects(self) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(
            plan, cap=capability(subagents=False, concurrency_limit=None)
        )
        assert actions(decisions) == [DispatchAction.REJECT] * 2

    def test_b9_subagents_none_queues(self) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(plan, cap=capability(subagents=None, concurrency_limit=None))
        assert actions(decisions) == [DispatchAction.QUEUE] * 2

    def test_b9_limit_zero_queues(self) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(plan, cap=capability(concurrency_limit=0))
        assert actions(decisions) == [DispatchAction.QUEUE] * 2

    def test_b9_limit_none_queues(self) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(plan, cap=capability(concurrency_limit=None))
        assert actions(decisions) == [DispatchAction.QUEUE] * 2

    def test_b9_slots_saturated_queues(self) -> None:
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(plan, cap=capability(concurrency_limit=2), run=runtime(2))
        assert actions(decisions) == [DispatchAction.QUEUE] * 2

    def test_b9_active_above_ceiling_never_starts(self) -> None:
        """`active > ceiling`（观测到超额）⇒ 绝不 START。"""
        plan = multi_plan("br_a", "br_b")
        decisions = schedule(plan, cap=capability(concurrency_limit=1), run=runtime(5))
        assert actions(decisions) == [DispatchAction.QUEUE] * 2


# ==========================================================================
# B10 —— 单 pass 槽分配
# ==========================================================================


class TestSinglePassSlotAllocation:
    def test_b10_slots_are_consumed_in_plan_order(self) -> None:
        plan = multi_plan("br_a", "br_b", "br_c")
        decisions = schedule(plan, cap=capability(concurrency_limit=2), run=runtime(0))
        assert actions(decisions) == [
            DispatchAction.START,
            DispatchAction.START,
            DispatchAction.QUEUE,
        ]

    def test_b10_reuse_does_not_consume_a_slot(self) -> None:
        plan = multi_plan("br_a", "br_b", "br_c")
        decisions = schedule(
            plan,
            reusable={"br_a": reusable_ref("br_a")},
            cap=capability(concurrency_limit=2),
            run=runtime(0),
        )
        assert actions(decisions) == [
            DispatchAction.REUSE,
            DispatchAction.START,
            DispatchAction.START,
        ]

    def test_b10_reject_does_not_consume_a_slot(self) -> None:
        plan = multi_plan("br_a", "br_b", "br_c")
        decisions = schedule(
            plan,
            facts_map={
                "br_a": facts(remaining_small=True),
                "br_b": facts(),
                "br_c": facts(),
            },
            cap=capability(concurrency_limit=2),
            run=runtime(0),
        )
        assert actions(decisions) == [
            DispatchAction.REJECT,
            DispatchAction.START,
            DispatchAction.START,
        ]

    def test_b10_partially_used_ceiling(self) -> None:
        plan = multi_plan("br_a", "br_b", "br_c")
        decisions = schedule(plan, cap=capability(concurrency_limit=3), run=runtime(2))
        assert actions(decisions) == [
            DispatchAction.START,
            DispatchAction.QUEUE,
            DispatchAction.QUEUE,
        ]


# ==========================================================================
# B11 / B12 —— DTO 严格构造
# ==========================================================================


class TestDtoStrictConstructors:
    def test_b11_ref_rejects_wrong_types(self) -> None:
        with pytest.raises(TypeError, match="branch_return 必须是 BranchReturn"):
            ReusableResultRef(
                source_branch_id="br_a",
                branch_return="nope",  # type: ignore[arg-type]
                validation=validation("br_a"),
            )
        with pytest.raises(TypeError, match="validation 必须是 ValidationRecord"):
            ReusableResultRef(
                source_branch_id="br_a",
                branch_return=branch_return("br_a"),
                validation="nope",  # type: ignore[arg-type]
            )

    def test_b11_ref_rejects_illegal_source_branch_id(self) -> None:
        """source-id 信任边界：非法 `source_branch_id` 必须被 **`ReusableResultRef` 自己**拒绝。

        ⚠️ 两个假绿来源都必须堵掉：

        1. **上游 fixture 提前抛错**：`branch_return("nope")` 在**参数求值**阶段就会抛
           `ContractError`（`BranchReturn.__post_init__` 自己就调 `validate_branch_id`），
           测试根本进不了 `ReusableResultRef.__post_init__` ⇒ 其余两个对象必须**完全合法**；
        2. **被一致性检查顺手拦住**：非法 source id 必然与合法对象的三处 id 不一致，
           于是"三处 id 必须一致"也会抛 `ValueError` ⇒ 只断言 `raises(ValueError)` 仍会假绿。

        ⇒ 因此断言**具体拒绝理由**（`BranchId` 格式）。删掉
        `validate_branch_id(self.source_branch_id)` 之后，错误理由会变成"三处 id 必须一致"，
        本测试**必然变红**（mutation proof 见 README §39）。
        """
        with pytest.raises(ValueError, match="BranchId"):
            ReusableResultRef(
                source_branch_id="nope",
                branch_return=branch_return("br_a"),
                validation=validation("br_a"),
            )

    def test_b11_ref_rejects_mismatched_branch_ids(self) -> None:
        with pytest.raises(ValueError, match="必须一致"):
            ReusableResultRef(
                source_branch_id="br_a",
                branch_return=branch_return("br_a"),
                validation=validation("br_b"),
            )

    def test_b11_ref_rejects_failed_validation(self) -> None:
        with pytest.raises(ValueError):
            ReusableResultRef(
                source_branch_id="br_a",
                branch_return=branch_return("br_a"),
                validation=validation("br_a", passed=False),
            )

    def test_b11_mapping_key_outside_plan_fails(self) -> None:
        plan = multi_plan("br_a", "br_b")
        with pytest.raises(SchedulerError, match="越界"):
            schedule(plan, reusable={"br_ghost": reusable_ref("br_ghost")})

    def test_b18_mapping_key_is_a_plan_child_but_not_admitted(self) -> None:
        """A11 要的是"**当前待 admission** child"，不只是"属于 Plan"。"""
        plan = multi_plan("br_a", "br_b", "br_c")
        with pytest.raises(SchedulerError, match="越界"):
            schedule(
                plan,
                admission=("br_a", "br_b"),
                facts_map={"br_a": facts(), "br_b": facts()},
                reusable={"br_c": reusable_ref("br_c")},
            )

    def test_b12_decision_rejects_illegal_inputs(self) -> None:
        with pytest.raises(ValueError):
            DispatchDecision(branch_id="nope", action=DispatchAction.START, reason="r")
        with pytest.raises(TypeError, match="action 必须是 DispatchAction"):
            DispatchDecision(
                branch_id="br_a", action="start", reason="r"  # type: ignore[arg-type]
            )
        with pytest.raises(TypeError, match="action 必须是 DispatchAction"):
            DispatchDecision(
                branch_id="br_a", action=True, reason="r"  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError, match="reason 必须是非空 str"):
            DispatchDecision(branch_id="br_a", action=DispatchAction.START, reason="   ")
        with pytest.raises(ValueError, match="reason 必须是非空 str"):
            DispatchDecision(
                branch_id="br_a", action=DispatchAction.START, reason=3  # type: ignore[arg-type]
            )


# ==========================================================================
# B13 / B14 —— 只读与确定性
# ==========================================================================


class TestReadOnlyAndDeterminism:
    def test_b13_runtime_facts_are_not_mutated(self) -> None:
        plan = multi_plan("br_a", "br_b")
        run = runtime(1)
        before = dataclasses.asdict(run)
        schedule(plan, run=run)
        assert dataclasses.asdict(run) == before

    def test_b14_decisions_are_deterministic(self) -> None:
        plan = multi_plan("br_a", "br_b", "br_c")
        cap = capability(concurrency_limit=2)
        first = schedule(plan, cap=cap)
        second = schedule(plan, cap=cap)
        assert first == second


# ==========================================================================
# B15 / B16 / B17 —— admission 集合的输入失败路径
# ==========================================================================


class TestAdmissionSetFailures:
    def test_b15_duplicate_admission_id_fails(self) -> None:
        plan = multi_plan("br_a", "br_b")
        with pytest.raises(SchedulerError, match="不得含重复"):
            schedule(plan, admission=("br_a", "br_a"), facts_map={"br_a": facts()})

    def test_b16_admission_id_outside_plan_fails(self) -> None:
        plan = multi_plan("br_a", "br_b")
        with pytest.raises(SchedulerError, match="子集"):
            schedule(
                plan,
                admission=("br_a", "br_ghost"),
                facts_map={"br_a": facts(), "br_ghost": facts()},
            )

    def test_b17_admitted_child_without_prepared_bundle_fails(self) -> None:
        plan = multi_plan("br_a", "br_b")
        with pytest.raises(SchedulerError, match="prepared ContextBundle"):
            schedule(plan, admission=("br_a",), facts_map={"br_a": facts()}, bundles={})

    def test_b17_bundles_may_contain_not_yet_admitted_children(self) -> None:
        """``[08]`` 预构造**全部** child，后续 pass 只裁决子集 ⇒ 多余的 bundle **不得**报错。"""
        plan = multi_plan("br_a", "br_b", "br_c")
        decisions = schedule(
            plan,
            admission=("br_a",),
            facts_map={"br_a": facts()},
            bundles=bundles_for("br_a", "br_b", "br_c"),
        )
        assert [d.branch_id for d in decisions] == ["br_a"]


# ==========================================================================
# B19 / B20 —— 值 DTO 的严格构造
# ==========================================================================


class TestValueDtoConstructors:
    @pytest.mark.parametrize(
        "slot",
        [
            "evidence_sufficient",
            "insufficient_independent_value",
            "remaining_small",
            "dependency_too_strong",
            "cost_exceeds_benefit",
        ],
    )
    @pytest.mark.parametrize("bad", [1, 0, "", "x"])
    def test_b19_non_bool_slot_values_are_rejected(self, slot: str, bad: object) -> None:
        with pytest.raises(TypeError, match="必须是 None 或 bool"):
            facts(**{slot: bad})

    @pytest.mark.parametrize("slot", ["duplicate", "context_duplicate"])
    def test_b19_relation_slots_must_be_relation_facts(self, slot: str) -> None:
        """直接构造 `AdmissionFacts`（绕开 fixture 的默认值），把非法值真送进 `__post_init__`。"""
        base = {
            "duplicate": relation(),
            "context_duplicate": relation(),
            "evidence_sufficient": None,
            "insufficient_independent_value": None,
            "remaining_small": None,
            "dependency_too_strong": None,
            "cost_exceeds_benefit": None,
        }
        for bad in (True, False, None, "none"):
            with pytest.raises(TypeError, match="必须是 RelationFact"):
                AdmissionFacts(**{**base, slot: bad})

    def test_b19_all_unknown_shape_is_legal(self) -> None:
        """"全 None 合法" = 五个 bool 槽为 None **且**两个关系槽是
        `RelationFact(status=None, related_branch_id=None)` —— 绝不是 `duplicate=None`。"""
        value = facts()
        assert value.duplicate == RelationFact(status=None, related_branch_id=None)
        assert value.context_duplicate == RelationFact(status=None)
        for slot in (
            "evidence_sufficient",
            "insufficient_independent_value",
            "remaining_small",
            "dependency_too_strong",
            "cost_exceeds_benefit",
        ):
            assert getattr(value, slot) is None

    def test_b19_legal_values_pass(self) -> None:
        value = facts(
            duplicate=relation(True, "br_b"),
            context_duplicate=relation(False),
            evidence_sufficient=True,
            insufficient_independent_value=False,
            remaining_small=None,
            dependency_too_strong=False,
            cost_exceeds_benefit=None,
        )
        assert value.duplicate.status is True

    @pytest.mark.parametrize("bad", [True, False, -1, 1.0, "1"])
    def test_b20_active_count_rejects_non_int_or_negative(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            SchedulingRuntimeFacts(active_subagent_count=bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize("good", [0, 1, 7])
    def test_b20_zero_and_positive_pass(self, good: int) -> None:
        assert SchedulingRuntimeFacts(active_subagent_count=good).active_subagent_count == good

    def test_b20_missing_argument_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            SchedulingRuntimeFacts()  # type: ignore[call-arg]


# ==========================================================================
# L1–L12 —— 结构锁（对真实源码）
# ==========================================================================


class TestStructuralLocks:
    def test_l1_data_attribute_reads_stay_within_the_allowlist(self) -> None:
        reads = scheduler_data_attribute_reads(SCHEDULER_SOURCE)
        assert reads <= ALLOWED_ATTRIBUTE_READS, sorted(reads - ALLOWED_ATTRIBUTE_READS)

    def test_l2_mapping_get_is_forbidden(self) -> None:
        assert mapping_get_calls(SCHEDULER_SOURCE) == []

    def test_l3_exact_cover_is_bound_to_real_operands_exactly_once(self) -> None:
        assert exact_keys_comparisons(SCHEDULER_SOURCE) == [(["Eq"], True)]
        assert helper_call_count(SCHEDULER_SOURCE) == 1
        assert helper_call_operands(SCHEDULER_SOURCE, "_require_exact_keys") == [
            ("admission_facts_by_branch", "branches_under_admission")
        ]

    def test_l4_no_second_decision_maker(self) -> None:
        assert calls_to(SCHEDULER_SOURCE, CONSTRUCT_FORBIDDEN) == []
        assert resolved_symbols(SCHEDULER_SOURCE) & set(IMPORT_FORBIDDEN) == set()
        assert "Plan" in resolved_symbols(SCHEDULER_SOURCE)

    def test_l5_forbidden_imports_are_absent(self) -> None:
        assert forbidden_import_hits(SCHEDULER_SOURCE, FORBIDDEN_IMPORTS) == set()

    def test_l6_sources_are_neither_imported_nor_called(self) -> None:
        assert imported_names(SCHEDULER_SOURCE) & set(SOURCE_NAMES) == set()
        assert calls_to(SCHEDULER_SOURCE, SOURCE_NAMES) == []

    def test_l7_dispatch_decision_has_exactly_one_construction_site(self) -> None:
        sources = {
            path.relative_to(REPO_ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((REPO_ROOT / "src").rglob("*.py"))
        }
        sites = {
            path: calls_to(source, ("DispatchDecision",))
            for path, source in sources.items()
            if calls_to(source, ("DispatchDecision",))
        }
        assert set(sites) == {"src/adaptive_router/scheduler.py"}, sites
        assert sum(len(value) for value in sites.values()) == 1

    def test_l8_dto_shapes_are_frozen(self) -> None:
        assert enum_member_names(DISPATCH_SOURCE, "DispatchAction") == (
            "START",
            "QUEUE",
            "REJECT",
            "REUSE",
        )
        assert dataclass_field_names(DISPATCH_SOURCE, "DispatchDecision") == (
            "branch_id",
            "action",
            "reason",
        )
        assert dataclass_field_names(ADMISSION_SOURCE, "AdmissionFacts") == (
            "duplicate",
            "context_duplicate",
            "evidence_sufficient",
            "insufficient_independent_value",
            "remaining_small",
            "dependency_too_strong",
            "cost_exceeds_benefit",
        )
        assert dataclass_field_names(ADMISSION_SOURCE, "RelationFact") == (
            "status",
            "related_branch_id",
        )
        assert dataclass_field_names(REUSABLE_SOURCE, "ReusableResultRef") == (
            "source_branch_id",
            "branch_return",
            "validation",
        )
        assert dataclass_field_names(RUNTIME_FACTS_SOURCE, "SchedulingRuntimeFacts") == (
            "active_subagent_count",
        )

    def test_l9_no_open_call(self) -> None:
        assert open_calls(SCHEDULER_SOURCE) == []

    def test_l10_no_state_writes(self) -> None:
        assert calls_to(SCHEDULER_SOURCE, ("AgentRun", "BranchState", "RoutingRun")) == []

    def test_l11_exact_signature(self) -> None:
        shape = signature_shape(SCHEDULER_SOURCE, "Scheduler", "schedule")
        assert shape == {
            "positional": ["self"],
            "kwonly": list(EXPECTED_KWONLY),
            "defaults": [False] * len(EXPECTED_KWONLY),
            "vararg": None,
            "kwarg": None,
        }

    def test_l12_membership_only_no_subscript(self) -> None:
        subscripts = subscripted_names(SCHEDULER_SOURCE)
        assert "bundles" not in subscripts
        assert "reusable_results_by_branch" not in subscripts
        # 正对照：七槽必须真的取出来（否则这条锁可能空转）
        assert "admission_facts_by_branch" in subscripts

    def test_l8_and_l11_agree_with_the_real_signature(self) -> None:
        """R 面复核：`inspect` 看到的签名与 AST 锁一致（绑定方法 ⇒ 不含 self）。"""
        parameters = inspect.signature(Scheduler().schedule).parameters
        assert tuple(parameters) == EXPECTED_KWONLY
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in parameters.values()
        )


# ==========================================================================
# 敏感性 probe：每条锁都必须能"因目标语义变异而红"
# ==========================================================================

MUT_L1_MODE = SCHEDULER_SOURCE + "\n_ = plan.mode\n"
MUT_L1_READ_ONLY = SCHEDULER_SOURCE + "\n_ = spec.read_only\n"
MUT_L1_BUNDLE = SCHEDULER_SOURCE + "\n_ = bundle.goal\n"
MUT_L1_RELATED = SCHEDULER_SOURCE + "\n_ = facts.duplicate.related_branch_id\n"
MUT_L2_GET = SCHEDULER_SOURCE + '\n_ = admission_facts_by_branch.get("br_a")\n'
MUT_L3_SUBSET = SCHEDULER_SOURCE.replace(
    "if set(actual) == set(expected):", "if set(actual) <= set(expected):"
)
MUT_L3_DROP_CALL = SCHEDULER_SOURCE.replace(
    """        _require_exact_keys(
            admission_facts_by_branch,
            branches_under_admission,
            label="admission_facts_by_branch",
        )""",
    "        pass",
)
MUT_L3_SECOND_CALL = SCHEDULER_SOURCE.replace(
    "        stray = sorted(set(reusable_results_by_branch) - admitted)",
    """        _require_exact_keys(
            reusable_results_by_branch,
            branches_under_admission,
            label="reusable_results_by_branch",
        )
        stray = sorted(set(reusable_results_by_branch) - admitted)""",
)
MUT_L3_OTHER_OPERANDS = SCHEDULER_SOURCE.replace(
    "            admission_facts_by_branch,\n            branches_under_admission,",
    "            reusable_results_by_branch,\n            branches_under_admission,",
)
MUT_L4_PLAN = SCHEDULER_SOURCE + '\n_ = Plan(mode=None, main_thread_duty="x", reason="y")\n'
MUT_L5_COST_BINDING = SCHEDULER_SOURCE + "\nfrom . import cost as c\n"
MUT_L5_COST_ABS = SCHEDULER_SOURCE + "\nimport adaptive_router.cost as c\n"
MUT_L5_ADAPTERS = SCHEDULER_SOURCE + "\nfrom . import adapters\n"
MUT_L6_SOURCE_IMPORT = SCHEDULER_SOURCE + "\nfrom .admission_facts import AdmissionFactsSource\n"
MUT_L6_SOURCE_CALL = SCHEDULER_SOURCE + "\n_ = ReusableResultSource()\n"
MUT_L9_OPEN = SCHEDULER_SOURCE + '\n_ = open("x")\n'
MUT_L10_BRANCH_STATE = SCHEDULER_SOURCE + '\n_ = BranchState("waiting")\n'
MUT_L11_COSTS = SCHEDULER_SOURCE.replace(
    "        runtime: SchedulingRuntimeFacts,",
    "        runtime: SchedulingRuntimeFacts,\n        costs: object = None,",
)
MUT_L11_POSITIONAL = SCHEDULER_SOURCE.replace("        self,\n        *,\n", "        self,\n")
MUT_L11_KWARGS = SCHEDULER_SOURCE.replace(
    "        runtime: SchedulingRuntimeFacts,\n    ) -> tuple[DispatchDecision, ...]:",
    "        runtime: SchedulingRuntimeFacts,\n        **kwargs: object,\n    ) -> tuple[DispatchDecision, ...]:",
)
MUT_L12_BUNDLE_SUBSCRIPT = SCHEDULER_SOURCE + "\n_ = bundles[list(bundles)[0]]\n"
MUT_L12_REF_SUBSCRIPT = SCHEDULER_SOURCE + "\n_ = reusable_results_by_branch[list(reusable_results_by_branch)[0]]\n"
MUT_L8_EXTRA_FIELD = DISPATCH_SOURCE.replace(
    "    reason: str\n", "    reason: str\n    extra: str = \"x\"\n"
)
MUT_L8_EXTRA_MEMBER = DISPATCH_SOURCE.replace(
    '    REUSE = "reuse"\n', '    REUSE = "reuse"\n    EXTRA = "extra"\n'
)


class TestLockSensitivity:
    def test_l1_probes_go_red(self) -> None:
        for mutated, expected in (
            (MUT_L1_MODE, "mode"),
            (MUT_L1_READ_ONLY, "read_only"),
            (MUT_L1_BUNDLE, "goal"),
            (MUT_L1_RELATED, "related_branch_id"),
        ):
            offenders = scheduler_data_attribute_reads(mutated) - ALLOWED_ATTRIBUTE_READS
            assert offenders == {expected}, mutated[-40:]

    def test_l1_does_not_flag_enum_member_constants(self) -> None:
        """负对照：`DispatchAction.<成员>` 是**常量引用**，不是数据读取。"""
        assert not (
            scheduler_data_attribute_reads(SCHEDULER_SOURCE)
            & {"START", "QUEUE", "REJECT", "REUSE"}
        )

    def test_l2_probe_goes_red(self) -> None:
        assert mapping_get_calls(MUT_L2_GET) != []

    def test_l3_probes_go_red(self) -> None:
        assert exact_keys_comparisons(MUT_L3_SUBSET) == [(["LtE"], True)]
        assert helper_call_count(MUT_L3_DROP_CALL) == 0
        assert helper_call_count(MUT_L3_SECOND_CALL) == 2
        assert helper_call_operands(MUT_L3_OTHER_OPERANDS, "_require_exact_keys") == [
            ("reusable_results_by_branch", "branches_under_admission")
        ]

    def test_l4_probe_goes_red(self) -> None:
        assert calls_to(MUT_L4_PLAN, CONSTRUCT_FORBIDDEN) != []

    def test_l5_alias_aware_probes_go_red(self) -> None:
        for mutated in (MUT_L5_COST_BINDING, MUT_L5_COST_ABS):
            assert forbidden_import_hits(mutated, FORBIDDEN_IMPORTS) == {"cost"}
        assert forbidden_import_hits(MUT_L5_ADAPTERS, FORBIDDEN_IMPORTS) == {"adapters"}

    def test_l6_probes_go_red(self) -> None:
        assert imported_names(MUT_L6_SOURCE_IMPORT) & set(SOURCE_NAMES) == {
            "AdmissionFactsSource"
        }
        assert calls_to(MUT_L6_SOURCE_CALL, SOURCE_NAMES) != []

    def test_l7_probe_counts_a_second_site(self) -> None:
        sources = {
            "src/adaptive_router/scheduler.py": SCHEDULER_SOURCE,
            "src/adaptive_router/other.py": (
                "from .dispatch_decisions import DispatchDecision as DD\nx = DD()\n"
            ),
        }
        sites = {
            path: calls_to(source, ("DispatchDecision",))
            for path, source in sources.items()
            if calls_to(source, ("DispatchDecision",))
        }
        assert set(sites) == {
            "src/adaptive_router/scheduler.py",
            "src/adaptive_router/other.py",
        }

    def test_l8_probes_go_red(self) -> None:
        assert dataclass_field_names(MUT_L8_EXTRA_FIELD, "DispatchDecision") == (
            "branch_id",
            "action",
            "reason",
            "extra",
        )
        assert enum_member_names(MUT_L8_EXTRA_MEMBER, "DispatchAction") == (
            "START",
            "QUEUE",
            "REJECT",
            "REUSE",
            "EXTRA",
        )

    def test_l9_probe_goes_red(self) -> None:
        assert open_calls(MUT_L9_OPEN) != []

    def test_l10_probe_goes_red(self) -> None:
        assert calls_to(MUT_L10_BRANCH_STATE, ("BranchState",)) != []

    def test_l11_probes_go_red(self) -> None:
        with_costs = signature_shape(MUT_L11_COSTS, "Scheduler", "schedule")
        assert "costs" in with_costs["kwonly"]
        assert with_costs["defaults"].count(True) == 1

        positional = signature_shape(MUT_L11_POSITIONAL, "Scheduler", "schedule")
        assert positional["kwonly"] == []
        assert "plan" in positional["positional"]

        with_kwargs = signature_shape(MUT_L11_KWARGS, "Scheduler", "schedule")
        assert with_kwargs["kwarg"] == "kwargs"

    def test_l12_probes_go_red(self) -> None:
        assert "bundles" in subscripted_names(MUT_L12_BUNDLE_SUBSCRIPT)
        assert "reusable_results_by_branch" in subscripted_names(MUT_L12_REF_SUBSCRIPT)

    def test_l12_negative_controls_do_not_go_red(self) -> None:
        """`for bid in bundles` / `bid in bundles` / `set(bundles)` **不得**报红。"""
        source = SCHEDULER_SOURCE + (
            "\n_ = [bid for bid in bundles]\n_ = 'br_a' in bundles\n_ = set(bundles)\n"
        )
        subscripts = subscripted_names(source)
        assert "bundles" not in subscripts
        assert "reusable_results_by_branch" not in subscripts

    def test_imported_symbol_bindings_helper_is_used_consistently(self) -> None:
        """L4/L6 的 alias-aware 判定依赖 binding 解析 —— 抽一条最小正例。"""
        bindings = imported_symbol_bindings(
            "from .contracts import Plan as P\nfrom .dispatch_decisions import DispatchDecision as DD\n"
        )
        assert bindings == {"P": "Plan", "DD": "DispatchDecision"}
