"""CostEstimator / Ledger / Summary 行为测试（第 3 步 3.4 / 3.5 / 3.6）。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-COST-001  成本无可靠数据时仅用低/中/高或相对描述
  TEST-COST-002  不得虚构精确 Token、费用、分支、切换、验证或结果
"""

from __future__ import annotations

import dataclasses

import pytest

from adaptive_router.contracts import (
    Confidence,
    ConfirmationSource,
    CostBasis,
    CostMagnitude,
    EffortLevel,
    LedgerStage,
    ModelSelection,
    ModelTier,
    new_branch_id,
    new_run_id,
)
from adaptive_router.cost import (
    DRIVER_FIELD_NAMES,
    NON_DRIVER_FIELDS,
    CostDriverSet,
    CostEstimator,
    CostLedger,
    CostSummaryBuilder,
    EstimationBasis,
    PlannedScope,
    ReusePolicy,
    SummaryNotReady,
    TelemetryObservation,
)

USD = "money:USD"


def drivers(**overrides: object) -> CostDriverSet:
    base: dict[str, object] = dict(
        scope=PlannedScope.MAIN_THREAD,
        tier=ModelTier.BALANCED,
        effort=EffortLevel.MEDIUM,
        planned_branch_count=0,
        max_concurrency=1,
        attempt=1,
        retry_allowance=1,
    )
    base.update(overrides)
    return CostDriverSet(**base)  # type: ignore[arg-type]


def estimator() -> CostEstimator:
    return CostEstimator()


# ==========================================================================
# 3.4 Cost Estimator —— [15a]
# ==========================================================================


class TestEstimatorNeverFabricates:
    """TEST-COST-001 / L55：**永不产生数字区间**（第 3 步收口 ①）。

    任何无真实 quote / history evidence 的情况
    → ``basis=UNKNOWN`` + **无区间**
    → 仅输出定性 ``magnitude``
    """

    def test_never_produces_a_numeric_range(self) -> None:
        """唯一可达的 basis 是 UNKNOWN；range 恒为 None。"""
        estimate = estimator().estimate(drivers()).estimate
        assert estimate.basis is CostBasis.UNKNOWN
        assert estimate.magnitude in tuple(CostMagnitude)
        assert estimate.is_quantitative is False
        assert estimate.range_min is None
        assert estimate.range_max is None

    def test_estimator_has_no_capability_input(self) -> None:
        """capability bool **不得**进入成本驱动集合。

        ``reliable_cost_data`` 是 [03] 探测的环境能力，
        只决定未来有没有机会取得 evidence，**不是成本高低的因素**。
        """
        field_names = set(CostDriverSet.__dataclass_fields__)
        assert "reliable_cost_data" not in field_names
        for forbidden in ("capability", "reliable_cost_data", "capability_snapshot"):
            assert forbidden not in field_names

    def test_quote_and_history_bases_are_unreachable_without_evidence(self) -> None:
        """PROVIDER_QUOTE / HISTORICAL 需要真实 evidence —— 目前不可达。

        直接覆盖**全部真实成本驱动组合**，不借助任何 capability bool。
        """
        for tier in ModelTier:
            for effort in EffortLevel:
                for branches in (0, 2, 8):
                    for concurrency in (1, 4):
                        estimate = estimator().estimate(
                            drivers(
                                tier=tier,
                                effort=effort,
                                planned_branch_count=branches,
                                max_concurrency=concurrency,
                            )
                        ).estimate
                        assert estimate.basis is not CostBasis.PROVIDER_QUOTE
                        assert estimate.basis is not CostBasis.HISTORICAL

    def test_never_invents_precision_under_any_driver_combination(self) -> None:
        """无论驱动组合如何，都不得出现数字。"""
        for tier in ModelTier:
            for effort in EffortLevel:
                for branches in (0, 2, 8):
                    for concurrency in (1, 4):
                        result = estimator().estimate(
                            drivers(
                                tier=tier,
                                effort=effort,
                                planned_branch_count=branches,
                                max_concurrency=concurrency,
                            )
                        )
                        assert result.estimate.is_quantitative is False

    def test_estimator_has_no_unit_price_constants(self) -> None:
        """没有真实 quote 输入时，不得持有任何"看起来像单价"的常数。"""
        for forbidden in ("_UNIT_LOW", "_UNIT_HIGH", "_range"):
            assert not hasattr(CostEstimator, forbidden), forbidden

    def test_magnitude_grows_with_tier_effort_and_branches(self) -> None:
        cheap = estimator().estimate(
            drivers(tier=ModelTier.LOW_COST, effort=EffortLevel.LOW)
        ).estimate
        pricey = estimator().estimate(
            drivers(
                tier=ModelTier.MAXIMUM,
                effort=EffortLevel.HIGH,
                planned_branch_count=6,
            )
        ).estimate
        assert cheap.magnitude is CostMagnitude.LOW
        assert pricey.magnitude is CostMagnitude.HIGH

    def test_magnitude_is_always_available(self) -> None:
        """L55 允许的定性描述：低/中/高 —— 无需任何 evidence。"""
        estimate = estimator().estimate(drivers()).estimate
        assert estimate.magnitude is CostMagnitude.MEDIUM

    def test_drivers_are_named_for_audit(self) -> None:
        estimate = estimator().estimate(
            drivers(tier=ModelTier.STRONG, planned_branch_count=3)
        ).estimate
        joined = " ".join(estimate.drivers)
        assert "tier=strong" in joined
        assert "planned_branch_count=3" in joined

    def test_driver_keys_use_real_field_names(self) -> None:
        """审计字符串的键必须是真实字段名，便于与契约字段对齐排查。"""
        for item in drivers().drivers():
            key = item.split("=", 1)[0]
            assert key in CostDriverSet.__dataclass_fields__, key


