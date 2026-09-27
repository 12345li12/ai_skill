"""contracts.decision 契约测试。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-EVAL-001     低成本评估 11 个维度              → TestEvaluationDimensions
  TEST-EVAL-002     先做低成本判断                    → TestEvaluationDimensions
  TEST-PLAN-001     简单/局部/不划算 → 直接执行，无形式化计划
  TEST-PLAN-002     强依赖/上下文复制多 → 单代理阶段化
  TEST-PLAN-003     ≥2 独立子任务 + 并行收益 > 成本 → 真实多代理分流
  TEST-PLAN-004     启动前列出分支目标/读写属性/主线职责
  TEST-PLAN-005     不分流条件
  TEST-MODEL-002    升级触发（失败/冲突/风险上升）
  TEST-RISK-001     高风险场景在风险动作前暂停         → TestRiskAssessment
  TEST-RISK-003     审批只列 4 项                      → TestApprovalJudgment
  TEST-RECOVERY-002 恢复后不得无限递归                → TestRecoveryDecisionLimit
  TEST-EVIDENCE-003 不得把计划值当成实际值             → TestLifecycleSeparation

第 2 步审查后新增的边界测试：
  § 生命周期分离（Evaluation → Plan → Risk → ModelSelection → RoutingPlan）
  § INV-05：ModelSelection 不得携带平台细节
"""

from __future__ import annotations

import dataclasses

import pytest

