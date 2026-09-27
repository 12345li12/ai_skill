"""RiskEvaluator 行为测试（第 3 步 3.3）。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-RISK-001  11 类高风险场景在风险动作前暂停  → TestRiskSignalCoverage
  TEST-RISK-004  批准范围与暂停判据分离            → TestNoPauseDecision

架构依据（4.2）：Risk Assessment 只回答"风险有多大"，**不决定"是否暂停"**。
判断是否需要审批是 Approval Judgment 的职责，且它在 Cost Estimator **之后**。
"""

from __future__ import annotations

import dataclasses

import pytest

from adaptive_router.contracts import (
    Confidence,
    ContractError,
    ImpactScope,
    ModelTier,
    RiskAssessment,
    RiskCategory,
    RiskLevel,
    new_branch_id,
)
from adaptive_router.risk import (
    SIGNAL_INPUT_FIELD,
    RiskEvaluator,
    RiskInputs,
    RiskSignal,
    requires_human_input,
)
from adaptive_router.contracts.decision import EffortLevel


def make_inputs(**overrides: object) -> RiskInputs:
    base: dict[str, object] = dict(
        broad_or_hard_to_review_change=False,
        hard_to_revert_operation=False,
        production_or_real_users=False,
        shared_infrastructure=False,
        release_deploy_merge_payment=False,
        outbound_message=False,
        external_write=False,
        credentials_privacy_permissions=False,
        major_conclusion_conflict=False,
        scope_expansion=False,
        affected_scope=ImpactScope.LOCAL,
        dependency_depth=0,
        evidence_sufficiency=Confidence.HIGH,
    )
    base.update(overrides)
    return RiskInputs(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# TEST-RISK-001
# --------------------------------------------------------------------------


class TestRiskSignalCoverage:
    def test_ten_signals_match_the_architecture_categories(self) -> None:
        """架构 2 节 [07] 的 RiskCategory 是 10 项（成本风险已移出，属 [15a]）。"""
        assert len(RiskSignal) == 10
        assert {s.value for s in RiskSignal} == {c.value for c in RiskCategory}

    def test_no_preemptive_cost_signal(self) -> None:
        """"显著额外费用"由 [15a] CostEstimate → ApprovalJudgment 判断。

        Risk 在 Cost Estimator 之前，不知道真正的成本估算，
        因此不得把成本风险伪装成前置 risk fact。
        """
        assert "SIGNIFICANT_ADDITIONAL_COST" not in {s.name for s in RiskSignal}
        assert not hasattr(RiskSignal, "SIGNIFICANT_ADDITIONAL_COST")

    def test_every_signal_can_be_raised_and_mapped(self) -> None:
        """每个信号都有对应的输入字段，且能映射回冻结契约的 RiskCategory。"""
        for signal in RiskSignal:
            field = SIGNAL_INPUT_FIELD[signal]
            inputs = make_inputs(**{field: True})
            assessment = RiskEvaluator().assess(inputs)
            assert RiskCategory(signal.value) in assessment.categories, signal

    def test_signal_to_field_mapping_is_total_and_disjoint(self) -> None:
        """映射必须覆盖全部信号，且不得有两个信号共用同一字段。"""
        assert set(SIGNAL_INPUT_FIELD) == set(RiskSignal)
        fields = list(SIGNAL_INPUT_FIELD.values())
        assert len(set(fields)) == len(fields)
        assert set(fields) <= set(RiskInputs.__dataclass_fields__)

    def test_clean_inputs_yield_low_risk_without_categories(self) -> None:
        assessment = RiskEvaluator().assess(make_inputs())
        assert assessment.level is RiskLevel.LOW
        assert assessment.categories == ()
        assert assessment.detail

    def test_any_signal_lifts_risk_above_low(self) -> None:
        for signal in RiskSignal:
            field = SIGNAL_INPUT_FIELD[signal]
            assessment = RiskEvaluator().assess(make_inputs(**{field: True}))
            assert assessment.level is not RiskLevel.LOW, signal

    def test_irreversible_signals_reach_high(self) -> None:
        """难恢复 / 生产影响 / 发布部署付款 / 凭据隐私 → HIGH。"""
        for signal in (
            RiskSignal.HARD_TO_REVERT_OPERATION,
            RiskSignal.PRODUCTION_OR_REAL_USERS,
            RiskSignal.RELEASE_DEPLOY_MERGE_PAYMENT,
            RiskSignal.CREDENTIALS_PRIVACY_PERMISSIONS,
        ):
            field = SIGNAL_INPUT_FIELD[signal]
            assessment = RiskEvaluator().assess(make_inputs(**{field: True}))
            assert assessment.level is RiskLevel.HIGH, signal

    def test_scope_and_dependency_depth_raise_risk(self) -> None:
        local = RiskEvaluator().assess(make_inputs())
        wide = RiskEvaluator().assess(make_inputs(affected_scope=ImpactScope.SYSTEM_WIDE))
        deep = RiskEvaluator().assess(make_inputs(dependency_depth=4))
        assert local.level is RiskLevel.LOW
        assert wide.level is not RiskLevel.LOW
        assert deep.level is not RiskLevel.LOW

    def test_low_evidence_raises_risk(self) -> None:
        thin = RiskEvaluator().assess(make_inputs(evidence_sufficiency=Confidence.LOW))
        assert thin.level is not RiskLevel.LOW


class TestRiskAssessmentIsANarrowing:
    """Risk 只**约束**模型选择，并且只能比输入更严，不能更松。"""

    def test_risk_never_goes_below_the_declared_inputs(self) -> None:
        for signal in RiskSignal:
            field = SIGNAL_INPUT_FIELD[signal]
            assessment = RiskEvaluator().assess(make_inputs(**{field: True}))
            # 输入宣称命中高风险场景 → 级别不得为 LOW
            assert assessment.level in (RiskLevel.MEDIUM, RiskLevel.HIGH)

    def test_assessment_is_a_frozen_contract_type(self) -> None:
        assessment = RiskEvaluator().assess(make_inputs())
        assert isinstance(assessment, RiskAssessment)
        with pytest.raises(dataclasses.FrozenInstanceError):
            assessment.level = RiskLevel.HIGH  # type: ignore[misc]

    def test_categories_are_an_immutable_tuple(self) -> None:
        assessment = RiskEvaluator().assess(
            make_inputs(external_write=True, outbound_message=True)
        )
        assert isinstance(assessment.categories, tuple)
        assert len(assessment.categories) == 2

    def test_detail_names_the_raised_signals(self) -> None:
        assessment = RiskEvaluator().assess(
            make_inputs(external_write=True, production_or_real_users=True)
        )
        assert "external_write" in assessment.detail
        assert "production_or_real_users" in assessment.detail

    def test_inputs_reject_unknown_fields(self) -> None:
        """输入集合必须封闭，否则会有人偷偷加一个成本 / 审批字段。"""
        with pytest.raises(TypeError):
            RiskInputs(needs_approval=True)  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# TEST-RISK-004 —— 暂停判据不属于此处
# --------------------------------------------------------------------------


class TestNoPauseDecision:
    def test_assessment_carries_no_approval_control(self) -> None:
        field_names = {f.name for f in dataclasses.fields(RiskAssessment)}
        assert field_names == {"level", "categories", "detail"}

    def test_risk_inputs_carry_no_approval_control(self) -> None:
        field_names = {f.name for f in dataclasses.fields(RiskInputs)}
        for forbidden in (
            "needs_approval",
            "requires_approval",
            "approved",
            "skip_approval",
            "pause",
        ):
            assert forbidden not in field_names

    def test_risk_inputs_carry_no_cost_fact(self) -> None:
        """L43 的"显著额外费用"是 [15a] 的事实，不得前置到 [07]。"""
        field_names = {f.name for f in dataclasses.fields(RiskInputs)}
        for forbidden in (
            "estimated_cost",
            "cost_estimate",
            "significant_cost",
            "budget",
        ):
            assert forbidden not in field_names

    def test_low_risk_still_permits_a_pause_decision_downstream(self) -> None:
        """即使 Risk 是 LOW，成本仍可能显著 —— 所以 Judgment 必须运行。

        本模块**不提供**任何"跳过审批判断"的出口。
        """
        assessment = RiskEvaluator().assess(make_inputs())
        assert assessment.level is RiskLevel.LOW
        assert not hasattr(assessment, "requires_approval_consideration")
        for forbidden in ("skip_approval", "approval_required", "may_proceed"):
            assert not hasattr(assessment, forbidden)


class TestRequiresHumanInputHelper:
    def test_helper_is_explicitly_outside_the_assessment(self) -> None:
        """把"需不需要人类输入"做成**独立函数**，而不是 RiskAssessment 的字段。

        这样它就无法被误用成
        ``if not risk.requires_approval_consideration: skip_approval_judgment()``
        那样的审批旁路 —— 它根本不在 risk 对象上。
        """
        assert callable(requires_human_input)
        assert not hasattr(RiskAssessment, "requires_human_input")

    def test_helper_only_reports_it_cannot_decide_alone(self) -> None:
        """本函数**不**回答"是否需要审批"，只回答"是否需要人的输入"。

        真正的是否审批由 Approval Judgment 在 Risk + Cost + Authorization
        齐备后统一判断（架构 4.2）。
        """
        assessment = RiskEvaluator().assess(make_inputs(production_or_real_users=True))
        assert requires_human_input(assessment) is True
        low = RiskEvaluator().assess(make_inputs())
        assert requires_human_input(low) is False

    def test_helper_documents_that_cost_can_trigger_it_later(self) -> None:
        docstring = requires_human_input.__doc__ or ""
        assert "Approval Judgment" in docstring


# --------------------------------------------------------------------------
# 冻结契约完整性
# --------------------------------------------------------------------------


class TestFrozenContractsUntouched:
    def test_risk_module_does_not_redefine_contract_types(self) -> None:
        from adaptive_router import risk as module

        assert module.RiskAssessment is RiskAssessment
        assert module.RiskCategory is RiskCategory

    def test_low_level_with_categories_is_still_rejected_by_the_contract(self) -> None:
        with pytest.raises(ContractError, match="不得为 LOW"):
            RiskAssessment(
                level=RiskLevel.LOW,
                categories=(RiskCategory.EXTERNAL_WRITE,),
                detail="x",
            )


# --------------------------------------------------------------------------
# 与 [06] Model Router 的衔接（只约束，不选择）
# --------------------------------------------------------------------------


class TestRiskConstrainsButDoesNotChoose:
    def test_evaluator_returns_no_model_choice(self) -> None:
        """Risk **约束**模型选择，但自己不选择 —— 输出里没有档位字段。"""
        assessment = RiskEvaluator().assess(make_inputs())
        for forbidden in ("tier", "model", "model_id", "effort"):
            assert not hasattr(assessment, forbidden)

    def test_module_does_not_import_model_selection(self) -> None:
        import inspect

        from adaptive_router import risk as module

        source = inspect.getsource(module)
        assert "ModelSelection" not in source
        assert "RoutingPlan" not in source

    def test_risk_level_is_available_for_a_downstream_constraint(self) -> None:
        """下游（[06]）需要的就是这个 level 与 categories。"""
        assessment = RiskEvaluator().assess(make_inputs(external_write=True))
        assert isinstance(assessment.level, RiskLevel)
        assert RiskCategory.EXTERNAL_WRITE in assessment.categories
        # 契约里的档位类型仍然可用（下游据此约束）
        assert ModelTier.STRONG is not None
        assert EffortLevel.HIGH is not None
        assert new_branch_id().startswith("br_")