class TestEstimatorCardinality:
    """架构 4.5 [15a] 基数：**每分支的每个执行 attempt 最多一次**。"""

    def test_attempt_one_always_produces_an_estimate(self) -> None:
        result = estimator().estimate(drivers(attempt=1))
        assert result.estimate is not None
        assert result.reuse is ReusePolicy.FRESH

    def test_unchanged_drivers_reuse_the_previous_estimate(self) -> None:
        """成本驱动未变化 → 可以复用上一 attempt 的估算，并保留来源。"""
        first = estimator().estimate(drivers(attempt=1)).estimate
        second = estimator().estimate(drivers(attempt=2), previous=first)
        assert second.reuse is ReusePolicy.REUSED
        assert second.estimate is first  # 同一对象，来源未被改写

    def test_changed_drivers_force_a_reestimate(self) -> None:
        """成本驱动变化 → 重新估算。"""
        first = estimator().estimate(drivers(attempt=1, tier=ModelTier.BALANCED)).estimate
        second = estimator().estimate(
            drivers(attempt=2, tier=ModelTier.STRONG), previous=first
        )
        assert second.reuse is ReusePolicy.FRESH
        assert second.estimate is not first
        assert second.estimate.magnitude is not CostMagnitude.LOW

    @pytest.mark.parametrize(
        "changed",
        [
            {"tier": ModelTier.MAXIMUM},
            {"effort": EffortLevel.HIGH},
            {"planned_branch_count": 4},
            {"max_concurrency": 8},
            {"scope": PlannedScope.MULTI_AGENT},
        ],
    )
    def test_each_cost_driver_change_triggers_a_reestimate(
        self, changed: dict[str, object]
    ) -> None:
        """模型 / 范围 / 并发等任一驱动变化都必须重估。"""
        first = estimator().estimate(drivers(attempt=1)).estimate
        second = estimator().estimate(drivers(attempt=2, **changed), previous=first)
        assert second.reuse is ReusePolicy.FRESH, changed

    def test_retry_allowance_change_triggers_a_reestimate(self) -> None:
        first = estimator().estimate(drivers(attempt=1, retry_allowance=1)).estimate
        second = estimator().estimate(
            drivers(attempt=2, retry_allowance=2), previous=first
        )
        assert second.reuse is ReusePolicy.FRESH

    def test_no_previous_means_fresh(self) -> None:
        result = estimator().estimate(drivers(attempt=3), previous=None)
        assert result.reuse is ReusePolicy.FRESH

    def test_attempt_must_be_one_based(self) -> None:
        with pytest.raises(ValueError, match="从 1 开始"):
            estimator().estimate(drivers(attempt=0))

    def test_drivers_are_frozen(self) -> None:
        d = drivers()
        with pytest.raises(dataclasses.FrozenInstanceError):
            d.tier = ModelTier.MAXIMUM  # type: ignore[misc]


