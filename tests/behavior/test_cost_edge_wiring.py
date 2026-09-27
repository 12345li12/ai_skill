"""``C1 成本观测边``（`[14] → [15b]/[15c]` 接线）—— 结构锁 **LCS-1 – LCS-9** + 行为测试 **B1 – B8**。

架构依据：`docs/architecture.md` 2 节 ``[15b] Cost Ledger`` / ``[15c] Cost Summary`` ·
4.5 Cost Y 型流 · 9.2 Coverage 规则 · ``INV-06`` / ``INV-07`` / ``INV-14`` / ``INV-18`` / ``INV-S3``。
范围、冻结、授权与实现记录见 `tests/behavior/README_step5b.md`
**§103**（scope review）· **§104**（冻结）· **§105**（实现授权）· **§106**（实现记录）。

**本模块要守住的四条主张：**

  1. **本边不构造**：账本（Session 级）与 builder（每 RoutingRun 一份）都由调用方注入；
  2. **只搬运、不折算**：观测七字段与 summary 三输入**全部同名透传**；``None`` 不折成 ``0``；
  3. **gate 只读真实事实**：``mark_converged()`` 的唯一调用点带 ``not routing_run.is_converged``
     守卫 ⇒ 未收敛时**显式失败**，且**不**把旗标打开；
  4. **缺失显式化**：账本里没有的 expected unit 记为**未观测**并**返回**
     （权威披露口径 = ``CostSummary.unobserved_unit_keys``）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `CostLedger` / `mark_converged` /
`observed=True` 等词 ⇒ 所有锁一律 **AST 级**。
⚠️ 敏感性 probe 一律在**源码字符串**上完成（``_mutate``），**不覆写** ``src/`` 里的真实文件；
真实源码的一次性变异证据见 README §106.5。
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from adaptive_router.agent_manager import AgentManager
from adaptive_router.contracts import (
    BranchId,
    BranchState,
    Confidence,
    ConfirmationSource,
    ContextBundle,
    CostActual,
    CostBasis,
    CostEstimate,
    CostMagnitude,
    CostRequest,
    LedgerEntry,
    LedgerStage,
    RoutingRun,
    RunTerminal,
    TerminalState,
    new_branch_id,
    new_run_id,
)
from adaptive_router.cost.ledger import CostLedger
from adaptive_router.cost.ledger_wiring import (
    CostEdgeWiringError,
    ExpectedUnit,
    build_run_summary,
    note_expected_units,
    record_observation,
)
from adaptive_router.cost.summary import CostSummaryBuilder
from adaptive_router.dispatch_decisions import DispatchAction, DispatchDecision

from _cost_edge_locks import (
    annotated_assign_names,
    annotated_name_targets,
    assignment_value_shapes,
    attribute_assignment_targets,
    call_keyword_shapes,
    call_keyword_shapes_by_caller,
    calls_to,
    clock_call_hits,
    forbidden_import_hits,
    function_raise_guards,
    function_return_annotation,
    function_signature_shape,
    imported_module_tokens,
    imported_names,
    module_level_container_assignments,
    module_level_function_names,
    none_coalescing_shapes,
    open_calls,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
WIRING_PATH = SRC / "cost/ledger_wiring.py"
WIRING = WIRING_PATH.read_text(encoding="utf-8")

USD = "money:USD"

#: LCS-9 —— 依赖白名单（精确集合）。
#: ⚠️ `dataclasses` / `dataclass` 是**唯一**允许的 stdlib token（§103.4 冻结的接口形状要求
#: `ExpectedUnit` 是 frozen dataclass）；`__future__` / `annotations` 是语言级前向声明，显式扣除。
FUTURE_TOKENS = {"__future__", "annotations"}
ALLOWED_STDLIB = {"dataclasses", "dataclass"}
EXPECTED_IMPORT_MODULES = {"contracts", "ledger", "summary"}
EXPECTED_IMPORTED_NAMES = {
    "BranchId",
    "Confidence",
    "CostActual",
    "CostEstimate",
    "CostMagnitude",
    "CostRequest",
    "CostSummary",
    "LedgerEntry",
    "LedgerStage",
    "RoutingRun",
    "RunId",
    "CostLedger",
    "TelemetryObservation",
    "CostSummaryBuilder",
    "dataclass",
}

#: LCS-5 —— `note_unit` 的**精确**实参形态：缺失 unit 只能记成"未观测且无任何数值"。
EXPECTED_NOTE_UNIT_SHAPES = {
    "branch_id": ("unit.branch_id",),
    "attempt": ("unit.attempt",),
    "planned": ("unit.planned",),
    "observed": ("False",),
    "confirmed_cost_exact": ("None",),
    "cost_accounting_key": ("None",),
    "magnitudes": ("()",),
}

#: LCS-2 —— 观测七字段必须与七个形参**同名**。
EXPECTED_OBSERVATION_SHAPES = {
    "run_id": ("run_id",),
    "branch_id": ("branch_id",),
    "stage": ("stage",),
    "attempt": ("attempt",),
    "planned": ("planned",),
    "requested": ("requested",),
    "actual": ("actual",),
}

#: LCS-2 —— summary 三输入同样同名透传。
EXPECTED_BUILD_SHAPES = {
    "complete_total_magnitude": ("complete_total_magnitude",),
    "confidence": ("confidence",),
    "extra_notes": ("extra_notes",),
}

#: LCS-5 —— 模块内**允许出现**的局部绑定（多一个都要红：防止"先算再传"或引入状态）。
EXPECTED_BINDINGS = {
    "__all__",
    "checked",
    "checked_builder",
    "checked_ledger",
    "observation",
    "units",
    "noted",
    "unobserved",
    "key",
}

#: LCS-5 / LCS-6 —— 控制方法（观测者一旦调用它们就获得了控制权）。
CONTROL_CALLS = (
    "materialize",
    "commit_started",
    "record_outcome",
    "close_run",
    "apply_runtime_control",
    "decide_recovery",
)

#: LCS-6 —— 禁用依赖方向（INV-S3 / Core 边界）。
FORBIDDEN_IMPORT_TOKENS = (
    "agent_manager",
    "scheduler",
    "adapters",
    "recovery",
    "recovery_wiring",
    "recovery_context",
    "execution",
    "terminal_wiring",
    "validation",
)

#: LCS-7 —— 受保护 artifact（基线见 README §103.7 / §106.5）。
PROTECTED_ARTIFACTS = {
    "src/adaptive_router/cost/ledger.py": (
        "A3605D8C12578AE63ABFF9E607F618044D925C99D231F24AE560B35957B61C40"
    ),
    "src/adaptive_router/cost/summary.py": (
        "CF2F98A818D8ECAED8FA8233484803DBC4DF34B6B8DF018782BC08093F7FF1CF"
    ),
    "src/adaptive_router/cost/estimator.py": (
        "023E9A607726E7156C8E604BEE1955E0B51A08160436B4F14E8EF58052763D66"
    ),
    "src/adaptive_router/cost/wiring.py": (
        "1292AB3892E661F43F4CE5B763A38BBCADE2DBCE28C2E884C5D5CE4DA4693E51"
    ),
    "src/adaptive_router/contracts/cost.py": (
        "367ED7B553A87FB7978E3029D46A136CA15A5E7BF5FB61125B105940233B8CB5"
    ),
    "docs/architecture.md": (
        "CB3FCE709C2548EF3C7676886CC921176E5B9C82AA10CDF506F6A18438FA1BB5"
    ),
}

#: LCS-5 / 结构 —— 七个模块级函数（不得增删）。
EXPECTED_FUNCTIONS = (
    "_require_ledger",
    "_require_builder",
    "_require_routing_run",
    "_require_expected_units",
    "record_observation",
    "note_expected_units",
    "build_run_summary",
)


def _mutate(old: str, new: str) -> str:
    """**合成变异源码**（只改字符串，不动 ``src/``）；锚点未命中即失败（防 probe 假绿）。"""
    variant = WIRING.replace(old, new)
    assert variant != WIRING, f"probe 的锚点未命中，变异未生效：{old!r}"
    return variant


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def estimate() -> CostEstimate:
    return CostEstimate(
        basis=CostBasis.HEURISTIC,
        magnitude=CostMagnitude.LOW,
        confidence=Confidence.MEDIUM,
        drivers=("model:balanced",),
    )


def request_value() -> CostRequest:
    return CostRequest(model_id="gpt-5-codex", effort="medium", platform="openai")


def actual_magnitude_only() -> CostActual:
    """观测到量级、但没有精确金额 —— 合法状态（``observed=True`` / 不可加）。"""
    return CostActual(
        source=ConfirmationSource.PROVIDER_API,
        confidence=Confidence.HIGH,
        cost_magnitude=CostMagnitude.LOW,
    )


def actual_exact_zero() -> CostActual:
    """**测到 0**（与 ``actual=None`` 完全不同，INV-07）。"""
    return CostActual(
        source=ConfirmationSource.PROVIDER_API,
        confidence=Confidence.HIGH,
        cost_exact=0.0,
        cost_accounting_key=USD,
    )


def zero_branch_run(*, converged: bool) -> RoutingRun:
    """0 分支的 Run：条件①**真空成立**；``converged`` 只切换真实收敛事实的两种取值。"""
    run_id = new_run_id()
    if not converged:
        return RoutingRun(
            run_id=run_id,
            branches=(),
            terminal=None,
            attempt=1,
            total_attempt_limit=1,
            control_flow_converged=False,
        )
    return RoutingRun(
        run_id=run_id,
        branches=(),
        terminal=RunTerminal(run_id=run_id, state=TerminalState.SUCCESS),
        attempt=1,
        total_attempt_limit=1,
        control_flow_converged=True,
    )


def summary_inputs() -> dict[str, object]:
    return {
        "complete_total_magnitude": CostMagnitude.MEDIUM,
        "confidence": Confidence.MEDIUM,
        "extra_notes": (),
    }


def bundle() -> ContextBundle:
    return ContextBundle(
        goal="g",
        background=(),
        constraints=(),
        allowed_operations=(),
        forbidden_operations=(),
        output_format="text",
        completion_criteria="done",
        confirmed_conclusions=(),
        excluded=(),
        includes_history=False,
    )


# ==========================================================================
# LCS-1 —— 本边不构造账本 / builder
# ==========================================================================


class TestLCS1NoConstructionByTheEdge:
    def test_edge_constructs_neither_ledger_nor_builder(self) -> None:
        assert calls_to(WIRING, ("CostLedger", "CostSummaryBuilder")) == []

    def test_no_module_in_src_constructs_them_either(self) -> None:
        hits = []
        for path in sorted(SRC.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for name, lineno in calls_to(source, ("CostLedger", "CostSummaryBuilder")):
                hits.append(f"{path.name}:{lineno}:{name}")
        assert hits == []

    def test_probe_second_ledger_construction_goes_red(self) -> None:
        variant = _mutate(
            "    checked = _require_ledger(ledger, entry=\"record_observation\")",
            "    ledger = CostLedger()\n"
            "    checked = _require_ledger(ledger, entry=\"record_observation\")",
        )
        assert calls_to(variant, ("CostLedger",)) != []


# ==========================================================================
# LCS-2 —— 观测 / 汇总输入：唯一调用点 + 同名透传
# ==========================================================================


class TestLCS2PassthroughIsUniqueAndNamed:
    def test_observation_is_constructed_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("TelemetryObservation",))) == 1

    def test_observation_fields_are_the_parameter_names(self) -> None:
        shapes = call_keyword_shapes_by_caller(WIRING, "TelemetryObservation")
        assert shapes == {"record_observation": EXPECTED_OBSERVATION_SHAPES}

    def test_record_is_called_exactly_once(self) -> None:
        assert len(calls_to(WIRING, ("record",))) == 1

    def test_build_inputs_are_the_parameter_names(self) -> None:
        assert call_keyword_shapes(WIRING, "build") == EXPECTED_BUILD_SHAPES

    def test_ledger_projection_call_uses_the_checked_ledger(self) -> None:
        assert call_keyword_shapes(WIRING, "note_from_ledger") == {
            "ledger": ("checked_ledger",),
            "run_id": ("run_id",),
        }

    def test_probe_hardcoded_stage_goes_red(self) -> None:
        variant = _mutate("        stage=stage,", "        stage=LedgerStage.AGENT_MANAGER,")
        shapes = call_keyword_shapes_by_caller(variant, "TelemetryObservation")
        assert shapes != {"record_observation": EXPECTED_OBSERVATION_SHAPES}


# ==========================================================================
# LCS-3 —— 收敛 gate：唯一 mark 点 + 真实事实守卫
# ==========================================================================


class TestLCS3ConvergenceGateIsReadOnly:
    def test_mark_converged_is_called_exactly_once_in_the_builder_gate(self) -> None:
        assert len(calls_to(WIRING, ("mark_converged",))) == 1
        assert set(call_keyword_shapes_by_caller(WIRING, "mark_converged")) == {
            "build_run_summary"
        }

    def test_the_guard_is_the_real_convergence_fact(self) -> None:
        assert ("not routing_run.is_converged", "CostEdgeWiringError") in (
            function_raise_guards(WIRING, "build_run_summary")
        )

    def test_is_converged_is_never_assigned(self) -> None:
        assert "is_converged" not in assignment_value_shapes(WIRING)
        assert "is_converged" not in annotated_assign_names(WIRING)

    def test_probe_dropped_guard_goes_red(self) -> None:
        variant = _mutate(
            "    if not routing_run.is_converged:\n",
            "    if False:\n",
        )
        assert ("not routing_run.is_converged", "CostEdgeWiringError") not in (
            function_raise_guards(variant, "build_run_summary")
        )

    def test_probe_forced_convergence_goes_red(self) -> None:
        variant = _mutate(
            "    checked_builder.mark_converged()",
            "    builder._converged = True",
        )
        assert len(calls_to(variant, ("mark_converged",))) == 0
        assert attribute_assignment_targets(variant) != ()


# ==========================================================================
# LCS-4 —— 不伪造 coverage / 缺失必须返回
# ==========================================================================


class TestLCS4NoFabricatedCoverage:
    def test_edge_constructs_no_cost_contracts(self) -> None:
        assert calls_to(
            WIRING, ("CostUnitSummary", "CostSummary", "CostActual", "CostEstimate")
        ) == []

    def test_note_expected_units_returns_the_unobserved_identity(self) -> None:
        annotation = function_return_annotation(WIRING, "note_expected_units")
        assert annotation == "tuple[tuple[BranchId, int], ...]"

    def test_probe_direct_summary_construction_goes_red(self) -> None:
        variant = _mutate(
            "    checked_builder.mark_converged()",
            "    CostSummary(run_id=run_id, units=(), complete_total_magnitude=None,\n"
            "                confidence=None, notes=())\n"
            "    checked_builder.mark_converged()",
        )
        assert calls_to(variant, ("CostSummary",)) != []


# ==========================================================================
# LCS-5 —— unknown ≠ 0
# ==========================================================================


class TestLCS5UnknownIsNotZero:
    def test_no_none_to_zero_coalescing(self) -> None:
        assert none_coalescing_shapes(WIRING) == ()

    def test_missing_units_are_noted_as_unobserved(self) -> None:
        assert call_keyword_shapes(WIRING, "note_unit") == EXPECTED_NOTE_UNIT_SHAPES

    def test_local_bindings_are_exactly_the_known_set(self) -> None:
        """绑定清单**无盲区**：普通赋值 + **任意深度**的标注赋值一起看（见 `_cost_edge_locks`）。"""
        names = set(assignment_value_shapes(WIRING)) | set(
            annotated_name_targets(WIRING)
        )
        assert names == EXPECTED_BINDINGS
        assert "unobserved" in names  # 函数内 `unobserved: list[...] = []` 必须在视图里

    def test_probe_rebound_local_goes_red(self) -> None:
        variant = _mutate(
            "    noted = set(checked_builder.unit_keys)",
            "    planned: CostEstimate = estimate()\n"
            "    noted = set(checked_builder.unit_keys)",
        )
        names = set(assignment_value_shapes(variant)) | set(
            annotated_name_targets(variant)
        )
        assert names != EXPECTED_BINDINGS

    def test_probe_coalescing_goes_red(self) -> None:
        variant = _mutate(
            "    noted = set(checked_builder.unit_keys)",
            "    noted = set(checked_builder.unit_keys or 0)",
        )
        assert none_coalescing_shapes(variant) != ()

    def test_probe_observed_true_for_missing_goes_red(self) -> None:
        variant = _mutate(
            "            observed=False,",
            "            observed=True,\n            confirmed_cost_exact=0.0,",
        )
        assert call_keyword_shapes(variant, "note_unit") != EXPECTED_NOTE_UNIT_SHAPES


# ==========================================================================
# LCS-6 —— 观测者无控制权
# ==========================================================================


class TestLCS6ObserverHasNoControl:
    def test_no_control_calls(self) -> None:
        assert calls_to(WIRING, CONTROL_CALLS) == []

    def test_no_forbidden_dependency(self) -> None:
        assert forbidden_import_hits(WIRING, FORBIDDEN_IMPORT_TOKENS) == set()

    def test_probe_control_call_goes_red(self) -> None:
        variant = _mutate(
            "from .ledger import CostLedger, TelemetryObservation",
            "from ..agent_manager import AgentManager\n"
            "from .ledger import CostLedger, TelemetryObservation",
        )
        variant = variant.replace(
            "    checked_builder.mark_converged()",
            "    routing_run.run_id and AgentManager(\n"
            "        run_id=routing_run.run_id, branch_order=(), total_attempt_limit=1\n"
            "    )\n    checked_builder.mark_converged()",
            1,
        )
        assert calls_to(variant, ("AgentManager",)) != []
        assert forbidden_import_hits(variant, FORBIDDEN_IMPORT_TOKENS) != set()


# ==========================================================================
# LCS-7 —— 受保护 artifact
# ==========================================================================


# ==========================================================================
# LCS-8 —— 无时钟 / 无状态 / 无 IO
# ==========================================================================


class TestLCS8NoClockNoState:
    def test_no_clock_calls(self) -> None:
        assert clock_call_hits(WIRING) == []

    def test_no_module_state_and_no_io(self) -> None:
        assert module_level_container_assignments(WIRING) == ()
        assert annotated_assign_names(WIRING) == ()
        assert attribute_assignment_targets(WIRING) == ()
        assert open_calls(WIRING) == []

    def test_probe_clock_goes_red(self) -> None:
        variant = _mutate(
            "from dataclasses import dataclass",
            "import time\nfrom dataclasses import dataclass",
        )
        variant = variant.replace(
            "    noted = set(checked_builder.unit_keys)",
            "    noted = set(checked_builder.unit_keys) if time.time() else set()",
            1,
        )
        assert clock_call_hits(variant) != []


# ==========================================================================
# LCS-9 —— 依赖白名单
# ==========================================================================


class TestLCS9ImportWhitelist:
    def test_imported_modules_are_exactly_the_whitelist(self) -> None:
        tokens = imported_module_tokens(WIRING) - FUTURE_TOKENS
        assert tokens == EXPECTED_IMPORT_MODULES | ALLOWED_STDLIB

    def test_imported_names_are_exactly_the_whitelist(self) -> None:
        assert imported_names(WIRING) - FUTURE_TOKENS == EXPECTED_IMPORTED_NAMES

    def test_function_inventory_is_exact_and_kw_only(self) -> None:
        assert module_level_function_names(WIRING) == EXPECTED_FUNCTIONS
        for name in ("record_observation", "note_expected_units", "build_run_summary"):
            shape = function_signature_shape(WIRING, name)
            assert shape["positional"] == [], name
            assert set(shape["defaults"]) <= {False}, name
            assert shape["vararg"] is None and shape["kwarg"] is None, name

    def test_probe_recovery_import_goes_red(self) -> None:
        variant = _mutate(
            "from .summary import CostSummaryBuilder",
            "from .summary import CostSummaryBuilder\nfrom ..recovery import decide_recovery",
        )
        assert imported_module_tokens(variant) - FUTURE_TOKENS != (
            EXPECTED_IMPORT_MODULES | ALLOWED_STDLIB
        )

# ==========================================================================
# 行为测试 B1 – B8
# ==========================================================================


class TestObservations:
    def test_b1_observation_fields_land_in_the_ledger(self) -> None:
        ledger = CostLedger()
        run_id, branch_id = new_run_id(), new_branch_id()
        planned, requested, actual = estimate(), request_value(), actual_magnitude_only()
        entry = record_observation(
            ledger=ledger,
            run_id=run_id,
            branch_id=branch_id,
            stage=LedgerStage.ADAPTER,
            attempt=1,
            planned=planned,
            requested=requested,
            actual=actual,
        )
        assert isinstance(entry, LedgerEntry)
        assert entry.run_id == run_id
        assert entry.branch_id == branch_id
        assert entry.stage is LedgerStage.ADAPTER
        assert entry.attempt == 1
        assert entry.planned is planned
        assert entry.requested is requested
        assert entry.actual is actual
        assert ledger.entries_for(run_id) == (entry,)

    def test_b2_unknown_observation_stays_unknown(self) -> None:
        ledger = CostLedger()
        run_id, branch_id = new_run_id(), new_branch_id()
        entry = record_observation(
            ledger=ledger,
            run_id=run_id,
            branch_id=branch_id,
            stage=LedgerStage.RECOVERY,
            attempt=1,
            planned=estimate(),
            requested=None,
            actual=None,
        )
        assert entry.is_unknown is True
        assert entry.actual is None

    def test_b2b_ledger_is_partitioned_by_run(self) -> None:
        ledger = CostLedger()
        run_a, run_b = new_run_id(), new_run_id()
        branch_id = new_branch_id()
        for run_id in (run_a, run_b):
            record_observation(
                ledger=ledger,
                run_id=run_id,
                branch_id=branch_id,
                stage=LedgerStage.MODEL_ROUTER,
                attempt=1,
                planned=estimate(),
                requested=None,
                actual=actual_magnitude_only(),
            )
        assert len(ledger.entries_for(run_a)) == 1
        assert len(ledger.entries_for(run_b)) == 1

    def test_b2c_guards_are_explicit_failures(self) -> None:
        with pytest.raises(CostEdgeWiringError):
            record_observation(  # type: ignore[arg-type]
                ledger=None,
                run_id=new_run_id(),
                branch_id=new_branch_id(),
                stage=LedgerStage.ADAPTER,
                attempt=1,
                planned=estimate(),
                requested=None,
                actual=None,
            )


class TestCoverageDenominator:
    def test_b3_full_coverage_returns_nothing_unobserved(self) -> None:
        run_id, branch_id = new_run_id(), new_branch_id()
        ledger = CostLedger()
        record_observation(
            ledger=ledger,
            run_id=run_id,
            branch_id=branch_id,
            stage=LedgerStage.ADAPTER,
            attempt=1,
            planned=estimate(),
            requested=None,
            actual=actual_magnitude_only(),
        )
        builder = CostSummaryBuilder(run_id)
        unobserved = note_expected_units(
            builder=builder,
            ledger=ledger,
            run_id=run_id,
            expected_units=(ExpectedUnit(branch_id, 1, estimate()),),
        )
        assert unobserved == ()
        assert builder.unit_count == 1

    def test_b4_missing_units_are_unobserved_and_returned(self) -> None:
        run_id, observed_branch, missing_branch = (
            new_run_id(),
            new_branch_id(),
            new_branch_id(),
        )
        ledger = CostLedger()
        record_observation(
            ledger=ledger,
            run_id=run_id,
            branch_id=observed_branch,
            stage=LedgerStage.ADAPTER,
            attempt=1,
            planned=estimate(),
            requested=None,
            actual=actual_magnitude_only(),
        )
        builder = CostSummaryBuilder(run_id)
        unobserved = note_expected_units(
            builder=builder,
            ledger=ledger,
            run_id=run_id,
            expected_units=(
                ExpectedUnit(observed_branch, 1, estimate()),
                ExpectedUnit(missing_branch, 1, estimate()),
            ),
        )
        assert unobserved == ((missing_branch, 1),)
        assert set(builder.unit_keys) == {(observed_branch, 1), (missing_branch, 1)}

    def test_b4b_ledger_entry_without_actual_is_unobserved(self) -> None:
        run_id, branch_id = new_run_id(), new_branch_id()
        ledger = CostLedger()
        record_observation(
            ledger=ledger,
            run_id=run_id,
            branch_id=branch_id,
            stage=LedgerStage.AGENT_MANAGER,
            attempt=1,
            planned=estimate(),
            requested=None,
            actual=None,
        )
        builder = CostSummaryBuilder(run_id)
        unobserved = note_expected_units(
            builder=builder,
            ledger=ledger,
            run_id=run_id,
            expected_units=(ExpectedUnit(branch_id, 1, estimate()),),
        )
        assert unobserved == ()
        assert builder.unit_count == 1

    def test_b5_observed_unit_is_never_overwritten(self) -> None:
        run_id, branch_id = new_run_id(), new_branch_id()
        ledger = CostLedger()
        record_observation(
            ledger=ledger,
            run_id=run_id,
            branch_id=branch_id,
            stage=LedgerStage.ADAPTER,
            attempt=1,
            planned=estimate(),
            requested=None,
            actual=actual_magnitude_only(),
        )
        builder = CostSummaryBuilder(run_id)
        units = (ExpectedUnit(branch_id, 1, estimate()),)
        assert note_expected_units(
            builder=builder, ledger=ledger, run_id=run_id, expected_units=units
        ) == ()
        # 幂等：再次调用不重复记、也不把已观测覆盖成未观测
        assert note_expected_units(
            builder=builder, ledger=ledger, run_id=run_id, expected_units=units
        ) == ()
        assert builder.unit_count == 1

    def test_b5b_expected_units_must_be_a_tuple_of_expected_unit(self) -> None:
        run_id = new_run_id()
        with pytest.raises(CostEdgeWiringError):
            note_expected_units(
                builder=CostSummaryBuilder(run_id),
                ledger=CostLedger(),
                run_id=run_id,
                expected_units=[],  # type: ignore[arg-type]
            )


class TestConvergenceGate:
    def test_b6_unconverged_run_refuses_and_leaves_the_flag_closed(self) -> None:
        builder = CostSummaryBuilder(new_run_id())
        with pytest.raises(CostEdgeWiringError):
            build_run_summary(
                builder=builder,
                routing_run=zero_branch_run(converged=False),
                **summary_inputs(),  # type: ignore[arg-type]
            )
        assert builder.is_converged is False

    def test_b6b_guards_are_explicit_failures(self) -> None:
        builder = CostSummaryBuilder(new_run_id())
        with pytest.raises(CostEdgeWiringError):
            build_run_summary(
                builder=builder,
                routing_run="nope",  # type: ignore[arg-type]
                **summary_inputs(),  # type: ignore[arg-type]
            )
        with pytest.raises(CostEdgeWiringError):
            build_run_summary(
                builder=None,  # type: ignore[arg-type]
                routing_run=zero_branch_run(converged=True),
                **summary_inputs(),  # type: ignore[arg-type]
            )

    def test_b7_converged_run_yields_a_summary_that_discloses_coverage(self) -> None:
        run_id, observed_branch, missing_branch = (
            new_run_id(),
            new_branch_id(),
            new_branch_id(),
        )
        ledger = CostLedger()
        record_observation(
            ledger=ledger,
            run_id=run_id,
            branch_id=observed_branch,
            stage=LedgerStage.ADAPTER,
            attempt=1,
            planned=estimate(),
            requested=None,
            actual=actual_magnitude_only(),
        )
        builder = CostSummaryBuilder(run_id)
        note_expected_units(
            builder=builder,
            ledger=ledger,
            run_id=run_id,
            expected_units=(
                ExpectedUnit(observed_branch, 1, estimate()),
                ExpectedUnit(missing_branch, 1, estimate()),
            ),
        )
        summary = build_run_summary(
            builder=builder,
            routing_run=zero_branch_run(converged=True),
            **summary_inputs(),  # type: ignore[arg-type]
        )
        assert summary.run_id == run_id
        assert summary.cost_units_total == 2
        assert summary.cost_units_observed == 1
        assert summary.coverage == 0.5
        assert summary.unobserved_unit_keys == ((missing_branch, 1),)
        assert summary.confirmed_cost_exact is None
        assert summary.complete_total_exact is None
        assert any("could not be reliably measured" in note for note in summary.notes)

    def test_b7b_gate_consumes_the_real_convergence_fact(self) -> None:
        """端到端：`[10]` 的 `close_run` 写出真实收敛事实后，gate 才放行。"""
        branch_id = new_branch_id()
        manager = AgentManager(
            run_id=new_run_id(), branch_order=(branch_id,), total_attempt_limit=2
        )
        manager.materialize(
            decisions=(
                DispatchDecision(
                    branch_id=branch_id, action=DispatchAction.START, reason="t"
                ),
            ),
            bundles={branch_id: bundle()},
        )
        manager.commit_started(
            branch_id=branch_id, attempt=1, handle="h", started_at="T0"
        )
        manager.record_outcome(
            branch_id=branch_id,
            attempt=1,
            state=BranchState.COMPLETED,
            ended_at="T1",
        )
        builder = CostSummaryBuilder(manager.routing_run.run_id)
        with pytest.raises(CostEdgeWiringError):
            build_run_summary(
                builder=builder,
                routing_run=manager.routing_run,
                **summary_inputs(),  # type: ignore[arg-type]
            )
        assert builder.is_converged is False

        manager.close_run(
            terminal_state=TerminalState.SUCCESS,
            no_pending_approval=True,
            no_pending_recovery=True,
            ended_at="T2",
        )
        assert manager.routing_run.is_converged is True
        summary = build_run_summary(
            builder=builder,
            routing_run=manager.routing_run,
            **summary_inputs(),  # type: ignore[arg-type]
        )
        assert summary.run_id == manager.routing_run.run_id
        assert summary.cost_units_total == 0

    def test_b8_unknown_is_not_zero(self) -> None:
        # 路径 A：actual=None ⇒ 未观测 ⇒ coverage 0、无任何精确值
        run_a, branch_a = new_run_id(), new_branch_id()
        ledger_a = CostLedger()
        record_observation(
            ledger=ledger_a,
            run_id=run_a,
            branch_id=branch_a,
            stage=LedgerStage.ADAPTER,
            attempt=1,
            planned=estimate(),
            requested=None,
            actual=None,
        )
        builder_a = CostSummaryBuilder(run_a)
        note_expected_units(
            builder=builder_a,
            ledger=ledger_a,
            run_id=run_a,
            expected_units=(ExpectedUnit(branch_a, 1, estimate()),),
        )
        summary_a = build_run_summary(
            builder=builder_a,
            routing_run=zero_branch_run(converged=True),
            **summary_inputs(),  # type: ignore[arg-type]
        )

        # 路径 B：actual = 精确 0.0 ⇒ **观测到** ⇒ coverage 1、已确认成本 = 0.0
        run_b, branch_b = new_run_id(), new_branch_id()
        ledger_b = CostLedger()
        record_observation(
            ledger=ledger_b,
            run_id=run_b,
            branch_id=branch_b,
            stage=LedgerStage.ADAPTER,
            attempt=1,
            planned=estimate(),
            requested=None,
            actual=actual_exact_zero(),
        )
        builder_b = CostSummaryBuilder(run_b)
        note_expected_units(
            builder=builder_b,
            ledger=ledger_b,
            run_id=run_b,
            expected_units=(ExpectedUnit(branch_b, 1, estimate()),),
        )
        summary_b = build_run_summary(
            builder=builder_b,
            routing_run=zero_branch_run(converged=True),
            **summary_inputs(),  # type: ignore[arg-type]
        )

        assert summary_a.coverage == 0.0
        assert summary_b.coverage == 1.0
        assert summary_a.confirmed_cost_exact is None
        assert summary_b.confirmed_cost_exact == 0.0
        assert summary_a.coverage != summary_b.coverage

    def test_b8b_missing_unit_is_never_turned_into_zero(self) -> None:
        """**本边自己那条补记路径**同样不得把"缺失"折成 0（与 B8 的账本路径互补）。"""
        run_id, observed_branch, missing_branch = (
            new_run_id(),
            new_branch_id(),
            new_branch_id(),
        )
        ledger = CostLedger()
        record_observation(
            ledger=ledger,
            run_id=run_id,
            branch_id=observed_branch,
            stage=LedgerStage.ADAPTER,
            attempt=1,
            planned=estimate(),
            requested=None,
            actual=actual_exact_zero(),
        )
        builder = CostSummaryBuilder(run_id)
        unobserved = note_expected_units(
            builder=builder,
            ledger=ledger,
            run_id=run_id,
            expected_units=(
                ExpectedUnit(observed_branch, 1, estimate()),
                ExpectedUnit(missing_branch, 1, estimate()),
            ),
        )
        summary = build_run_summary(
            builder=builder,
            routing_run=zero_branch_run(converged=True),
            **summary_inputs(),  # type: ignore[arg-type]
        )
        assert unobserved == ((missing_branch, 1),)
        assert summary.coverage == 0.5
        assert missing_branch in {key[0] for key in summary.unobserved_unit_keys}
        assert summary.complete_total_exact is None  # coverage<1 ⇒ 不得给完整精确总成本
