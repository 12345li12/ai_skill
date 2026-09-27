"""contracts.cost 契约测试。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-COST-001  成本无可靠数据时仅用低/中/高或相对描述   → TestCostEstimate / TestCoverageGates
  TEST-COST-002  不得虚构精确 Token、费用、分支、切换、验证或结果
                                                          → TestUnknownIsNotZero / TestTotalsAreDerived
"""

from __future__ import annotations

import dataclasses

import pytest

from adaptive_router.contracts.common import (
    Confidence,
    ConfirmationSource,
    ContractError,
    new_branch_id,
    new_run_id,
)
from adaptive_router.contracts.cost import (
    COVERAGE_QUALITATIVE_THRESHOLD,
    CostActual,
    CostBasis,
    CostEstimate,
    CostMagnitude,
    CostRequest,
    CostSummary,
    CostUnitSummary,
    LedgerEntry,
    LedgerStage,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_estimate(**overrides: object) -> CostEstimate:
    base: dict[str, object] = dict(
        basis=CostBasis.HEURISTIC,
        magnitude=CostMagnitude.MEDIUM,
        confidence=Confidence.MEDIUM,
        drivers=("model tier: balanced", "2 branches"),
    )
    base.update(overrides)
    return CostEstimate(**base)  # type: ignore[arg-type]


def make_request() -> CostRequest:
    return CostRequest(
        model_id="gpt-5-codex", effort="medium", platform="openai", max_concurrency=2
    )


USD = "money:USD"
EUR = "money:EUR"
CREDIT = "provider-credit:openai"


def make_actual(**overrides: object) -> CostActual:
    """构造 CostActual，并保持"口径与精确金额同生同灭"。

    ``cost_exact`` 被覆盖为 ``None`` 时，会自动清掉 ``cost_accounting_key``，
    除非调用方显式给出。
    """
    base: dict[str, object] = dict(
        source=ConfirmationSource.PROVIDER_API,
        confidence=Confidence.HIGH,
        tokens_in=1000,
        tokens_out=500,
        cost_exact=0.42,
        cost_accounting_key=USD,
    )
    base.update(overrides)
    if base["cost_exact"] is None and "cost_accounting_key" not in overrides:
        base["cost_accounting_key"] = None
    return CostActual(**base)  # type: ignore[arg-type]


def make_entry(actual: CostActual | None = None, attempt: int = 1) -> LedgerEntry:
    return LedgerEntry(
        run_id=new_run_id(),
        branch_id=new_branch_id(),
        stage=LedgerStage.ADAPTER,
        attempt=attempt,
        planned=make_estimate(),
        requested=make_request(),
        actual=actual,
    )


def make_unit(
    *,
    attempt: int = 1,
    branch_id: str | None = None,
    observed: bool = True,
    cost: float | None = 0.20,
    magnitudes: tuple[CostMagnitude, ...] = (CostMagnitude.MEDIUM,),
    planned_range: tuple[float, float] | None = None,
    accounting_key: str | None = USD,
) -> CostUnitSummary:
    planned = (
        make_estimate(range_min=planned_range[0], range_max=planned_range[1])
        if planned_range
        else make_estimate()
    )
    return CostUnitSummary(
        branch_id=branch_id or new_branch_id(),
        attempt=attempt,
        planned=planned,
        observed=observed,
        magnitudes=magnitudes if (observed and cost is None) else (),
        confirmed_cost_exact=cost if observed else None,
        cost_accounting_key=accounting_key if (observed and cost is not None) else None,
    )


def make_summary(units: tuple[CostUnitSummary, ...] | None = None, **overrides: object):
    """构造 CostSummary。

    coverage / 计数 / 金额**全部派生**，所以这里只能通过 units 间接控制。
    """
    base: dict[str, object] = dict(
        run_id=new_run_id(),
        units=units if units is not None else (make_unit(),),
        complete_total_magnitude=CostMagnitude.MEDIUM,
        confidence=Confidence.HIGH,
        notes=(),
    )
    base.update(overrides)
    return CostSummary(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# TEST-COST-001
# --------------------------------------------------------------------------


class TestCostEstimate:
    def test_magnitude_only_is_legal(self) -> None:
        """L55：无可靠数据时仅用低/中/高。"""
        estimate = make_estimate()
        assert estimate.is_quantitative is False
        assert estimate.magnitude is CostMagnitude.MEDIUM

    def test_basis_unknown_forbids_a_numeric_range(self) -> None:
        """无依据就不许有数字。"""
        with pytest.raises(ContractError, match="不得给出精确区间"):
            make_estimate(basis=CostBasis.UNKNOWN, range_min=1.0, range_max=2.0)

    def test_range_must_be_complete(self) -> None:
        with pytest.raises(ContractError, match="必须同时给出"):
            make_estimate(range_min=1.0)

    def test_range_order_is_enforced(self) -> None:
        with pytest.raises(ContractError, match="不得大于"):
            make_estimate(range_min=5.0, range_max=1.0)

    def test_range_must_not_be_negative(self) -> None:
        with pytest.raises(ContractError, match="不得为负"):
            make_estimate(range_min=-1.0, range_max=1.0)

    def test_historical_basis_is_representable(self) -> None:
        """basis=historical 需要跨会话数据，必须 opt-in（INV-15）。"""
        estimate = make_estimate(basis=CostBasis.HISTORICAL, confidence=Confidence.HIGH)
        assert estimate.basis is CostBasis.HISTORICAL

    def test_estimate_carries_no_model_choice(self) -> None:
        """INV-05：Cost Estimator 只估算，不获得模型选择权。"""
        field_names = {f.name for f in dataclasses.fields(CostEstimate)}
        assert "model_id" not in field_names
        assert "tier" not in field_names
        assert "effort" not in field_names


# --------------------------------------------------------------------------
# TEST-COST-002 —— unknown ≠ 0
# --------------------------------------------------------------------------


class TestUnknownIsNotZero:
    def test_unobserved_actual_is_not_observed(self) -> None:
        actual = CostActual.unobserved()
        assert actual.observed is False
        assert actual.source is ConfirmationSource.UNAVAILABLE
        assert actual.cost_exact is None

    def test_zero_cost_counts_as_observed(self) -> None:
        """测到 0 就是测到了 —— 与"没测到"相反。"""
        actual = make_actual(cost_exact=0, tokens_in=0, tokens_out=0)
        assert actual.observed is True
        assert actual.cost_exact == 0

    def test_zero_tokens_counts_as_observed(self) -> None:
        actual = make_actual(cost_exact=None, tokens_in=0, tokens_out=0)
        assert actual.observed is True

    def test_unobserved_must_not_carry_a_value(self) -> None:
        with pytest.raises(ContractError, match="source 必须是 UNAVAILABLE"):
            CostActual(
                source=ConfirmationSource.PROVIDER_API,
                confidence=Confidence.LOW,
            )

    def test_observed_must_not_declare_unavailable_source(self) -> None:
        with pytest.raises(ContractError, match="不得为 UNAVAILABLE"):
            CostActual(
                source=ConfirmationSource.UNAVAILABLE,
                confidence=Confidence.LOW,
                cost_exact=1.0,
            )

    def test_entry_distinguishes_unknown_from_zero(self) -> None:
        unknown_entry = make_entry(actual=None)
        zero_entry = make_entry(
            actual=make_actual(cost_exact=0, tokens_in=0, tokens_out=0)
        )
        assert unknown_entry.is_unknown is True
        assert zero_entry.is_unknown is False
        assert unknown_entry.observed != zero_entry.observed

    def test_negative_values_are_rejected(self) -> None:
        with pytest.raises(ContractError, match="signed|不得为负"):
            make_actual(tokens_in=-1)


# --------------------------------------------------------------------------
# [15b] Ledger
# --------------------------------------------------------------------------


class TestLedgerEntry:
    def test_carries_run_branch_and_attempt(self) -> None:
        """架构 4.5：否则成本账会跨任务串号。"""
        entry = make_entry()
        assert entry.run_id.startswith("run_")
        assert entry.branch_id.startswith("br_")
        assert entry.attempt == 1

    def test_attempt_is_required_and_one_based(self) -> None:
        with pytest.raises(ContractError, match="从 1 开始"):
            make_entry(attempt=0)

    def test_four_ledger_stages(self) -> None:
        """架构 2 节 [15b]：埋点 [06] [10] [11] [14]。"""
        assert len(LedgerStage) == 4
        assert {s.value for s in LedgerStage} == {
            "model_router",
            "agent_manager",
            "adapter",
            "recovery",
        }

    def test_additivity_requires_an_exact_cost(self) -> None:
        """架构 9.2 边界 2：只有可加的观测才能求精确和。

        时长是可观测的，但**不可加** —— 它不是可与费用求和的同一口径。
        """
        only_duration = make_entry(
            actual=make_actual(
                cost_exact=None, tokens_in=0, tokens_out=0, duration_ms=1200
            )
        )
        assert only_duration.observed is True
        assert only_duration.is_additive is False

    def test_additivity_holds_when_exact_cost_is_observed(self) -> None:
        assert make_entry(actual=make_actual()).is_additive is True

    def test_ledger_carries_no_control_fields(self) -> None:
        """INV-06：只观测，不拥有 stop / retry / rebuild 控制权。"""
        field_names = {f.name for f in dataclasses.fields(LedgerEntry)}
        for forbidden in ("stop", "retry", "rebuild", "action", "command", "decision"):
            assert forbidden not in field_names


# --------------------------------------------------------------------------
# CostUnitSummary —— 一个 unit 严格对应 (branch_id, attempt)
# --------------------------------------------------------------------------


class TestCostUnitIdentity:
    def test_unit_carries_attempt(self) -> None:
        """最后三项之 ②：单位必须携带 attempt，否则多次尝试失去独立身份。"""
        field_names = {f.name for f in dataclasses.fields(CostUnitSummary)}
        assert "branch_id" in field_names
        assert "attempt" in field_names

    def test_two_attempts_on_one_branch_are_distinguishable(self) -> None:
        """branch A / attempt 1 / $0.20 与 branch A / attempt 2 / $0.35 必须可区分。"""
        branch = new_branch_id()
        first = make_unit(branch_id=branch, attempt=1, cost=0.20)
        second = make_unit(branch_id=branch, attempt=2, cost=0.35)
        assert first.branch_id == second.branch_id
        assert first.unit_key != second.unit_key
        assert first.unit_key == (branch, 1)
        assert second.unit_key == (branch, 2)

    def test_attempt_is_one_based_and_required(self) -> None:
        with pytest.raises(ContractError, match="从 1 开始"):
            make_unit(attempt=0)

    def test_attempt_must_be_an_int(self) -> None:
        with pytest.raises(ContractError, match="attempt 必须是 int"):
            CostUnitSummary(
                branch_id=new_branch_id(),
                attempt="1",  # type: ignore[arg-type]
                planned=make_estimate(),
                observed=True,
                magnitudes=(),
                confirmed_cost_exact=1.0,
            )

    def test_branch_id_must_be_valid(self) -> None:
        with pytest.raises(ContractError, match="BranchId"):
            make_unit(branch_id="attempt-1")


class TestCostUnitObservedSemantics:
    def test_unobserved_unit_must_not_carry_amounts(self) -> None:
        with pytest.raises(ContractError, match="不得给出精确值"):
            CostUnitSummary(
                branch_id=new_branch_id(),
                attempt=1,
                planned=make_estimate(),
                observed=False,
                magnitudes=(),
                confirmed_cost_exact=0.0,  # ← 用 0 填坑
            )

    def test_unobserved_unit_must_not_carry_magnitudes(self) -> None:
        with pytest.raises(ContractError, match="不得给定量级"):
            CostUnitSummary(
                branch_id=new_branch_id(),
                attempt=1,
                planned=make_estimate(),
                observed=False,
                magnitudes=(CostMagnitude.LOW,),
            )

    def test_unobserved_unit_must_not_carry_a_variance(self) -> None:
        with pytest.raises(ContractError, match="不得给出差额"):
            CostUnitSummary(
                branch_id=new_branch_id(),
                attempt=1,
                planned=make_estimate(),
                observed=False,
                magnitudes=(),
                variance_exact=0.1,
            )

    def test_observed_without_exact_cost_is_legal(self) -> None:
        """观测到了数据，但没有精确金额 —— 仍然是 observed，不是 unknown。"""
        unit = make_unit(cost=None, magnitudes=(CostMagnitude.MEDIUM,))
        assert unit.is_observed_but_inexact is True
        assert unit.has_exact_cost is False
        assert unit.is_additive is False

    def test_observed_must_report_something(self) -> None:
        """观测到了却什么都不报，等于把已确认信息丢掉。"""
        with pytest.raises(ContractError, match="confirmed_cost_exact 或 magnitudes"):
            make_unit(cost=None, magnitudes=())

    def test_variance_requires_an_exact_cost(self) -> None:
        """差额依赖精确成本 —— 只有量级就算不出差额。"""
        with pytest.raises(ContractError, match="就算不出 variance_exact"):
            CostUnitSummary(
                branch_id=new_branch_id(),
                attempt=1,
                planned=make_estimate(),
                observed=True,
                magnitudes=(CostMagnitude.MEDIUM,),
                confirmed_cost_exact=None,
                variance_exact=0.1,
            )

    def test_observed_with_exact_cost_is_fully_reported(self) -> None:
        unit = make_unit()
        assert unit.has_exact_cost is True
        assert unit.is_observed_but_inexact is False
        assert unit.is_additive is True


# --------------------------------------------------------------------------
# 最后三项之 ③：Summary 的 coverage 与金额必须机械核算
# --------------------------------------------------------------------------


class TestTotalsAreDerived:
    def test_caller_cannot_supply_coverage_or_totals(self) -> None:
        """这些字段全部派生 —— 调用方**没有机会**填写。"""
        names = {f.name for f in dataclasses.fields(CostSummary)}
        assert names == {
            "run_id",
            "units",
            "complete_total_magnitude",
            "confidence",
            "notes",
        }
        for derived in (
            "coverage",
            "cost_units_total",
            "cost_units_observed",
            "confident_units_additive",
            "confirmed_cost_exact",
            "complete_total_exact",
            "branches",
            "entries_total",
        ):
            assert derived not in names

    def test_derived_values_cannot_be_passed_in(self) -> None:
        with pytest.raises(TypeError):
            CostSummary(  # type: ignore[call-arg]
                run_id=new_run_id(),
                units=(),
                coverage=1.0,
                complete_total_magnitude=CostMagnitude.LOW,
                confidence=Confidence.LOW,
                notes=(),
            )

    def test_coverage_is_recomputed_from_units(self) -> None:
        summary = make_summary(
            units=(make_unit(attempt=1), make_unit(attempt=2, observed=False)),
            notes=("1 unit unobserved",),
        )
        assert summary.cost_units_total == 2
        assert summary.cost_units_observed == 1
        assert summary.coverage == pytest.approx(0.5)

    def test_complete_total_is_the_mechanical_sum(self) -> None:
        """unit 合计 $0.55 ⇒ complete_total_exact 必须是 $0.55。

        最后三项之 ③：调用方无法把它写成 $999 —— 因为它根本不是入参。
        """
        first = make_unit(attempt=1, cost=0.20)
        second = make_unit(attempt=2, cost=0.35)
        summary = make_summary(units=(first, second))
        assert summary.coverage == 1.0
        assert summary.confident_units_additive is True
        assert summary.complete_total_exact == pytest.approx(0.55)
        assert summary.confirmed_cost_exact == pytest.approx(0.55)

    def test_confirmed_total_is_the_sum_of_observed_exact_units(self) -> None:
        first = make_unit(attempt=1, cost=0.20)
        second = make_unit(attempt=2, cost=0.35)
        third = make_unit(attempt=3, observed=False)
        summary = make_summary(
            units=(first, second, third), notes=("1 unit unobserved",)
        )
        assert summary.confirmed_cost_exact == pytest.approx(0.55)
        assert summary.complete_total_exact is None  # coverage < 1

    def test_variance_is_derived_only_when_fully_additive(self) -> None:
        unit = make_unit(attempt=1, cost=1.5, planned_range=(1.0, 2.0))
        summary = make_summary(units=(unit,))
        assert summary.planned_total_exact == pytest.approx(2.0)
        assert summary.complete_total_exact == pytest.approx(1.5)
        assert summary.variance_total_exact == pytest.approx(-0.5)

    def test_variance_is_unknown_when_planned_range_is_missing(self) -> None:
        summary = make_summary(units=(make_unit(attempt=1, cost=1.5),))
        assert summary.planned_total_exact is None
        assert summary.variance_total_exact is None

    def test_duplicate_unit_identity_is_rejected(self) -> None:
        """一个 (branch_id, attempt) 只能有一条汇总。"""
        branch = new_branch_id()
        with pytest.raises(ContractError, match="身份必须唯一"):
            make_summary(
                units=(
                    make_unit(branch_id=branch, attempt=1),
                    make_unit(branch_id=branch, attempt=1),
                )
            )

    def test_unobserved_unit_keys_are_exposed(self) -> None:
        branch = new_branch_id()
        summary = make_summary(
            units=(
                make_unit(branch_id=branch, attempt=1),
                make_unit(branch_id=branch, attempt=2, observed=False),
            ),
            notes=("1 unit unobserved",),
        )
        assert summary.unobserved_unit_keys == ((branch, 2),)
        assert len(summary.unobserved_units) == 1


# --------------------------------------------------------------------------
# 成本口径可比性（架构 9.2 边界 2：跨平台口径不同不可加）
# --------------------------------------------------------------------------


class TestCostAccountingKeyOnActual:
    def test_exact_cost_requires_an_accounting_key(self) -> None:
        """有金额没口径 → 拒绝：无法判断可比性。"""
        with pytest.raises(ContractError, match="cost_accounting_key"):
            CostActual(
                source=ConfirmationSource.PROVIDER_API,
                confidence=Confidence.HIGH,
                cost_exact=0.42,
                cost_accounting_key=None,
            )

    def test_accounting_key_requires_an_exact_cost(self) -> None:
        """有口径没金额 → 拒绝：口径只用来解释金额。"""
        with pytest.raises(ContractError, match="不得给出 cost_accounting_key"):
            CostActual(
                source=ConfirmationSource.PROVIDER_API,
                confidence=Confidence.HIGH,
                tokens_in=10,
                cost_exact=None,
                cost_accounting_key=USD,
            )

    def test_unobserved_actual_needs_no_accounting_key(self) -> None:
        """无 exact cost 时不要求 accounting key。"""
        actual = CostActual.unobserved()
        assert actual.cost_accounting_key is None
        assert actual.observed is False

    def test_magnitude_only_needs_no_accounting_key(self) -> None:
        """只有量级、没有金额时也不要求口径。"""
        actual = make_actual(cost_exact=None, cost_accounting_key=None)
        assert actual.observed is True
        assert actual.cost_accounting_key is None

    def test_money_and_credit_keys_are_opaque_to_the_core(self) -> None:
        """Core 不解释 business 含义，只保存 opaque key。"""
        for key in (USD, EUR, CREDIT, "provider-credit:some-vendor"):
            actual = make_actual(cost_accounting_key=key)
            assert actual.cost_accounting_key == key

    def test_empty_accounting_key_is_rejected(self) -> None:
        with pytest.raises(ContractError, match="cost_accounting_key"):
            make_actual(cost_accounting_key="  ")


class TestCostAccountingKeyOnUnit:
    def test_unit_exact_cost_requires_an_accounting_key(self) -> None:
        with pytest.raises(ContractError, match="cost_accounting_key"):
            CostUnitSummary(
                branch_id=new_branch_id(),
                attempt=1,
                planned=make_estimate(),
                observed=True,
                magnitudes=(),
                confirmed_cost_exact=0.20,
                cost_accounting_key=None,
            )

    def test_unit_accounting_key_requires_an_exact_cost(self) -> None:
        with pytest.raises(ContractError, match="不得给出 cost_accounting_key"):
            CostUnitSummary(
                branch_id=new_branch_id(),
                attempt=1,
                planned=make_estimate(),
                observed=True,
                magnitudes=(CostMagnitude.MEDIUM,),
                confirmed_cost_exact=None,
                cost_accounting_key=USD,
            )

    def test_unit_is_additive_only_with_an_accounting_key(self) -> None:
        """``is_additive`` 不再等同于 ``has_exact_cost``。"""
        ok = make_unit(cost=0.20, accounting_key=USD)
        assert ok.has_exact_cost is True
        assert ok.is_additive is True

    def test_inexact_unit_is_not_additive(self) -> None:
        inexact = make_unit(cost=None, magnitudes=(CostMagnitude.MEDIUM,))
        assert inexact.has_exact_cost is False
        assert inexact.is_additive is False


class TestCrossDenominationIsNotAdditive:
    """架构 9.2 边界 2：跨平台口径不同不可加。

    这是审查指出的真实缺口 —— 原实现只看 ``has_exact_cost``，
    会把 USD + EUR 拼成一个假精确值。
    """

    def test_same_accounting_key_is_additive(self) -> None:
        """USD + USD → 可加。"""
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.20, accounting_key=USD),
                make_unit(attempt=2, cost=0.30, accounting_key=USD),
            )
        )
        assert summary.confident_units_additive is True
        assert summary.is_cross_denomination is False
        assert summary.cost_accounting_key == USD
        assert summary.complete_total_exact == pytest.approx(0.50)

    def test_usd_plus_eur_is_not_additive(self) -> None:
        """USD + EUR → **不可加**，不得产出 0.50。"""
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.20, accounting_key=USD),
                make_unit(attempt=2, cost=0.30, accounting_key=EUR),
            ),
            notes=("units use different denominations: money:EUR, money:USD",),
        )
        assert summary.confident_units_additive is False
        assert summary.is_cross_denomination is True
        assert summary.cost_accounting_key is None
        assert summary.confirmed_cost_exact is None
        assert summary.complete_total_exact is None

    def test_usd_plus_provider_credit_is_not_additive(self) -> None:
        """USD + provider credit → **不可加**。"""
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.20, accounting_key=USD),
                make_unit(attempt=2, cost=0.30, accounting_key=CREDIT),
            ),
            notes=("mixed money and provider credits",),
        )
        assert summary.confident_units_additive is False
        assert summary.is_cross_denomination is True
        assert summary.complete_total_exact is None

    def test_cross_denomination_with_full_coverage_is_still_legal(self) -> None:
        """coverage=1 且跨口径 → 合法对象，只是没有可加总额。"""
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.20, accounting_key=USD),
                make_unit(attempt=2, cost=0.30, accounting_key=EUR),
            ),
            notes=("cross-denomination",),
        )
        assert summary.coverage == 1.0
        assert summary.is_fully_observed_but_not_additive is True
        assert summary.is_quantitatively_complete is False
        assert summary.is_qualitative_only is False

    def test_mixed_denomination_with_a_magnitude_only_unit_is_not_additive(
        self,
    ) -> None:
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.20, accounting_key=USD),
                make_unit(attempt=2, cost=None, magnitudes=(CostMagnitude.MEDIUM,)),
            ),
            notes=("one unit has no exact cost",),
        )
        assert summary.confident_units_additive is False
        assert summary.confirmed_cost_exact is None

    def test_three_way_cross_denomination_is_not_additive(self) -> None:
        """审查给出的三方场景：美元 + 本地 GPU 秒 + token。"""
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.20, accounting_key=USD),
                make_unit(attempt=2, cost=12.5, accounting_key="resource:gpu-second"),
                make_unit(attempt=3, cost=8000.0, accounting_key="resource:token"),
            ),
            notes=("three non-additive measures",),
        )
        assert summary.is_cross_denomination is True
        assert summary.accounting_keys == frozenset(
            {USD, "resource:gpu-second", "resource:token"}
        )
        assert summary.complete_total_exact is None
        assert summary.confirmed_cost_exact is None

    def test_accounting_key_is_ignored_for_unobserved_units(self) -> None:
        """未观测的 unit 不携带口径，不影响一致性判断。"""
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.20, accounting_key=USD),
                make_unit(attempt=2, observed=False),
            ),
            notes=("1 unit unobserved",),
        )
        assert summary.accounting_keys == frozenset({USD})
        assert summary.confident_units_additive is True  # 已观测部分口径一致
        assert summary.confirmed_cost_exact == pytest.approx(0.20)
        # 但 coverage < 1，仍不得给完整总额
        assert summary.complete_total_exact is None