class TestDriverDescriptionCoversEveryField:
    """机械保证：``drivers()`` 编码**全部**成本驱动字段。

    背景：``CostEstimator._same_drivers`` 用 ``drivers()`` 的字符串元组
    比对来决定 fresh / reuse。**这是复用判定的唯一依据。**

    若将来给 ``CostDriverSet`` 加一个驱动字段却忘了加进 ``drivers()``，
    "驱动已变化"会被静默误判为"可复用上一 attempt 的估算" ——
    那正是不该发生的静默错误。本组测试让那种情况**必然失败**。
    """

    def test_driver_field_names_match_the_dataclass(self) -> None:
        """``DRIVER_FIELD_NAMES`` == 全部字段 − 已登记的例外。"""
        derived = tuple(
            name
            for name in CostDriverSet.__dataclass_fields__
            if name not in NON_DRIVER_FIELDS
        )
        assert DRIVER_FIELD_NAMES == derived, (
            "新增 / 删除 CostDriverSet 字段后，必须同步更新 DRIVER_FIELD_NAMES "
            "或在 NON_DRIVER_FIELDS 中显式登记理由"
        )

    def test_non_driver_fields_are_real_fields_with_a_reason(self) -> None:
        """例外必须真实存在，且必须写明理由（防止随手加白名单）。"""
        assert set(NON_DRIVER_FIELDS) <= set(CostDriverSet.__dataclass_fields__)
        for field_name, reason in NON_DRIVER_FIELDS.items():
            assert reason.strip(), field_name

    def test_drivers_count_equals_driver_field_count(self) -> None:
        """描述条数必须与驱动字段数相同 —— 少一条就是漏编码。"""
        assert len(drivers().drivers()) == len(DRIVER_FIELD_NAMES)

    @pytest.mark.parametrize("field_name", DRIVER_FIELD_NAMES)
    def test_every_driver_field_has_a_non_empty_description(
        self, field_name: str
    ) -> None:
        descriptions = dict(
            item.split("=", 1) for item in drivers().drivers()
        )
        assert field_name in descriptions, f"{field_name} 未被 drivers() 编码"
        assert descriptions[field_name].strip(), field_name

    def test_no_extra_descriptions_beyond_the_driver_fields(self) -> None:
        described = tuple(item.split("=", 1)[0] for item in drivers().drivers())
        assert described == DRIVER_FIELD_NAMES

    def test_changing_any_encoded_field_breaks_reuse(self) -> None:
        """字段级证明：只改一个驱动字段就不得复用。"""
        base = drivers(attempt=1)
        first = estimator().estimate(base).estimate
        alternatives = {
            "scope": PlannedScope.MULTI_AGENT,
            "tier": ModelTier.MAXIMUM,
            "effort": EffortLevel.HIGH,
            "planned_branch_count": 5,
            "max_concurrency": 9,
            "retry_allowance": 3,
        }
        assert set(alternatives) == set(DRIVER_FIELD_NAMES)
        for field_name, value in alternatives.items():
            changed = drivers(attempt=2, **{field_name: value})
            outcome = estimator().estimate(changed, previous=first)
            assert outcome.reuse is ReusePolicy.FRESH, field_name

    def test_identical_drivers_at_a_later_attempt_still_reuse(self) -> None:
        """attempt 本身不是成本驱动：只有它变化时仍应复用。"""
        first = estimator().estimate(drivers(attempt=1)).estimate
        second = estimator().estimate(drivers(attempt=2), previous=first)
        assert second.reuse is ReusePolicy.REUSED