from adaptive_router.contracts.common import (
    Confidence,
    ContractError,
    new_branch_id,
    new_run_id,
)
from adaptive_router.contracts.decision import (
    ApprovalJudgment,
    BranchSpec,
    EffortLevel,
    Evaluation,
    ExecutionMode,
    FailureClass,
    ImpactScope,
    ModelSelection,
    ModelTier,
    Ordinal,
    Plan,
    RecoveryDecision,
    RecoveryTarget,
    RiskAssessment,
    RiskCategory,
    RiskLevel,
    RoutingPlan,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_evaluation() -> Evaluation:
    return Evaluation(
        scale=Ordinal.LOW,
        complexity=Ordinal.LOW,
        risk=Ordinal.LOW,
        impact_scope=ImpactScope.LOCAL,
        independence=Ordinal.HIGH,
        parallel_benefit=Ordinal.LOW,
        context_volume=Ordinal.LOW,
        scheduling_cost=Ordinal.LOW,
        rework_probability=Ordinal.LOW,
        review_value=Ordinal.LOW,
        concurrency_capacity=Ordinal.LOW,
        reason="low-cost assessment",
    )


def make_model(
    tier: ModelTier = ModelTier.BALANCED, effort: EffortLevel = EffortLevel.MEDIUM
) -> ModelSelection:
    """[06] 的输出：**只有抽象计划，没有平台 model ID**。"""
    return ModelSelection(tier=tier, effort=effort, reason="balanced is enough")


def make_branch(goal: str = "inspect module A") -> BranchSpec:
    """[05] 的输出：分支形状，**不含模型档位**。"""
    return BranchSpec(
        branch_id=new_branch_id(),
        goal=goal,
        read_only=True,
        completion_criteria="report findings with evidence",
    )


def make_plan(mode: ExecutionMode = ExecutionMode.MULTI_AGENT) -> Plan:
    if mode is ExecutionMode.MULTI_AGENT:
        return Plan(
            mode=mode,
            main_thread_duty="coordinate",
            reason="parallel benefit",
            branches=(make_branch("A"), make_branch("B")),
        )
    return Plan(mode=mode, main_thread_duty="do it", reason="simple and local")


def make_routing_plan(plan: Plan | None = None) -> RoutingPlan:
    plan = plan or make_plan()
    return RoutingPlan(
        run_id=new_run_id(),
        plan=plan,
        main_thread_branch_id=new_branch_id(),
        main_thread_model=make_model(),
        branch_models=tuple(
            (spec.branch_id, make_model()) for spec in plan.branches
        ),
    )


# --------------------------------------------------------------------------
# TEST-EVAL-001 / 002
# --------------------------------------------------------------------------


class TestEvaluationDimensions:
    def test_exactly_eleven_dimensions(self) -> None:
        """L23 的 11 个维度，不得增删。"""
        assert len(make_evaluation().dimensions) == 11

    def test_dimension_names_match_protocol_order(self) -> None:
        expected = [
            "scale",
            "complexity",
            "risk",
            "impact_scope",
            "independence",
            "parallel_benefit",
            "context_volume",
            "scheduling_cost",
            "rework_probability",
            "review_value",
            "concurrency_capacity",
        ]
        assert [f.name for f in dataclasses.fields(Evaluation)][:11] == expected

    def test_evaluation_does_not_decide_execution_mode(self) -> None:
        """第 2 步审查裁决 ②：执行方式是 [05] Planner 的职责。

        若 Evaluation 也存 mode，Planner 只剩"重复确认"，决策职责被架空。
        """
        field_names = {f.name for f in dataclasses.fields(Evaluation)}
        assert "mode" not in field_names
        assert "execution_mode" not in field_names
        assert field_names == set(
            [f.name for f in dataclasses.fields(Evaluation)][:11]
        ) | {"reason"}

    def test_reason_is_kept_as_assessment_summary(self) -> None:
        """裁决允许保留 reason 作为评估摘要。"""
        assert "reason" in {f.name for f in dataclasses.fields(Evaluation)}

    def test_rejects_non_ordinal_dimension(self) -> None:
        with pytest.raises(ContractError, match="scale"):
            Evaluation(
                scale="low",  # type: ignore[arg-type]
                complexity=Ordinal.LOW,
                risk=Ordinal.LOW,
                impact_scope=ImpactScope.LOCAL,
                independence=Ordinal.HIGH,
                parallel_benefit=Ordinal.LOW,
                context_volume=Ordinal.LOW,
                scheduling_cost=Ordinal.LOW,
                rework_probability=Ordinal.LOW,
                review_value=Ordinal.LOW,
                concurrency_capacity=Ordinal.LOW,
                reason="x",
            )

    def test_reason_is_mandatory(self) -> None:
        with pytest.raises(ContractError, match="reason"):
            Evaluation(
                scale=Ordinal.LOW,
                complexity=Ordinal.LOW,
                risk=Ordinal.LOW,
                impact_scope=ImpactScope.LOCAL,
                independence=Ordinal.HIGH,
                parallel_benefit=Ordinal.LOW,
                context_volume=Ordinal.LOW,
                scheduling_cost=Ordinal.LOW,
                rework_probability=Ordinal.LOW,
                review_value=Ordinal.LOW,
                concurrency_capacity=Ordinal.LOW,
                reason="  ",
            )

    def test_is_frozen(self) -> None:
        evaluation = make_evaluation()
        with pytest.raises(dataclasses.FrozenInstanceError):
            evaluation.risk = Ordinal.HIGH  # type: ignore[misc]


# --------------------------------------------------------------------------
# TEST-PLAN-001 / 002 / 003 / 004 / 005
# --------------------------------------------------------------------------


class TestPlanShape:
    def test_direct_execution_carries_no_branches(self) -> None:
        """TEST-PLAN-001：简单/局部 → 直接执行，不建分支。"""
        plan = make_plan(ExecutionMode.DIRECT)
        assert plan.branches == ()
        assert plan.is_multi_agent is False

    def test_staged_single_agent_carries_no_branches(self) -> None:
        """TEST-PLAN-002：无真实代理时阶段化，**不得模拟分支**（L17）。"""
        plan = make_plan(ExecutionMode.STAGED_SINGLE_AGENT)
        assert plan.branches == ()

    def test_multi_agent_requires_at_least_two_branches(self) -> None:
        """TEST-PLAN-003。"""
        with pytest.raises(ContractError, match="至少需要 2 个"):
            Plan(
                mode=ExecutionMode.MULTI_AGENT,
                main_thread_duty="coordinate",
                reason="parallel benefit",
                branches=(make_branch(),),
            )

    def test_multi_agent_with_two_branches_is_legal(self) -> None:
        plan = make_plan()
        assert plan.is_multi_agent is True
        assert len(plan.branches) == 2

    def test_direct_mode_must_not_smuggle_branches(self) -> None:
        with pytest.raises(ContractError, match="不得携带分支"):
            Plan(
                mode=ExecutionMode.DIRECT,
                main_thread_duty="coordinate",
                reason="x",
                branches=(make_branch("A"), make_branch("B")),
            )

    def test_duplicate_branch_ids_are_rejected(self) -> None:
        branch = make_branch("A")
        with pytest.raises(ContractError, match="不得重复"):
            Plan(
                mode=ExecutionMode.MULTI_AGENT,
                main_thread_duty="coordinate",
                reason="x",
                branches=(branch, branch),
            )

    def test_branches_must_be_an_immutable_tuple(self) -> None:
        with pytest.raises(ContractError, match="必须是 tuple"):
            Plan(
                mode=ExecutionMode.MULTI_AGENT,
                main_thread_duty="coordinate",
                reason="x",
                branches=[make_branch("A"), make_branch("B")],  # type: ignore[arg-type]
            )

    def test_branch_spec_requires_completion_criteria(self) -> None:
        """L27：每个分支必须有完成标准，否则无法 [13] Validation。"""
        with pytest.raises(ContractError, match="completion_criteria"):
            BranchSpec(
                branch_id=new_branch_id(),
                goal="A",
                read_only=True,
                completion_criteria="",
            )

    def test_branch_spec_does_not_choose_a_model(self) -> None:
        """第 2 步审查裁决（阻塞 A）：[05] 在 [06] 之前，不得提前选模型。"""
        field_names = {f.name for f in dataclasses.fields(BranchSpec)}
        assert "planned_model_tier" not in field_names
        assert "planned_reasoning_effort" not in field_names
        assert "model" not in field_names
        assert field_names == {
            "branch_id",
            "goal",
            "read_only",
            "completion_criteria",
        }

    def test_plan_carries_main_thread_duty_and_reason(self) -> None:
        """TEST-PLAN-004：主线职责与一句理由由 Planner 负责。"""
        plan = make_plan()
        assert plan.main_thread_duty
        assert plan.reason


# --------------------------------------------------------------------------
# 生命周期分离（第 2 步审查阻塞 A）
# --------------------------------------------------------------------------


class TestLifecycleSeparation:
    """Evaluation → Plan → Risk → ModelSelection → RoutingPlan 的职责边界。"""

    def test_model_selection_is_abstract_only(self) -> None:
        """[06] 只输出抽象档位 + 强度 + 理由。**不含平台细节。**"""
        field_names = {f.name for f in dataclasses.fields(ModelSelection)}
        assert field_names == {"tier", "effort", "reason"}
        for forbidden in ("model_id", "confirmation", "requested", "actual"):
            assert forbidden not in field_names

    def test_model_selection_has_no_platform_model_identifier(self) -> None:
        """Core 禁止出现平台细节：provider model ID 属于 [11] Adapter。"""
        selection = make_model()
        assert not hasattr(selection, "model_id")
        assert not hasattr(selection, "actual_model_id")

    def test_three_value_confirmation_lives_in_branch_return(self) -> None:
        """planned/requested/actual 三值在 [12] BranchReturn 才同时存在。"""
        from adaptive_router.contracts.runtime import BranchReturn

        assert "model" in BranchReturn.__dataclass_fields__
        assert "effort" in BranchReturn.__dataclass_fields__

    def test_routing_plan_pairs_planner_branches_with_router_tiers(self) -> None:
        """RoutingPlan 是两个时点的汇合点：分支形状 + 档位。"""
        plan = make_plan()
        routing = make_routing_plan(plan)
        assert routing.branch_ids == tuple(b.branch_id for b in plan.branches)
        for branch_id in routing.branch_ids:
            assert isinstance(routing.model_for_branch(branch_id), ModelSelection)

    def test_routing_plan_rejects_invented_branches(self) -> None:
        """[06] 不得凭空发明 Plan 里没有的分支。"""
        plan = make_plan()
        with pytest.raises(ContractError, match="Plan 未定义的分支"):
            RoutingPlan(
                run_id=new_run_id(),
                plan=plan,
                main_thread_branch_id=new_branch_id(),
                main_thread_model=make_model(),
                branch_models=((new_branch_id(), make_model()),),  # ← 不在 plan 里
            )

    def test_routing_plan_requires_a_tier_for_every_branch(self) -> None:
        plan = make_plan()
        with pytest.raises(ContractError, match="缺少档位选择"):
            RoutingPlan(
                run_id=new_run_id(),
                plan=plan,
                main_thread_branch_id=new_branch_id(),
                main_thread_model=make_model(),
                branch_models=(),
            )

    def test_routing_plan_rejects_duplicate_branch_models(self) -> None:
        plan = make_plan()
        first = plan.branches[0].branch_id
        second = plan.branches[1].branch_id
        with pytest.raises(ContractError, match="branch_id 不得重复"):
            RoutingPlan(
                run_id=new_run_id(),
                plan=plan,
                main_thread_branch_id=new_branch_id(),
                main_thread_model=make_model(),
                branch_models=(
                    (first, make_model()),
                    (first, make_model()),
                    (second, make_model()),
                ),
            )

    # ----------------------------------------------------------------------
    # 阻塞 C：主线程的 cost-unit 身份
    # ----------------------------------------------------------------------

    def test_main_thread_has_its_own_execution_unit_identity(self) -> None:
        """DIRECT / STAGED 下主线程是唯一执行单位，Ledger 必须有合法 branch_id。"""
        for mode in (ExecutionMode.DIRECT, ExecutionMode.STAGED_SINGLE_AGENT):
            plan = make_plan(mode)
            routing = make_routing_plan(plan)
            assert plan.branches == ()
            assert routing.execution_unit_ids == (routing.main_thread_branch_id,)
            assert len(routing.execution_unit_ids) == 1

    def test_multi_agent_run_has_main_thread_plus_each_branch(self) -> None:
        plan = make_plan(ExecutionMode.MULTI_AGENT)
        routing = make_routing_plan(plan)
        units = routing.execution_unit_ids
        assert len(units) == 1 + len(plan.branches)
        assert units[0] == routing.main_thread_branch_id
        assert set(units[1:]) == {b.branch_id for b in plan.branches}
        assert len(set(units)) == len(units)  # 身份唯一

    def test_child_branch_must_not_take_the_main_thread_identity(self) -> None:
        """执行单位身份必须唯一，child branch 不得占用主线程 ID。"""
        plan = make_plan(ExecutionMode.MULTI_AGENT)
        main_id = plan.branches[0].branch_id  # 让主线程占用一个 child 的 ID
        with pytest.raises(ContractError, match="不得被 child branch 占用"):
            RoutingPlan(
                run_id=new_run_id(),
                plan=plan,
                main_thread_branch_id=main_id,
                main_thread_model=make_model(),
                branch_models=tuple(
                    (spec.branch_id, make_model()) for spec in plan.branches
                ),
            )

    def test_model_for_branch_resolves_the_main_thread_too(self) -> None:
        routing = make_routing_plan()
        assert (
            routing.model_for_branch(routing.main_thread_branch_id)
            is routing.main_thread_model
        )

    def test_branch_models_reject_non_branch_identifiers(self) -> None:
        """BranchId 底层是 str，真正阻止误用的是前缀校验（不是静态类型）。"""
        plan = make_plan()
        with pytest.raises(ContractError, match="BranchId"):
            RoutingPlan(
                run_id=new_run_id(),
                plan=plan,
                main_thread_branch_id=new_branch_id(),
                main_thread_model=make_model(),
                branch_models=(
                    ("balanced", make_model()),  # type: ignore[arg-type]
                    (plan.branches[1].branch_id, make_model()),
                ),
            )

    def test_main_thread_branch_id_must_be_a_valid_branch_id(self) -> None:
        plan = make_plan()
        with pytest.raises(ContractError, match="BranchId"):
            RoutingPlan(
                run_id=new_run_id(),
                plan=plan,
                main_thread_branch_id="main-thread",  # type: ignore[arg-type]
                main_thread_model=make_model(),
            )

    # ----------------------------------------------------------------------
    # 阻塞 D：TEST-PLAN-004 的跨契约组合断言
    # ----------------------------------------------------------------------

    def test_routing_plan_exposes_all_six_prelaunch_facts(self) -> None:
        """TEST-PLAN-004（contract 层）：L27 的六项数据**全部可表达**。

        这六项跨 `[05]` 与 `[06]` 两个时间点，因此**不在同一个 dataclass 里**。
        本测试只证明"数据具备"，"启动前真的展示出来"属第 3 步行为测试。
        """
        plan = make_plan(ExecutionMode.MULTI_AGENT)
        routing = make_routing_plan(plan)

        for spec in plan.branches:
            selection = routing.model_for_branch(spec.branch_id)
            # 1. 分支目标
            assert spec.goal
            # 2. 计划模型（抽象档位）
            assert isinstance(selection.tier, ModelTier)
            # 3. 计划推理强度
            assert isinstance(selection.effort, EffortLevel)
            # 4. 读写属性
            assert isinstance(spec.read_only, bool)

        # 5. 主线职责
        assert plan.main_thread_duty
        # 6. 一句理由
        assert plan.reason
        assert routing.main_thread_model.reason

    def test_the_six_facts_span_exactly_two_nodes(self) -> None:
        """分散是正确的：Planner 定形状，Model Router 定档位。"""
        branch_fields = set(BranchSpec.__dataclass_fields__)
        selection_fields = set(ModelSelection.__dataclass_fields__)
        plan_fields = set(Plan.__dataclass_fields__)

        assert {"goal", "read_only"} <= branch_fields
        assert {"tier", "effort", "reason"} <= selection_fields
        assert {"main_thread_duty", "reason"} <= plan_fields
        # Planner 侧不出现档位
        assert "planned_model_tier" not in branch_fields
        assert "tier" not in branch_fields
        # Model Router 侧不出现任务形状
        assert "goal" not in selection_fields


# --------------------------------------------------------------------------
# TEST-RISK-001
# --------------------------------------------------------------------------


class TestRiskAssessment:
    def test_assessment_does_not_carry_any_approval_control(self) -> None:
        """第 2 步审查裁决 ④：Risk 只回答"风险有多大"。

        一个"是否需要考虑审批"的 bool 极易被写成
        ``if not risk.requires_approval_consideration: skip_approval_judgment()``
        —— 那会重新制造审批旁路。
        """
        field_names = {f.name for f in dataclasses.fields(RiskAssessment)}
        for forbidden in (
            "requires_approval_consideration",
            "needs_approval",
            "approved",
            "requires_approval",
        ):
            assert forbidden not in field_names
        assert field_names == {"level", "categories", "detail"}

    def test_no_preemptive_cost_risk_category(self) -> None:
        """裁决 ④：Risk 在 Cost Estimator **之前**，不得前置成本风险。

        "显著额外费用"由 ``[15a] CostEstimate → ApprovalJudgment`` 判断。
        """
        assert not hasattr(RiskCategory, "SIGNIFICANT_ADDITIONAL_COST")
        assert "SIGNIFICANT_ADDITIONAL_COST" not in {m.name for m in RiskCategory}

    def test_risk_categories_match_protocol_scenarios(self) -> None:
        """L41/L43 的场景类别，去掉前置成本风险后为 10 项。"""
        assert len(RiskCategory) == 10
        assert {m.name for m in RiskCategory} == {
            "BROAD_HARD_TO_REVIEW_CHANGE",
            "HARD_TO_REVERT_OPERATION",
            "PRODUCTION_OR_REAL_USERS",
            "SHARED_INFRASTRUCTURE",
            "RELEASE_DEPLOY_MERGE_PAYMENT",
            "OUTBOUND_MESSAGE",
            "EXTERNAL_WRITE",
            "CREDENTIALS_PRIVACY_PERMISSIONS",
            "MAJOR_CONCLUSION_CONFLICT",
            "SCOPE_EXPANSION",
        }

    def test_high_risk_is_representable(self) -> None:
        assessment = RiskAssessment(
            level=RiskLevel.HIGH,
            categories=(RiskCategory.PRODUCTION_OR_REAL_USERS,),
            detail="touches production",
        )
        assert assessment.level is RiskLevel.HIGH

    def test_low_risk_with_categories_is_still_rejected(self) -> None:
        """类别本身就是风险证据，不得同时声明 LOW。"""
        with pytest.raises(ContractError, match="不得为 LOW"):
            RiskAssessment(
                level=RiskLevel.LOW,
                categories=(RiskCategory.EXTERNAL_WRITE,),
                detail="writes externally",
            )

    def test_categories_must_be_an_immutable_tuple(self) -> None:
        with pytest.raises(ContractError, match="必须是 tuple"):
            RiskAssessment(
                level=RiskLevel.HIGH,
                categories=[RiskCategory.EXTERNAL_WRITE],  # type: ignore[arg-type]
                detail="x",
            )


# --------------------------------------------------------------------------
# TEST-RISK-003
# --------------------------------------------------------------------------


class TestApprovalJudgment:
    def test_checklist_has_exactly_four_items(self) -> None:
        """L43：审批只列拟执行动作/影响范围/主要风险/恢复方式。"""
        judgment = ApprovalJudgment(
            needs_approval=True,
            proposed_action="delete remote branch",
            impact_scope="shared repository",
            main_risk="irreversible",
            recovery_method="restore from backup tag",
        )
        assert len(judgment.checklist) == 4
        assert judgment.checklist == (
            "delete remote branch",
            "shared repository",
            "irreversible",
            "restore from backup tag",
        )

    def test_checklist_fields_are_mandatory_even_when_not_needed(self) -> None:
        with pytest.raises(ContractError, match="recovery_method"):
            ApprovalJudgment(
                needs_approval=False,
                proposed_action="read file",
                impact_scope="none",
                main_risk="none",
                recovery_method="",
            )

    def test_judgment_is_not_the_gate(self) -> None:
        """架构 4.2：Judgment 是系统决策，Gate 是阻塞式人类边界。"""
        field_names = {f.name for f in dataclasses.fields(ApprovalJudgment)}
        assert "human_input" not in field_names
        assert "suspended" not in field_names

    def test_judgment_runs_after_cost_so_it_needs_no_risk_flag(self) -> None:
        """Design note：Judgment 在 Cost Estimator 之后，因此必然会运行。

        即使 Risk 是 LOW，成本仍可能显著 —— 这就是为什么 Risk 侧
        不需要（也不应该有）任何"是否需要审批"的标志。
        """
        low_risk = RiskAssessment(
            level=RiskLevel.LOW, categories=(), detail="nothing risky"
        )
        assert not hasattr(low_risk, "requires_approval_consideration")
        # 成本显著时仍可产生 needs_approval=True 的判断
        judgment = ApprovalJudgment(
            needs_approval=True,
            proposed_action="run maximum tier on 8 branches",
            impact_scope="significant additional cost",
            main_risk="budget overrun",
            recovery_method="abort remaining branches",
        )
        assert judgment.needs_approval is True


# --------------------------------------------------------------------------
# TEST-RECOVERY-002
# --------------------------------------------------------------------------


class TestRecoveryDecisionLimit:
    def test_attempt_beyond_limit_must_not_produce_a_return_edge(self) -> None:
        """架构 4.4 / 9.1：attempt 2 失败 → TERMINAL_FAILURE，禁止静默继续。"""
        with pytest.raises(ContractError, match="已达上限"):
            RecoveryDecision(
                failure_class=FailureClass.MODEL,
                route_to=RecoveryTarget.MODEL_ROUTER,
                attempt=2,
                total_attempt_limit=2,
                carry_forward=("facts",),
                minimal_correction="raise tier",
                confidence=Confidence.MEDIUM,
            )

    def test_first_attempt_recovery_is_legal(self) -> None:
        """TEST-MODEL-002：升级触发经由 Recovery 决策表达。"""
        decision = RecoveryDecision(
            failure_class=FailureClass.MODEL,
            route_to=RecoveryTarget.MODEL_ROUTER,
            attempt=1,
            total_attempt_limit=2,
            carry_forward=("facts", "logs", "excluded directions"),
            minimal_correction="raise tier to strong",
            confidence=Confidence.MEDIUM,
        )
        assert decision.attempt == 1
        assert decision.route_to is RecoveryTarget.MODEL_ROUTER

    def test_has_nine_failure_classes(self) -> None:
        """L49 的 9 类失败。"""
        assert len(FailureClass) == 9

    def test_attempt_numbering_starts_at_one(self) -> None:
        with pytest.raises(ContractError, match="从 1 开始"):
            RecoveryDecision(
                failure_class=FailureClass.TOOL,
                route_to=RecoveryTarget.CAPABILITY,
                attempt=0,
                total_attempt_limit=2,
                carry_forward=(),
                minimal_correction="reprobe capability",
                confidence=Confidence.LOW,
            )

    def test_carry_forward_must_be_immutable(self) -> None:
        with pytest.raises(ContractError, match="必须是 tuple"):
            RecoveryDecision(
                failure_class=FailureClass.TOOL,
                route_to=RecoveryTarget.CAPABILITY,
                attempt=1,
                total_attempt_limit=2,
                carry_forward=["facts"],  # type: ignore[arg-type]
                minimal_correction="reprobe",
                confidence=Confidence.LOW,
            )


# --------------------------------------------------------------------------
# INV-03 的类型前提
# --------------------------------------------------------------------------


class TestRoutingPlanIsTheHandoffBoundary:
    def test_routing_plan_is_frozen(self) -> None:
        """INV-03：Adapter 不得自行改变 RoutingPlan。"""
        routing = make_routing_plan()
        with pytest.raises(dataclasses.FrozenInstanceError):
            routing.main_thread_model = make_model()  # type: ignore[misc]

    def test_rejects_wrong_run_id_prefix(self) -> None:
        with pytest.raises(ContractError, match="RunId"):
            RoutingPlan(
                run_id=new_branch_id(),
                plan=make_plan(),
                main_thread_branch_id=new_branch_id(),
                main_thread_model=make_model(),
            )

    def test_carries_only_abstract_tiers(self) -> None:
        """交给 Adapter 的是抽象档位，平台模型由 Adapter 自己翻译。"""
        routing = make_routing_plan()
        assert isinstance(routing.main_thread_model, ModelSelection)
        assert not hasattr(routing.main_thread_model, "model_id")