# --------------------------------------------------------------------------
# Coverage 分级门（现在全部由 units 推导）
# --------------------------------------------------------------------------


class TestCoverageGates:
    def test_coverage_zero_forbids_any_amount(self) -> None:
        """coverage=0 → confirmed_cost_exact 派生为 None（不可能有金额）。"""
        summary = make_summary(
            units=(make_unit(attempt=1, observed=False),),
            complete_total_magnitude=CostMagnitude.MEDIUM,
            confidence=Confidence.LOW,
            notes=("no usage data returned by platform",),
        )
        assert summary.coverage == 0.0
        assert summary.confirmed_cost_exact is None
        assert summary.complete_total_exact is None
        assert summary.is_qualitative_only is True

    def test_low_coverage_is_qualitative_only(self) -> None:
        """c < 0.5 → 整体只给定性结论，但已确认部分仍可展示。"""
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.10),
                make_unit(attempt=2, observed=False),
                make_unit(attempt=3, observed=False),
                make_unit(attempt=4, observed=False),
            ),
            confidence=Confidence.LOW,
            notes=("3 of 4 units have no usage data",),
        )
        assert summary.coverage == pytest.approx(0.25)
        assert summary.is_qualitative_only is True
        assert summary.confirmed_cost_exact == pytest.approx(0.10)
        assert summary.complete_total_exact is None

    def test_partial_coverage_allows_confirmed_exact_only(self) -> None:
        """架构 9.2 / 审查样例：已确认成本精确 + 覆盖率 + 完整总成本 unknown。"""
        units = tuple(
            make_unit(attempt=i, cost=0.42 / 31) for i in range(1, 32)
        ) + tuple(make_unit(attempt=i, observed=False) for i in range(32, 51))
        summary = make_summary(units=units, notes=("19 units could not be confirmed",))

        assert summary.cost_units_total == 50
        assert summary.cost_units_observed == 31
        assert summary.coverage == pytest.approx(0.62)
        assert summary.confirmed_cost_exact == pytest.approx(0.42)
        assert summary.complete_total_exact is None
        assert summary.is_quantitatively_complete is False
        assert summary.is_qualitative_only is False  # >= 0.5

    def test_single_observed_unit_is_full_coverage(self) -> None:
        """第 2 步审查裁决 C 第二层：attempt 级 unit，不是 Ledger 行数。

        一次 attempt 产生 4 条 telemetry，只有 Adapter 那条有真实费用 ——
        coverage 仍是 100%，不是 25%。
        """
        summary = make_summary(units=(make_unit(attempt=1, cost=0.42),))
        assert summary.cost_units_total == 1
        assert summary.cost_units_total != len(LedgerStage)
        assert summary.coverage == 1.0
        assert summary.is_quantitatively_complete is True

    def test_full_coverage_but_non_additive_is_still_legal(self) -> None:
        """coverage=1 但口径不可加 → **合法**，只是不能 exact total。

        含义是"全部知道，但不能相加"，不是 unknown。
        例：unit A 美元费用完整 / unit B 本地 GPU 秒完整 /
            unit C 只给 token —— coverage 100%，三个口径不能加。
        """
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.20),
                make_unit(attempt=2, cost=None, magnitudes=(CostMagnitude.MEDIUM,)),
                make_unit(attempt=3, cost=None, magnitudes=(CostMagnitude.LOW,)),
            ),
            confidence=Confidence.HIGH,
            notes=("units use non-additive measures",),
        )
        assert summary.coverage == 1.0
        assert summary.confident_units_additive is False
        assert summary.is_fully_observed_but_not_additive is True
        assert summary.complete_total_exact is None
        assert summary.is_quantitatively_complete is False
        assert summary.is_qualitative_only is False  # coverage 是满的

    def test_non_additive_must_not_produce_a_confirmed_total_either(self) -> None:
        """口径不可加时连"已确认合计"都不该给 —— 否则会把不同口径相加。"""
        summary = make_summary(
            units=(
                make_unit(attempt=1, cost=0.20),
                make_unit(attempt=2, cost=None, magnitudes=(CostMagnitude.MEDIUM,)),
            ),
            notes=("non-additive",),
        )
        assert summary.confident_units_additive is False
        assert summary.confirmed_cost_exact is None

    def test_incomplete_coverage_requires_notes(self) -> None:
        with pytest.raises(ContractError, match="必须给出 notes"):
            make_summary(
                units=(
                    make_unit(attempt=1),
                    make_unit(attempt=2, observed=False),
                ),
                notes=(),
            )

    def test_threshold_is_the_frozen_value(self) -> None:
        """架构 9.2 冻结值 0.5。"""
        assert COVERAGE_QUALITATIVE_THRESHOLD == 0.5