class TestEstimatorHasNoModelChoice:
    """INV-05：Cost Estimator 只估算，不获得模型选择权。"""
    def test_estimator_output_carries_no_model_choice(self) -> None:
        from adaptive_router.contracts import CostEstimate

        field_names = {f.name for f in dataclasses.fields(CostEstimate)}
        for forbidden in ("tier", "model_id", "effort", "model"):
            assert forbidden not in field_names

    def test_estimator_reads_an_already_decided_selection(self) -> None:
        """它**读** [06] 已定的档位，不能反过来改档位。"""
        selection = ModelSelection(
            tier=ModelTier.STRONG, effort=EffortLevel.HIGH, reason="complex debugging"
        )
        d = CostDriverSet.for_selection(
            selection=selection,
            scope=PlannedScope.MAIN_THREAD,
            planned_branch_count=0,
            max_concurrency=1,
            attempt=1,
            retry_allowance=1,
        )
        assert d.tier is selection.tier
        assert d.effort is selection.effort
        # 估算不修改原选择
        estimator().estimate(d)
        assert selection.tier is ModelTier.STRONG

    def test_estimator_does_not_touch_approval(self) -> None:
        """它**不能自己触发审批门**，只提供输入。"""
        import inspect

        from adaptive_router.cost import estimator as module

        source = inspect.getsource(module)
        for forbidden in ("ApprovalJudgment", "ApprovalGate", "needs_approval"):
            assert forbidden not in source, forbidden


# ==========================================================================
# 3.5 Cost Ledger —— [15b]
# ==========================================================================


