"""Planner 行为测试（Step 5A ②）。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-PLAN-001  简单/局部/不划算 → 直接执行，不建形式化计划
  TEST-PLAN-002  阶段依赖紧密/上下文复制多/无真实代理 → 单代理阶段化
  TEST-PLAN-003  ≥2 独立且完成标准明确的子任务 + 并行收益 > 成本 → 真实多代理分流
  TEST-PLAN-005  七种不分流条件

  TEST-PLAN-004  **本批 deferred** —— "启动前六项在主线可见"同时承载于 [05] + [16]，
                 "对用户可见"要等 Reporting / Router 主线接起来后才算完整。
                 本批只保证 Planner 产生足够的**计划事实**（见 TestPlanFactsAreProduced）。

架构依据（2 节 [05]）：Planner 是**唯一**决定执行方式的节点。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.contracts import (
    CapabilitySnapshot,
    Evaluation,
    ExecutionMode,
    ImpactScope,
    Ordinal,
    Plan,
    ProjectContext,
    UnavailableReason,
)
from adaptive_router.planner import (
    CROSS_MODULE_MODULES,
    HEAVY_CONTEXT_SIZE,
    SPLIT_MIN_SUBTASKS,
    Evaluator,
    EvidenceStrength,
    NoSplitReason,
    PlannedSubtask,
    Planner,
    TaskFacts,
)
from adaptive_router.planning_constraints import EffectivePlanningConstraints

from _import_detectors import imported_module_tokens, imported_names

FIXED_TIME = "2026-09-17T00:00:00Z"


def unrestricted() -> EffectivePlanningConstraints:
    """``[02]`` **显式**允许分流（不是"没传"，也不是 fail-open）。

    5A 的既有用例全部走这条 —— ``auto_split_allowed=True`` 必须与 5A
    冻结行为**完全等价**（A3 钉子 ②）。
    """
    return EffectivePlanningConstraints(
        auto_split_allowed=True,
        rationale="no higher-priority split restriction",
    )


def no_split() -> EffectivePlanningConstraints:
    """``[02]`` 已解析出"更高规则限制自动分流"。"""
    return EffectivePlanningConstraints(
        auto_split_allowed=False,
        rationale="system: auto_split 被该层（system_denied）限制（L11）",
    )

#: 被测模块源码。⚠️ 只用于 **AST 解析**，不做子串扫描 ——
#: 模块 docstring 会**提到** `ModelSelection` / `main_thread_branch_id`
#: 以说明边界，那是文档，不是依赖。
MODULE_SOURCE = (
    pathlib.Path(__file__).resolve().parents[2] / "src/adaptive_router/planner.py"
).read_text(encoding="utf-8")
_MODULE_TREE = ast.parse(MODULE_SOURCE)


def _imported_names() -> set[str]:
    """源码中**真实 import 进来**的标识符（不含 docstring 里的提及）。"""
    return imported_names(MODULE_SOURCE)


def _imported_modules() -> set[str]:
    """被测模块的 import token（保守检测器，见 `_import_detectors`）。"""
    return imported_module_tokens(MODULE_SOURCE)


def _identifier_names() -> set[str]:
    """源码中**作为标识符使用**的名字（Name 节点，不含字符串/文档）。"""
    return {
        node.id for node in ast.walk(_MODULE_TREE) if isinstance(node, ast.Name)
    }


def _attribute_names() -> set[str]:
    return {
        node.attr for node in ast.walk(_MODULE_TREE) if isinstance(node, ast.Attribute)
    }


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def snapshot(**overrides: object) -> CapabilitySnapshot:
    base: dict[str, object] = dict(
        subagents=True,
        async_background=True,
        concurrency_limit=4,
        available_models=("gpt-5-codex",),
        available_effort_levels=(),
        state_wait_append_stop=True,
        hot_switch=True,
        reliable_cost_data=False,
        probed_dimensions=("subagents",),
        unavailable_reason=None,
        probed_at=FIXED_TIME,
    )
    base.update(overrides)
    return CapabilitySnapshot(**base)  # type: ignore[arg-type]


def project(**overrides: object) -> ProjectContext:
    base: dict[str, object] = dict(
        root="/repo",
        present=True,
        boundary_dirs=("src",),
        writable=(),
        denied=(),
        readme_refs=("README.md",),
        agents_md_present=False,
        agents_md_digest=None,
        test_commands=(),
        high_risk_files=(),
        model_preference=None,
        approval_preference=None,
        source_path=None,
        read_at=FIXED_TIME,
    )
    base.update(overrides)
    return ProjectContext(**base)  # type: ignore[arg-type]


def subtask(goal: str) -> PlannedSubtask:
    return PlannedSubtask(goal=goal, completion_criteria=f"report {goal} with evidence")


def facts(**overrides: object) -> TaskFacts:
    """默认：一个**足够分流**的任务（跨模块、3 个独立子任务、收益中上）。"""
    base: dict[str, object] = dict(
        affected_modules=4,
        mechanical=False,
        touches_cross_module_contracts=True,
        requires_iterative_debugging=True,
        is_review_task=False,
        subtasks=(subtask("A"), subtask("B"), subtask("C")),
        shared_context_size=0,
        has_sequential_dependency=False,
        branch_setup_cost=Ordinal.LOW,
        evidence=EvidenceStrength.STRONG,
        local_and_reversible=True,
    )
    base.update(overrides)
    return TaskFacts(**base)  # type: ignore[arg-type]


def make_plan(
    *,
    fact_overrides: dict[str, object] | None = None,
    capability: CapabilitySnapshot | None = None,
    project_context: ProjectContext | None = None,
    constraints: EffectivePlanningConstraints | None = None,
) -> Plan:
    fact_set = facts(**(fact_overrides or {}))
    cap = capability if capability is not None else snapshot()
    proj = project_context if project_context is not None else project()
    evaluation: Evaluation = Evaluator().evaluate(fact_set, capability=cap, project=proj)
    return Planner().plan(
        evaluation,
        fact_set,
        capability=cap,
        project=proj,
        constraints=constraints if constraints is not None else unrestricted(),
    )


# ==========================================================================
# TEST-PLAN-003 —— 真实多代理分流
# ==========================================================================


class TestPlan003MultiAgent:
    def test_returns_the_frozen_contract_type(self) -> None:
        assert isinstance(make_plan(), Plan)

    def test_independent_subtasks_with_benefit_split_into_branches(self) -> None:
        plan = make_plan()
        assert plan.mode is ExecutionMode.MULTI_AGENT
        assert plan.is_multi_agent is True
        assert len(plan.branches) == 3

    def test_at_least_two_independent_subtasks_are_required(self) -> None:
        plan = make_plan(
            fact_overrides={"subtasks": (subtask("A"), subtask("B"))}
        )
        assert plan.mode is ExecutionMode.MULTI_AGENT
        assert len(plan.branches) == SPLIT_MIN_SUBTASKS

    def test_each_branch_carries_goal_and_completion_criteria(self) -> None:
        plan = make_plan()
        for branch in plan.branches:
            assert branch.goal
            assert branch.completion_criteria
            assert branch.branch_id.startswith("br_")

    def test_branch_ids_are_unique(self) -> None:
        plan = make_plan()
        ids = [b.branch_id for b in plan.branches]
        assert len(set(ids)) == len(ids)

    def test_read_only_attribute_comes_from_the_declared_subtask(self) -> None:
        plan = make_plan(
            fact_overrides={
                "subtasks": (
                    PlannedSubtask("A", "done A", read_only=True),
                    PlannedSubtask("B", "done B", read_only=False),
                )
            }
        )
        assert [b.read_only for b in plan.branches] == [True, False]


# ==========================================================================
# TEST-PLAN-001 —— 直接执行
# ==========================================================================


class TestPlan001Direct:
    def test_simple_local_task_is_direct_with_no_branches(self) -> None:
        plan = make_plan(
            fact_overrides={
                "affected_modules": 1,
                "mechanical": True,
                "touches_cross_module_contracts": False,
                "requires_iterative_debugging": False,
                "subtasks": (),
            }
        )
        assert plan.mode is ExecutionMode.DIRECT
        assert plan.branches == ()

    def test_direct_carries_no_formal_plan(self) -> None:
        """L25：简单/局部 → 不展示形式化计划。"""
        plan = make_plan(
            fact_overrides={
                "affected_modules": 1,
                "mechanical": True,
                "touches_cross_module_contracts": False,
                "requires_iterative_debugging": False,
                "subtasks": (),
            }
        )
        assert plan.is_multi_agent is False
        assert plan.main_thread_duty

    def test_not_worth_splitting_is_direct(self) -> None:
        """收益不足（建分支太贵）→ 直接执行。"""
        plan = make_plan(
            fact_overrides={
                "subtasks": (subtask("A"), subtask("B")),
                "branch_setup_cost": Ordinal.HIGH,
            }
        )
        assert plan.mode is ExecutionMode.DIRECT
        assert plan.branches == ()


# ==========================================================================
# TEST-PLAN-002 —— 单代理阶段化
# ==========================================================================


class TestPlan002StagedSingleAgent:
    def test_strong_dependency_stages_instead_of_branching(self) -> None:
        """阶段依赖紧密 → 单代理阶段化，**不得模拟分支**。"""
        plan = make_plan(fact_overrides={"has_sequential_dependency": True})
        assert plan.mode is ExecutionMode.STAGED_SINGLE_AGENT
        assert plan.branches == ()

    def test_no_real_subagents_stages(self) -> None:
        """L17：无真实子代理 → 单代理阶段化，不暂停、不模拟分支。"""
        plan = make_plan(capability=snapshot(subagents=False))
        assert plan.mode is ExecutionMode.STAGED_SINGLE_AGENT
        assert plan.branches == ()

    def test_insufficient_capacity_stages(self) -> None:
        plan = make_plan(capability=snapshot(concurrency_limit=1))
        assert plan.mode is ExecutionMode.STAGED_SINGLE_AGENT
        assert plan.branches == ()

    def test_heavy_context_duplication_stages(self) -> None:
        """L25：**上下文复制多** → 单代理阶段化。

        3 个高度独立的子任务 + 大量重复共享上下文 + 其他条件正常 ——
        分流会把同一份大上下文复制到每个分支，收益被抵消。
        """
        plan = make_plan(
            fact_overrides={
                "subtasks": (subtask("A"), subtask("B"), subtask("C")),
                "shared_context_size": HEAVY_CONTEXT_SIZE,
            }
        )
        assert plan.mode is ExecutionMode.STAGED_SINGLE_AGENT
        assert plan.branches == ()

    def test_context_duplication_is_reported_as_a_blocker(self) -> None:
        fact_set = facts(shared_context_size=HEAVY_CONTEXT_SIZE + 5)
        cap = snapshot()
        evaluation = Evaluator().evaluate(fact_set, capability=cap, project=project())
        reasons = Planner().no_split_reasons(evaluation, fact_set, capability=cap)
        assert NoSplitReason.CONTEXT_DUPLICATION in reasons
        assert NoSplitReason.CONTEXT_DUPLICATION.is_l29_condition is False

    def test_context_duplication_is_not_confused_with_low_benefit(self) -> None:
        """它是**独立** blocker —— 即便并行收益被判为 MEDIUM 也要收回。"""
        fact_set = facts(shared_context_size=HEAVY_CONTEXT_SIZE)
        cap = snapshot()
        evaluation = Evaluator().evaluate(fact_set, capability=cap, project=project())
        assert evaluation.parallel_benefit is not Ordinal.LOW
        assert NoSplitReason.CONTEXT_DUPLICATION in Planner().no_split_reasons(
            evaluation, fact_set, capability=cap
        )

    def test_moderate_context_does_not_force_staging(self) -> None:
        """未达阈值的上下文量不得误伤分流。"""
        plan = make_plan(fact_overrides={"shared_context_size": 5})
        assert plan.mode is ExecutionMode.MULTI_AGENT

    def test_staged_plan_has_no_child_branches(self) -> None:
        for overrides, cap in (
            ({"has_sequential_dependency": True}, None),
            ({"shared_context_size": HEAVY_CONTEXT_SIZE}, None),
            ({}, snapshot(subagents=False)),
            ({}, snapshot(concurrency_limit=1)),
        ):
            plan = make_plan(fact_overrides=overrides, capability=cap)
            assert plan.branches == (), overrides

    def test_the_three_l25_staging_conditions_are_all_covered(self) -> None:
        """L25 的三种 STAGED 条件：**强依赖 / 上下文复制多 / 无真实代理**。"""
        covered = {
            "strong_dependency": make_plan(
                fact_overrides={"has_sequential_dependency": True}
            ),
            "context_duplication": make_plan(
                fact_overrides={"shared_context_size": HEAVY_CONTEXT_SIZE}
            ),
            "no_real_subagents": make_plan(capability=snapshot(subagents=False)),
        }
        for label, plan in covered.items():
            assert plan.mode is ExecutionMode.STAGED_SINGLE_AGENT, label
            assert plan.branches == (), label


# ==========================================================================
# TEST-PLAN-005 —— 七种不分流条件
# ==========================================================================


class TestPlan005NoSplitConditions:
    """**TEST-PLAN-005 = COMPLETE**（5B.2 起；见 `README_step5b.md` §5）。

    L29 的七种"不分流条件"是**系统级语义**，归属见 architecture Amendment A2：

        用户关闭        → [01] SessionGate / entrypoint（Router 外强制，Step 4）
        更高规则限制    → [02] Policy 解析约束 → [05] 执行 POLICY_NO_SPLIT（A3）
        ──────────────────────────────────────────────
        简单任务 / 强依赖 / 收益不足 / 容量不足 / 能力缺失 → **[05] Planner**

    本类只覆盖 **[05] 侧的五种**；另两种的承载是 [01] / [02]。
    三段通路现在都已接通，端到端由
    ``TestPolicyPlanningSeam.test_real_policy_restriction_yields_zero_branches``
    用**真实** ``Policy.resolve(...).planning`` 喂**真实** ``Planner`` 证明。
    """

    def test_l29_conditions_owned_by_the_planner_are_exactly_five(self) -> None:
        """[05] 拥有的 L29 条件恰好五种。"""
        owned = NoSplitReason.l29_conditions()
        assert len(owned) == 5
        assert {r.name for r in owned} == {
            "SIMPLE_TASK",
            "STRONG_DEPENDENCY",
            "INSUFFICIENT_BENEFIT",
            "INSUFFICIENT_CAPACITY",
            "MISSING_CAPABILITY",
        }

    def test_planner_does_not_judge_user_disabled(self) -> None:
        """**用户关闭不得由 Planner 判断** —— 那会破坏已冻结的 INV-S1。

        Step 4 已把"用户关闭"强制为 **Router 外** 的 BYPASS。
        """
        names = {r.name for r in NoSplitReason}
        for forbidden in (
            "USER_DISABLED",
            "PROTOCOL_DISABLED",
            "USER_CLOSED",
            "HIGHER_RULE_LIMIT",
            "POLICY_BLOCKED",
        ):
            assert forbidden not in names, forbidden

    def test_no_unreachable_blocker_is_declared(self) -> None:
        """``PlannedSubtask`` 已禁止空 completion criteria → 不设不可达的 blocker。"""
        assert "UNSTABLE_COMPLETION_CRITERIA" not in {r.name for r in NoSplitReason}

    def test_empty_completion_criteria_is_rejected_at_construction(self) -> None:
        """证明上面那条不是漏掉，而是**真的不可达**。"""
        with pytest.raises(ValueError):
            PlannedSubtask(goal="A", completion_criteria="")

    def test_internal_blockers_are_marked_as_such(self) -> None:
        """内部 blocker 不得冒充 L29 条件。"""
        for reason in (
            NoSplitReason.CONTEXT_DUPLICATION,
            NoSplitReason.TOO_FEW_SUBTASKS,
        ):
            assert reason.is_l29_condition is False, reason
        for reason in NoSplitReason.l29_conditions():
            assert reason.is_l29_condition is True, reason

    def test_simple_task_is_reported(self) -> None:
        fact_set = facts(
            affected_modules=1,
            mechanical=True,
            touches_cross_module_contracts=False,
            requires_iterative_debugging=False,
            subtasks=(),
        )
        easy = Evaluator().evaluate(
            fact_set, capability=snapshot(), project=project()
        )
        reasons = Planner().no_split_reasons(easy, fact_set, capability=snapshot())
        assert NoSplitReason.SIMPLE_TASK in reasons

    def test_strong_dependency_is_reported(self) -> None:
        fact_set = facts(has_sequential_dependency=True)
        evaluation = Evaluator().evaluate(
            fact_set, capability=snapshot(), project=project()
        )
        reasons = Planner().no_split_reasons(evaluation, fact_set, capability=snapshot())
        assert NoSplitReason.STRONG_DEPENDENCY in reasons

    def test_insufficient_benefit_is_reported(self) -> None:
        fact_set = facts(branch_setup_cost=Ordinal.HIGH)
        evaluation = Evaluator().evaluate(
            fact_set, capability=snapshot(), project=project()
        )
        reasons = Planner().no_split_reasons(evaluation, fact_set, capability=snapshot())
        assert NoSplitReason.INSUFFICIENT_BENEFIT in reasons

    def test_insufficient_capacity_is_reported(self) -> None:
        fact_set = facts()
        cap = snapshot(concurrency_limit=1)
        evaluation = Evaluator().evaluate(fact_set, capability=cap, project=project())
        assert NoSplitReason.INSUFFICIENT_CAPACITY in Planner().no_split_reasons(
            evaluation, fact_set, capability=cap
        )

    def test_missing_capability_is_reported(self) -> None:
        fact_set = facts()
        cap = snapshot(subagents=False)
        evaluation = Evaluator().evaluate(fact_set, capability=cap, project=project())
        assert NoSplitReason.MISSING_CAPABILITY in Planner().no_split_reasons(
            evaluation, fact_set, capability=cap
        )

    def test_too_few_subtasks_is_reported(self) -> None:
        for count in (0, 1):
            fact_set = facts(subtasks=tuple(subtask(f"S{i}") for i in range(count)))
            evaluation = Evaluator().evaluate(
                fact_set, capability=snapshot(), project=project()
            )
            assert NoSplitReason.TOO_FEW_SUBTASKS in Planner().no_split_reasons(
                evaluation, fact_set, capability=snapshot()
            )

    def test_a_clean_split_task_has_no_blockers(self) -> None:
        fact_set = facts()
        evaluation = Evaluator().evaluate(
            fact_set, capability=snapshot(), project=project()
        )
        assert Planner().no_split_reasons(
            evaluation, fact_set, capability=snapshot()
        ) == ()

    def test_reasons_are_recorded_in_the_plan_reason(self) -> None:
        plan = make_plan(fact_overrides={"has_sequential_dependency": True})
        assert "strong_dependency" in plan.reason

    def test_planner_never_branches_when_blocked(self) -> None:
        """**核心**：命中任一不分流条件时绝不建 branch。"""
        blocked_cases = [
            ({"has_sequential_dependency": True}, None),
            ({"subtasks": ()}, None),
            ({"branch_setup_cost": Ordinal.HIGH}, None),
            ({}, snapshot(subagents=False)),
            ({}, snapshot(concurrency_limit=1)),
        ]
        for overrides, cap in blocked_cases:
            plan = make_plan(fact_overrides=overrides, capability=cap)
            assert plan.branches == (), overrides
            assert plan.mode in (
                ExecutionMode.DIRECT,
                ExecutionMode.STAGED_SINGLE_AGENT,
            )

    def test_unknown_capability_is_not_treated_as_missing(self) -> None:
        """未知 ≠ 不支持：不得因为 Snapshot 未知就强制降级。"""
        cap = snapshot(
            subagents=None,
            concurrency_limit=None,
            unavailable_reason=UnavailableReason.TOOL_FAILURE,
        )
        plan = make_plan(capability=cap)
        assert plan.mode is ExecutionMode.MULTI_AGENT


# ==========================================================================
# TEST-PLAN-004（deferred）—— 本批只保证"计划事实"齐备
# ==========================================================================


class TestPlanFactsAreProduced:
    """TEST-PLAN-004 **本批 deferred**。

    L27 的六项（分支目标 / 计划模型 / 计划推理强度 / 读写属性 / 主线职责 / 一句理由）
    跨越 `[05]` 与 `[06]` 两个时点，而"**启动前在主线可见**"还要等
    Reporting / Router 主线接起来。本批只保证 Planner 侧的四项事实齐备。
    """

    def test_planner_produces_goal_and_read_write_and_main_duty_and_reason(self) -> None:
        plan = make_plan()
        for branch in plan.branches:
            assert branch.goal
            assert isinstance(branch.read_only, bool)
        assert plan.main_thread_duty
        assert plan.reason

    def test_planner_does_not_produce_model_tier(self) -> None:
        """计划模型 / 推理强度属 [06] —— Planner 不得越界。"""
        plan = make_plan()
        for branch in plan.branches:
            assert not hasattr(branch, "tier")
            assert not hasattr(branch, "planned_model_tier")
            assert not hasattr(branch, "effort")
            assert not hasattr(branch, "planned_reasoning_effort")

    def test_planner_module_does_not_import_model_selection(self) -> None:
        """[06] 的产物不得被 Planner 引入（docstring 里的提及不算）。"""
        assert "ModelSelection" not in _imported_names()
        assert "ModelTier" not in _imported_names()
        assert "EffortLevel" not in _imported_names()
        assert "model_router" not in _imported_modules()

    def test_planner_module_does_not_reference_model_selection_identifiers(
        self,
    ) -> None:
        names = _identifier_names() | _attribute_names()
        for forbidden in ("ModelSelection", "ModelTier", "EffortLevel"):
            assert forbidden not in names, forbidden

    def test_visibility_is_deferred_not_claimed(self) -> None:
        """本批**不**宣称"启动前六项在主线可见"。"""
        plan = make_plan()
        assert not hasattr(plan, "mainline_announcement")
        assert not hasattr(plan, "user_visible")


# ==========================================================================
# 边界：branch_id 注入 / 不含主线程身份
# ==========================================================================


class TestPlannerIdentityBoundaries:
    def test_branch_id_factory_is_injectable_for_stable_tests(self) -> None:
        counter = {"n": 0}

        def factory() -> str:
            counter["n"] += 1
            return f"br_test{counter['n']}"

        fact_set = facts()
        cap, proj = snapshot(), project()
        evaluation = Evaluator().evaluate(fact_set, capability=cap, project=proj)
        plan = Planner(branch_id_factory=factory).plan(
            evaluation, fact_set, capability=cap, project=proj, constraints=unrestricted()
        )
        assert [b.branch_id for b in plan.branches] == [
            "br_test1",
            "br_test2",
            "br_test3",
        ]

    def test_branch_ids_still_satisfy_the_frozen_prefix_rule(self) -> None:
        plan = make_plan()
        for branch in plan.branches:
            assert branch.branch_id.startswith("br_")

    def test_planner_does_not_assign_a_main_thread_branch_id(self) -> None:
        """主线程执行单位身份属后续 ``RoutingPlan`` 装配阶段。"""
        plan = make_plan()
        assert not hasattr(plan, "main_thread_branch_id")
        assert not hasattr(plan, "execution_unit_ids")
        # 只在**真实标识符**上断言 —— docstring 会提到该概念以说明边界
        assert "main_thread_branch_id" not in _identifier_names()
        assert "main_thread_branch_id" not in _attribute_names()

    def test_plan_carries_only_child_branches(self) -> None:
        plan = make_plan()
        assert len(plan.branches) == 3
        assert all(b.branch_id.startswith("br_") for b in plan.branches)


# ==========================================================================
# 边界：Planner 不做审批 / 成本 / 策略
# ==========================================================================


class TestPlannerDoesNotOverreach:
    def test_planner_does_not_touch_approval(self) -> None:
        assert not {
            "ApprovalJudgment",
            "ApprovalGate",
            "needs_approval",
        } & (_imported_names() | _identifier_names() | _attribute_names())

    def test_planner_does_not_touch_cost(self) -> None:
        """[15a] 在 [06] 之后，本批**不接线**。"""
        assert not {
            "CostEstimator",
            "CostEstimate",
            "CostLedger",
            "CostSummary",
        } & (_imported_names() | _identifier_names())
        assert not any(
            module.startswith("adaptive_router.cost") or module == ".cost"
            for module in _imported_modules()
        )

    def test_planner_does_not_import_risk(self) -> None:
        """风险由 [07] 独立产生，Planner 不消费也未接线。"""
        assert "risk" not in _imported_modules()
        assert "RiskAssessment" not in _imported_names()
        assert "RiskAssessment" not in _identifier_names()

    def test_planner_does_not_probe_capabilities(self) -> None:
        for forbidden in ("CapabilityProbe", "CapabilityRegistry"):
            assert forbidden not in _imported_names(), forbidden

    def test_plan_is_frozen(self) -> None:
        plan = make_plan()
        with pytest.raises(dataclasses.FrozenInstanceError):
            plan.mode = ExecutionMode.DIRECT  # type: ignore[misc]

    def test_evaluation_input_is_not_mutated(self) -> None:
        fact_set = facts()
        cap, proj = snapshot(), project()
        evaluation = Evaluator().evaluate(fact_set, capability=cap, project=proj)
        before = dataclasses.asdict(evaluation)
        Planner().plan(
            evaluation, fact_set, capability=cap, project=proj, constraints=unrestricted()
        )
        assert dataclasses.asdict(evaluation) == before


# ==========================================================================
# 输入校验
# ==========================================================================


class TestInputValidation:
    def test_planner_rejects_a_non_evaluation(self) -> None:
        with pytest.raises(TypeError):
            Planner().plan(  # type: ignore[arg-type]
                "nope",
                facts(),
                capability=snapshot(),
                project=project(),
                constraints=unrestricted(),
            )

    def test_planner_rejects_a_non_facts(self) -> None:
        cap, proj = snapshot(), project()
        evaluation = Evaluator().evaluate(facts(), capability=cap, project=proj)
        with pytest.raises(TypeError):
            Planner().plan(  # type: ignore[arg-type]
                evaluation,
                None,
                capability=cap,
                project=proj,
                constraints=unrestricted(),
            )

    def test_planner_rejects_a_non_constraints(self) -> None:
        cap, proj = snapshot(), project()
        evaluation = Evaluator().evaluate(facts(), capability=cap, project=proj)
        with pytest.raises(TypeError):
            Planner().plan(  # type: ignore[arg-type]
                evaluation, facts(), capability=cap, project=proj, constraints=None
            )

    def test_constraints_is_required_not_defaulted(self) -> None:
        """**禁止 fail-open**：漏传必须是 ``TypeError``，不得默认允许分流（A3 钉子 ③）。"""
        import inspect

        parameter = inspect.signature(Planner.plan).parameters["constraints"]
        assert parameter.default is inspect.Parameter.empty
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY

    def test_subtask_requires_goal_and_criteria(self) -> None:
        with pytest.raises(ValueError):
            PlannedSubtask(goal="", completion_criteria="done")
        with pytest.raises(ValueError):
            PlannedSubtask(goal="A", completion_criteria="  ")

    def test_task_facts_reject_unknown_fields(self) -> None:
        """输入集合封闭 —— 不得偷偷加成本 / 审批字段。"""
        with pytest.raises(TypeError):
            TaskFacts(needs_approval=True)  # type: ignore[call-arg]


# ==========================================================================
# TEST-PLAN-005 —— [02] Policy → [05] Planner 的真实通路（Amendment A3）
# ==========================================================================

#: 必然落到 DIRECT 的规划事实（简单 / 局部 / 机械明确）。
DIRECT_FACTS: dict[str, object] = {
    "affected_modules": 1,
    "mechanical": True,
    "touches_cross_module_contracts": False,
    "requires_iterative_debugging": False,
    "subtasks": (),
}


def make_stable_plan(
    *,
    fact_overrides: dict[str, object] | None = None,
    constraints: EffectivePlanningConstraints | None = None,
) -> Plan:
    """同上，但 ``branch_id`` 是**确定性**的 —— 两次独立运行可以逐字段比较。

    ``new_branch_id`` 每次生成随机 UUID，所以默认 ``make_plan`` 产出的两个
    ``MULTI_AGENT`` 计划**不可能**相等。要做"行为完全等价"的比较就必须固定它。
    """
    counter = {"n": 0}

    def factory() -> str:
        counter["n"] += 1
        return f"br_stable{counter['n']}"

    fact_set = facts(**(fact_overrides or {}))
    cap, proj = snapshot(), project()
    evaluation: Evaluation = Evaluator().evaluate(fact_set, capability=cap, project=proj)
    return Planner(branch_id_factory=factory).plan(
        evaluation,
        fact_set,
        capability=cap,
        project=proj,
        constraints=constraints if constraints is not None else unrestricted(),
    )


class TestPolicyPlanningSeam:
    """A3 三条钉子的行为落点。"""

    # -- 钉子 ①：POLICY_NO_SPLIT **不是** L29 条件 ------------------------

    def test_policy_no_split_is_not_an_l29_condition(self) -> None:
        assert NoSplitReason.POLICY_NO_SPLIT.is_l29_condition is False
        assert NoSplitReason.POLICY_NO_SPLIT not in NoSplitReason.l29_conditions()

    def test_l29_ownership_table_is_unchanged(self) -> None:
        """A3 钉子 ①：``[05]`` 自己拥有的 L29 条件**仍只有五种**。"""
        assert len(NoSplitReason.l29_conditions()) == 5

    # -- 钉子 ②：条件性回收 --------------------------------------------

    def test_multi_agent_is_recycled_to_staged_with_zero_branches(self) -> None:
        """otherwise-``MULTI_AGENT`` + 策略禁止分流 → ``STAGED_SINGLE_AGENT`` + 0 分支。"""
        baseline = make_plan()
        assert baseline.mode is ExecutionMode.MULTI_AGENT  # 前置：否则断言是空地的

        plan = make_plan(constraints=no_split())
        assert plan.mode is ExecutionMode.STAGED_SINGLE_AGENT
        assert plan.branches == ()
        assert plan.is_multi_agent is False

    def test_the_recycled_plan_reports_policy_no_split(self) -> None:
        plan = make_plan(constraints=no_split())
        assert NoSplitReason.POLICY_NO_SPLIT.value in plan.reason

    def test_direct_is_not_promoted_by_policy_no_split(self) -> None:
        """A3 ②：otherwise-``DIRECT`` **仍 DIRECT**。

        ⚠️ 这正是 A3 明确警告过的错误做法：只要 ``auto_split_allowed=False``
        就把 ``POLICY_NO_SPLIT`` 混进 L29 集合 —— 若它同时被算作
        ``staged_only``，本该 DIRECT 的简单任务会被**提升**成 STAGED，
        于是"更高规则限制"反而**增加**了执行复杂度。
        """
        plan = make_plan(fact_overrides=dict(DIRECT_FACTS), constraints=no_split())
        assert plan.mode is ExecutionMode.DIRECT
        assert plan.branches == ()
        # policy 没有插手：理由里不得出现 POLICY_NO_SPLIT
        assert NoSplitReason.POLICY_NO_SPLIT.value not in plan.reason

    def test_staged_stays_staged_under_policy_no_split(self) -> None:
        """A3 ②：otherwise-``STAGED`` 仍 STAGED（不因 policy 变成别的模式）。"""
        plan = make_plan(
            fact_overrides={"has_sequential_dependency": True}, constraints=no_split()
        )
        assert plan.mode is ExecutionMode.STAGED_SINGLE_AGENT
        assert plan.branches == ()
        assert NoSplitReason.POLICY_NO_SPLIT.value not in plan.reason

    def test_unrestricted_policy_is_behaviourally_identical_to_5a(self) -> None:
        """A3 ②：``auto_split_allowed=True`` 必须与 5A 冻结行为**完全等价**。

        用确定性 branch_id 做**逐字段**比较（含 branches），而不是只比 mode。
        """
        cases = (
            {},
            dict(DIRECT_FACTS),
            {"has_sequential_dependency": True},
            {"shared_context_size": HEAVY_CONTEXT_SIZE},
            {"subtasks": (subtask("A"), subtask("B"))},
            {"branch_setup_cost": Ordinal.HIGH},
        )
        for overrides in cases:
            baseline = make_stable_plan(fact_overrides=overrides)
            explicit = make_stable_plan(
                fact_overrides=overrides, constraints=unrestricted()
            )
            assert baseline == explicit, overrides
            assert baseline.branches == explicit.branches, overrides

    def test_the_equivalence_case_is_not_vacuous(self) -> None:
        """上一条必须真的覆盖到 MULTI_AGENT —— 否则"等价"是空地的。"""
        modes = {
            make_stable_plan(fact_overrides=overrides).mode
            for overrides in ({}, dict(DIRECT_FACTS))
        }
        assert modes == {ExecutionMode.MULTI_AGENT, ExecutionMode.DIRECT}

    def test_unrestricted_policy_never_mentions_policy_no_split(self) -> None:
        assert NoSplitReason.POLICY_NO_SPLIT.value not in make_plan().reason

    # -- 真实通路：真实 Policy.resolve(...).planning → 真实 Planner --------

    def test_real_policy_restriction_yields_zero_branches(self) -> None:
        """**TEST-PLAN-005 的完成通路**（不是用假 constraints 顶替）。

            Policy restriction → EffectivePlanningConstraints → [05] Planner
            → 0 child branches

        ``[02]`` 侧走真实的 `Policy.resolve(...)`；``[05]`` 侧走真实的 `Planner`。
        """
        from adaptive_router.policy import Policy, PolicyFacts

        resolved = Policy().resolve(
            PolicyFacts(
                session_authorized=True,
                system_denied=("auto_split",),  # 更高规则：不得自动分流
                developer_denied=(),
                safety_denied=(),
                platform_denied=(),
                user_denied=(),
                project_denied=(),
                user_granted_scopes=(),
                user_commands=(),
                user_read_only_override=False,
                project_model_preference=None,
                project_approval_preference=None,
                protocol_default_mode="observe",
            )
        )
        assert resolved.planning.auto_split_allowed is False  # 前置事实

        plan = make_plan(constraints=resolved.planning)
        assert plan.mode is ExecutionMode.STAGED_SINGLE_AGENT
        assert plan.branches == ()
        assert NoSplitReason.POLICY_NO_SPLIT.value in plan.reason

    def test_real_policy_allowance_still_splits(self) -> None:
        """对照：同一份真实 Policy 在**没有**限制时仍应分流（否则上面是假通路）。"""
        from adaptive_router.policy import Policy, PolicyFacts

        resolved = Policy().resolve(
            PolicyFacts(
                session_authorized=True,
                system_denied=(),
                developer_denied=(),
                safety_denied=(),
                platform_denied=(),
                user_denied=(),
                project_denied=(),
                user_granted_scopes=(),
                user_commands=(),
                user_read_only_override=False,
                project_model_preference=None,
                project_approval_preference=None,
                protocol_default_mode="observe",
            )
        )
        assert resolved.planning.auto_split_allowed is True
        plan = make_plan(constraints=resolved.planning)
        assert plan.mode is ExecutionMode.MULTI_AGENT
        assert len(plan.branches) >= SPLIT_MIN_SUBTASKS

    # -- seam 归属：中性 DTO，不产生 planner → policy 反向依赖 --------------

    def test_dto_is_one_object_across_both_nodes(self) -> None:
        """``policy`` 只是 re-export；两个节点拿到的是**同一个类对象**。"""
        from adaptive_router import planning_constraints as pc
        from adaptive_router import policy as policy_mod

        assert (
            policy_mod.EffectivePlanningConstraints
            is pc.EffectivePlanningConstraints
            is EffectivePlanningConstraints
        )

    def test_planner_does_not_import_policy(self) -> None:
        """禁止形成 ``planner → policy`` 这种与数据流 ``[02] → [05]`` 相反的实现依赖。"""
        assert "policy" not in _imported_modules()
        assert "planning_constraints" in _imported_modules()