# --------------------------------------------------------------------------
# INV-18 —— 命名即约束
# --------------------------------------------------------------------------


class TestInv18FieldNaming:
    def test_exact_amount_fields_are_named_confirmed(self) -> None:
        """未知覆盖时精确值字段名必须含"已确认"语义。

        ``confirmed_cost_exact`` / ``complete_total_exact`` / ``coverage``
        现在是**派生属性**（不是 dataclass field），仍然必须是公开 API。
        ``complete_total_magnitude`` 是 L55 允许的定性结论，仍由调用方给出，
        但它是**量级**而非金额，不构成"自由填写总额"的漏洞。
        """
        for derived in (
            "confirmed_cost_exact",
            "complete_total_exact",
            "coverage",
            "cost_units_total",
            "cost_units_observed",
            "confident_units_additive",
        ):
            assert isinstance(getattr(CostSummary, derived), property), derived

        magnitudes = {
            f.name for f in dataclasses.fields(CostSummary) if "magnitude" in f.name
        }
        assert magnitudes == {"complete_total_magnitude"}

        # 不得存在一个含糊的 "cost" / "total" 精确字段
        field_names = {f.name for f in dataclasses.fields(CostSummary)}
        assert "cost" not in field_names
        assert "total" not in field_names
        assert not hasattr(CostSummary, "cost")
        assert not hasattr(CostSummary, "total")

    def test_coverage_is_always_available(self) -> None:
        """INV-14：Cost Summary 必须披露 coverage（派生，永远存在）。"""
        summary = make_summary(
            units=(), confidence=Confidence.LOW, notes=("no units executed",)
        )
        assert summary.coverage == 0.0
        assert summary.cost_units_total == 0
        assert summary.complete_total_exact is None

    def test_no_units_means_no_amounts(self) -> None:
        summary = make_summary(
            units=(), confidence=Confidence.LOW, notes=("no units executed",)
        )
        assert summary.confirmed_cost_exact is None
        assert summary.is_qualitative_only is True