class TestLedgerTelemetry:
    def test_records_one_entry_per_stage_and_attempt(self) -> None:
        ledger = CostLedger()
        run_id, branch_id = new_run_id(), new_branch_id()
        planned = estimator().estimate(drivers()).estimate

        for stage in LedgerStage:
            ledger.record(
                TelemetryObservation(
                    run_id=run_id,
                    branch_id=branch_id,
                    stage=stage,
                    attempt=1,
                    planned=planned,
                    requested=None,
                    actual=None,
                )
            )
        entries = ledger.entries_for(run_id)
        assert len(entries) == len(LedgerStage) == 4
        assert {e.stage for e in entries} == set(LedgerStage)

    def test_every_entry_belongs_to_a_run_branch_attempt(self) -> None:
        ledger = CostLedger()
        run_id, branch_id = new_run_id(), new_branch_id()
        ledger.record(
            TelemetryObservation(
                run_id=run_id,
                branch_id=branch_id,
                stage=LedgerStage.ADAPTER,
                attempt=2,
                planned=estimator().estimate(drivers()).estimate,
                requested=None,
                actual=None,
            )
        )
        entry = ledger.entries_for(run_id)[0]
        assert (entry.run_id, entry.branch_id, entry.attempt) == (run_id, branch_id, 2)

    def test_ledger_keeps_unobserved_entries_as_unknown(self) -> None:
        """unknown ≠ 0：没测到就是没测到。"""
        ledger = CostLedger()
        run_id = new_run_id()
        ledger.record(
            TelemetryObservation(
                run_id=run_id,
                branch_id=new_branch_id(),
                stage=LedgerStage.MODEL_ROUTER,
                attempt=1,
                planned=estimator().estimate(drivers()).estimate,
                requested=None,
                actual=None,
            )
        )
        entry = ledger.entries_for(run_id)[0]
        assert entry.is_unknown is True
        assert entry.observed is False

    def test_ledger_observes_but_never_controls(self) -> None:
        """INV-06：Ledger 只观测，不拥有 stop / retry / rebuild 控制权。"""
        public = {name for name in dir(CostLedger) if not name.startswith("_")}
        for forbidden in ("stop", "retry", "rebuild", "cancel", "abort", "reschedule"):
            assert forbidden not in public

    def test_entries_are_immutable_once_recorded(self) -> None:
        ledger = CostLedger()
        run_id = new_run_id()
        ledger.record(
            TelemetryObservation(
                run_id=run_id,
                branch_id=new_branch_id(),
                stage=LedgerStage.ADAPTER,
                attempt=1,
                planned=estimator().estimate(drivers()).estimate,
                requested=None,
                actual=None,
            )
        )
        entry = ledger.entries_for(run_id)[0]
        with pytest.raises(dataclasses.FrozenInstanceError):
            entry.attempt = 9  # type: ignore[misc]

    def test_entries_for_filters_by_run(self) -> None:
        ledger = CostLedger()
        first_run, second_run = new_run_id(), new_run_id()
        planned = estimator().estimate(drivers()).estimate
        for run_id in (first_run, second_run):
            ledger.record(
                TelemetryObservation(
                    run_id=run_id,
                    branch_id=new_branch_id(),
                    stage=LedgerStage.ADAPTER,
                    attempt=1,
                    planned=planned,
                    requested=None,
                    actual=None,
                )
            )
        assert len(ledger.entries_for(first_run)) == 1
        assert len(ledger.entries_for(second_run)) == 1

    def test_ledger_carries_no_persistence_by_default(self) -> None:
        """INV-15：默认不存在跨会话持久化后端。"""
        assert CostLedger().persistence is None


# ==========================================================================
# 3.6 Cost Summary —— [15c]
# ==========================================================================


class TestSummaryBuilder:
    def _unit(
        self,
        builder: CostSummaryBuilder,
        *,
        branch_id: str,
        attempt: int,
        cost: float | None,
        key: str | None = USD,
        magnitude: CostMagnitude | None = None,
    ) -> None:
        planned = estimator().estimate(drivers(attempt=attempt)).estimate
        builder.note_unit(
            branch_id=branch_id,
            attempt=attempt,
            planned=planned,
            observed=cost is not None or magnitude is not None,
            confirmed_cost_exact=cost,
            cost_accounting_key=key,
            magnitudes=(magnitude,) if magnitude else (),
        )

    @staticmethod
    def _converged(builder: CostSummaryBuilder) -> CostSummaryBuilder:
        """满足架构 4.5 的两个收敛条件后才允许 build。"""
        builder.mark_converged()
        return builder

    # ------------------------------------------------------------------
    # convergence gate（第 3 步收口 ②）
    # ------------------------------------------------------------------

    def test_build_before_convergence_is_rejected(self) -> None:
        """⚠️ build-before-convergence 必须**现在就不可能**。

        否则会出现：Approval 仍 WAITING / Recovery 未结束 → Summary 已产生，
        这正是冻结架构禁止的路径。
        """
        builder = CostSummaryBuilder(run_id=new_run_id())
        self._unit(builder, branch_id=new_branch_id(), attempt=1, cost=0.20)
        with pytest.raises(SummaryNotReady):
            builder.build(complete_total_magnitude=CostMagnitude.LOW)

    def test_mark_converged_allows_build(self) -> None:
        builder = CostSummaryBuilder(run_id=new_run_id())
        self._unit(builder, branch_id=new_branch_id(), attempt=1, cost=0.20)
        builder.mark_converged()
        summary = builder.build(complete_total_magnitude=CostMagnitude.LOW)
        assert summary.run_id == builder.run_id

    def test_recording_units_does_not_satisfy_the_gate(self) -> None:
        """记录 unit 不等于收敛 —— 数据齐备不代表控制流已收敛。"""
        builder = CostSummaryBuilder(run_id=new_run_id())
        branch = new_branch_id()
        for attempt in (1, 2, 3):
            self._unit(builder, branch_id=branch, attempt=attempt, cost=0.10)
        assert builder.unit_count == 3
        assert builder.is_converged is False
        with pytest.raises(SummaryNotReady):
            builder.build(complete_total_magnitude=CostMagnitude.LOW)

    def test_the_gate_reports_the_architecture_conditions(self) -> None:
        builder = CostSummaryBuilder(run_id=new_run_id())
        with pytest.raises(SummaryNotReady) as excinfo:
            builder.build(complete_total_magnitude=CostMagnitude.LOW)
        message = str(excinfo.value)
        assert "收敛" in message
        assert "mark_converged" in message

    def test_gate_cannot_be_satisfied_implicitly(self) -> None:
        """没有"默认已收敛"的路径：新 Builder 一律未收敛。"""
        for _ in range(3):
            builder = CostSummaryBuilder(run_id=new_run_id())
            assert builder.is_converged is False

    # ------------------------------------------------------------------
    # 聚合正确性（全部在收敛后）
    # ------------------------------------------------------------------

    def test_summary_is_built_per_routing_run(self) -> None:
        """[15c] 的聚合边界是 RoutingRun，不是 Session。"""
        run_id = new_run_id()
        builder = self._converged(CostSummaryBuilder(run_id=run_id))
        self._unit(builder, branch_id=new_branch_id(), attempt=1, cost=0.20)
        summary = builder.build(complete_total_magnitude=CostMagnitude.LOW)
        assert summary.run_id == run_id

    def test_complete_total_is_mechanically_summed(self) -> None:
        builder = CostSummaryBuilder(run_id=new_run_id())
        branch = new_branch_id()
        self._unit(builder, branch_id=branch, attempt=1, cost=0.20)
        self._unit(builder, branch_id=branch, attempt=2, cost=0.35)
        summary = self._converged(builder).build(
            complete_total_magnitude=CostMagnitude.LOW
        )
        assert summary.coverage == 1.0
        assert summary.complete_total_exact == pytest.approx(0.55)

    def test_cross_denomination_never_produces_a_total(self) -> None:
        builder = CostSummaryBuilder(run_id=new_run_id())
        branch = new_branch_id()
        self._unit(builder, branch_id=branch, attempt=1, cost=0.20, key="money:USD")
        self._unit(builder, branch_id=branch, attempt=2, cost=0.30, key="money:EUR")
        summary = self._converged(builder).build(
            complete_total_magnitude=CostMagnitude.MEDIUM
        )
        assert summary.is_cross_denomination is True
        assert summary.confirmed_cost_exact is None
        assert summary.complete_total_exact is None

    def test_partial_coverage_requires_notes(self) -> None:
        builder = CostSummaryBuilder(run_id=new_run_id())
        branch = new_branch_id()
        planned = estimator().estimate(drivers()).estimate
        builder.note_unit(
            branch_id=branch,
            attempt=1,
            planned=planned,
            observed=True,
            confirmed_cost_exact=0.10,
            cost_accounting_key=USD,
        )
        builder.note_unit(
            branch_id=branch,
            attempt=2,
            planned=planned,
            observed=False,
            confirmed_cost_exact=None,
            cost_accounting_key=None,
        )
        summary = self._converged(builder).build(
            complete_total_magnitude=CostMagnitude.LOW
        )
        assert summary.coverage == pytest.approx(0.5)
        assert summary.notes  # 自动生成说明，不用调用方手写
        assert summary.complete_total_exact is None

    def test_zero_coverage_yields_qualitative_only(self) -> None:
        builder = CostSummaryBuilder(run_id=new_run_id())
        planned = estimator().estimate(drivers()).estimate
        builder.note_unit(
            branch_id=new_branch_id(),
            attempt=1,
            planned=planned,
            observed=False,
            confirmed_cost_exact=None,
            cost_accounting_key=None,
        )
        summary = self._converged(builder).build(
            complete_total_magnitude=CostMagnitude.MEDIUM
        )
        assert summary.coverage == 0.0
        assert summary.confirmed_cost_exact is None
        assert summary.is_qualitative_only is True

    def test_builder_refuses_duplicate_unit_identity(self) -> None:
        builder = CostSummaryBuilder(run_id=new_run_id())
        branch = new_branch_id()
        self._unit(builder, branch_id=branch, attempt=1, cost=0.20)
        with pytest.raises(ValueError, match="身份"):
            self._unit(builder, branch_id=branch, attempt=1, cost=0.99)

    def test_builder_output_is_a_frozen_contract_type(self) -> None:
        from adaptive_router.contracts import CostSummary

        builder = CostSummaryBuilder(run_id=new_run_id())
        self._unit(builder, branch_id=new_branch_id(), attempt=1, cost=0.20)
        summary = self._converged(builder).build(
            complete_total_magnitude=CostMagnitude.LOW
        )
        assert isinstance(summary, CostSummary)
        with pytest.raises(dataclasses.FrozenInstanceError):
            summary.notes = ()  # type: ignore[misc]

    def test_builder_never_accepts_a_freeform_total(self) -> None:
        """INV-18 / 纯聚合：调用方无法传入总额（与收敛与否无关）。"""
        builder = self._converged(CostSummaryBuilder(run_id=new_run_id()))
        self._unit(builder, branch_id=new_branch_id(), attempt=1, cost=0.20)
        with pytest.raises(TypeError):
            builder.build(  # type: ignore[call-arg]
                complete_total_magnitude=CostMagnitude.LOW,
                complete_total_exact=999.0,
            )

    def test_unit_level_magnitude_is_used_when_exact_is_missing(self) -> None:
        """observed≠exact：有量级无金额时仍算已观测，但不可加。"""
        builder = CostSummaryBuilder(run_id=new_run_id())
        self._unit(
            builder,
            branch_id=new_branch_id(),
            attempt=1,
            cost=None,
            key=None,
            magnitude=CostMagnitude.MEDIUM,
        )
        summary = self._converged(builder).build(
            complete_total_magnitude=CostMagnitude.MEDIUM
        )
        assert summary.coverage == 1.0
        assert summary.is_fully_observed_but_not_additive is True
        assert summary.complete_total_exact is None

    def test_estimate_basis_marks_heuristic_vs_unknown(self) -> None:
        assert EstimationBasis.HEURISTIC.value == CostBasis.HEURISTIC.value
        assert EstimationBasis.UNKNOWN.value == CostBasis.UNKNOWN.value


# ==========================================================================
# 真实性：不虚构
# ==========================================================================


class TestNoFabrication:
    def test_cost_module_does_not_touch_platform_surfaces(self) -> None:
        import inspect

        from adaptive_router.cost import estimator, ledger, summary

        for module in (estimator, ledger, summary):
            source = inspect.getsource(module)
            for forbidden in ("os.environ", "subprocess", "requests", "socket"):
                assert forbidden not in source, (module.__name__, forbidden)

    def test_observation_carries_a_confirmation_source(self) -> None:
        """INV-08：实际值必须携带 ConfirmationSource。"""
        field_names = {f.name for f in dataclasses.fields(TelemetryObservation)}
        assert "actual" in field_names
        observation = TelemetryObservation(
            run_id=new_run_id(),
            branch_id=new_branch_id(),
            stage=LedgerStage.ADAPTER,
            attempt=1,
            planned=estimator().estimate(drivers()).estimate,
            requested=None,
            actual=None,
        )
        assert observation.actual is None
        assert ConfirmationSource.UNAVAILABLE.value == "unavailable"
