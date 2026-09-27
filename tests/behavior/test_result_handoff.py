"""A15 seam DTO 的契约测试（``result_handoff.py`` 的六个 DTO）。

架构依据：`docs/architecture.md` 2 节 ``[12] Execution Results``（**Amendment A15**，FROZEN）。
范围与形状：`tests/behavior/README_step5b.md` §62.6。

⚠️ 本文件只测 **seam DTO 自身**（形状 / 构造期不变量 / provenance 结构）；
``[12]`` 转换器与 LR 锁在 `test_execution_results.py`。
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from adaptive_router.contracts import (
    Confidence,
    ConfirmationSource,
    EffortLevel,
    ModelTier,
    new_agent_id,
    new_branch_id,
)
from adaptive_router.result_handoff import (
    ActualFacts,
    PlannedFacts,
    ProviderResultPayload,
    RequestedFacts,
    ResultHandoff,
    RuntimeResultPayload,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
HANDOFF_SOURCE = (SRC / "result_handoff.py").read_text(encoding="utf-8")

from _results_locks import (  # noqa: E402  (路径依赖 conftest，放在源码读取之后)
    clock_call_hits,
    dataclass_field_names,
    mapping_get_calls,
    open_calls,
    resolved_symbols,
)


def planned() -> PlannedFacts:
    return PlannedFacts(
        tier=ModelTier.BALANCED,
        effort=EffortLevel.MEDIUM,
        source=ConfirmationSource.PLANNER,
    )


def requested() -> RequestedFacts:
    return RequestedFacts(
        model_id="gpt-5-codex",
        effort=EffortLevel.MEDIUM,
        source=ConfirmationSource.REQUEST,
    )


def actual(*, observed: bool = True) -> ActualFacts:
    if observed:
        return ActualFacts(
            model_id="gpt-5-codex-2025-xx",
            effort=EffortLevel.MEDIUM,
            source=ConfirmationSource.PROVIDER_API,
            confidence=Confidence.HIGH,
            observed=True,
        )
    return ActualFacts(
        model_id=None,
        effort=None,
        source=ConfirmationSource.UNAVAILABLE,
        confidence=Confidence.LOW,
        observed=False,
    )


def payload() -> ProviderResultPayload:
    return ProviderResultPayload(
        provider_status="ok",
        conclusion_summary="done",
        key_evidence=("e1", "e2"),
        residual_risks=(),
        main_agent_todo=("t1",),
    )


def handoff() -> ResultHandoff:
    return ResultHandoff(
        branch_id=new_branch_id(),
        agent_id=new_agent_id(),
        attempt=1,
        planned_fact=planned(),
        requested_fact=requested(),
        actual_fact=actual(),
        provider_payload=payload(),
    )


# ==========================================================================
# 字段形状（LR-12）
# ==========================================================================


class TestFieldShapes:
    def test_result_handoff_has_exactly_seven_fields(self) -> None:
        assert dataclass_field_names(HANDOFF_SOURCE, "ResultHandoff") == (
            "branch_id",
            "agent_id",
            "attempt",
            "planned_fact",
            "requested_fact",
            "actual_fact",
            "provider_payload",
        )

    def test_runtime_result_payload_has_three_fields(self) -> None:
        assert dataclass_field_names(HANDOFF_SOURCE, "RuntimeResultPayload") == (
            "requested",
            "actual",
            "payload",
        )

    def test_planned_facts_shape(self) -> None:
        assert dataclass_field_names(HANDOFF_SOURCE, "PlannedFacts") == (
            "tier",
            "effort",
            "source",
        )

    def test_requested_facts_shape(self) -> None:
        assert dataclass_field_names(HANDOFF_SOURCE, "RequestedFacts") == (
            "model_id",
            "effort",
            "source",
        )

    def test_actual_facts_shape(self) -> None:
        assert dataclass_field_names(HANDOFF_SOURCE, "ActualFacts") == (
            "model_id",
            "effort",
            "source",
            "confidence",
            "observed",
        )

    def test_provider_payload_shape_uses_provider_status(self) -> None:
        """裁定 ⑦：字段名是 **`provider_status`**，**不是** `validation_state`。

        ⚠️ **AST 级判定**：本模块的 docstring 里**故意提及** `validation_state`
        （为了说明"不用它"）⇒ 子串扫描会**假阳性**。
        """
        assert dataclass_field_names(HANDOFF_SOURCE, "ProviderResultPayload") == (
            "provider_status",
            "conclusion_summary",
            "key_evidence",
            "residual_risks",
            "main_agent_todo",
        )
        all_fields: set[str] = set()
        for name in (
            "ResultHandoff",
            "RuntimeResultPayload",
            "PlannedFacts",
            "RequestedFacts",
            "ActualFacts",
            "ProviderResultPayload",
        ):
            all_fields.update(dataclass_field_names(HANDOFF_SOURCE, name))
        assert "validation_state" not in all_fields

    def test_every_dto_is_frozen(self) -> None:
        for value in (planned(), requested(), actual(), payload(), handoff()):
            field_name = next(iter(dataclasses.fields(value))).name
            with pytest.raises(dataclasses.FrozenInstanceError):
                value.__setattr__(field_name, None)


# ==========================================================================
# 构造期不变量
# ==========================================================================


class TestPlannedFacts:
    def test_accepts_valid(self) -> None:
        assert planned().tier is ModelTier.BALANCED

    def test_rejects_unavailable_source(self) -> None:
        """`ExecutionValue` 不变量 3：`planned` 有值 ⇒ source 不得为 `UNAVAILABLE`。"""
        with pytest.raises(ValueError, match="不变量 3"):
            PlannedFacts(
                tier=ModelTier.BALANCED,
                effort=EffortLevel.MEDIUM,
                source=ConfirmationSource.UNAVAILABLE,
            )

    @pytest.mark.parametrize("bad", ["balanced", None, 1])
    def test_rejects_non_tier(self, bad: object) -> None:
        with pytest.raises(TypeError):
            PlannedFacts(
                tier=bad,  # type: ignore[arg-type]
                effort=EffortLevel.MEDIUM,
                source=ConfirmationSource.PLANNER,
            )


class TestRequestedFacts:
    def test_accepts_valid(self) -> None:
        assert requested().model_id == "gpt-5-codex"

    def test_rejects_unavailable_source(self) -> None:
        """`ExecutionValue` 不变量 4。"""
        with pytest.raises(ValueError, match="不变量 4"):
            RequestedFacts(
                model_id="gpt-5-codex",
                effort=EffortLevel.MEDIUM,
                source=ConfirmationSource.UNAVAILABLE,
            )

    @pytest.mark.parametrize("bad", ["", "   ", None, 1])
    def test_rejects_empty_model_id(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            RequestedFacts(
                model_id=bad,  # type: ignore[arg-type]
                effort=EffortLevel.MEDIUM,
                source=ConfirmationSource.REQUEST,
            )

    def test_does_not_duplicate_the_tier_rule(self) -> None:
        """⚠️ "model_id 不得是抽象档位" 这条规则**只有一处实现**（`BranchReturn`）。

        本 DTO 因此**接受** `"balanced"`，由转换期的 `BranchReturn` 拦下 ——
        见 `test_execution_results.py::test_B10_*`。
        """
        assert RequestedFacts(
            model_id="balanced",
            effort=EffortLevel.MEDIUM,
            source=ConfirmationSource.REQUEST,
        ).model_id == "balanced"


class TestActualFacts:
    def test_observed_requires_values_and_a_source(self) -> None:
        assert actual().observed is True
        assert actual().model_id == "gpt-5-codex-2025-xx"

    def test_unobserved_is_explicit(self) -> None:
        """未观测 ⇒ 两个值必须为 `None`、source 必须是 `UNAVAILABLE`（不变量 1）。"""
        unobserved = actual(observed=False)
        assert unobserved.model_id is None and unobserved.effort is None
        assert unobserved.source is ConfirmationSource.UNAVAILABLE

    def test_observed_with_unavailable_source_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="observed=True 时 source 不得为 UNAVAILABLE"):
            ActualFacts(
                model_id="m",
                effort=EffortLevel.MEDIUM,
                source=ConfirmationSource.UNAVAILABLE,
                confidence=Confidence.LOW,
                observed=True,
            )

    def test_observed_with_missing_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="都必须给值"):
            ActualFacts(
                model_id=None,
                effort=EffortLevel.MEDIUM,
                source=ConfirmationSource.PROVIDER_API,
                confidence=Confidence.LOW,
                observed=True,
            )

    def test_unobserved_with_values_is_rejected(self) -> None:
        """未观测**不得携带数值**（否则 0/占位值会冒充 actual）。"""
        with pytest.raises(ValueError, match="必须为 None"):
            ActualFacts(
                model_id="m",
                effort=None,
                source=ConfirmationSource.UNAVAILABLE,
                confidence=Confidence.LOW,
                observed=False,
            )

    def test_unobserved_with_confirmed_source_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="必须是 UNAVAILABLE"):
            ActualFacts(
                model_id=None,
                effort=None,
                source=ConfirmationSource.INFERRED,
                confidence=Confidence.LOW,
                observed=False,
            )

    @pytest.mark.parametrize("bad", [1, 0, "yes", None])
    def test_rejects_non_bool_observed(self, bad: object) -> None:
        with pytest.raises(TypeError, match="observed 必须是 bool"):
            ActualFacts(
                model_id=None,
                effort=None,
                source=ConfirmationSource.UNAVAILABLE,
                confidence=Confidence.LOW,
                observed=bad,  # type: ignore[arg-type]
            )


class TestProviderResultPayload:
    def test_accepts_empty_evidence_and_risks(self) -> None:
        """空证据 / 空风险是**合法输入**（B14）。"""
        empty = ProviderResultPayload(
            provider_status="ok",
            conclusion_summary="done",
            key_evidence=(),
            residual_risks=(),
            main_agent_todo=(),
        )
        assert empty.key_evidence == () and empty.residual_risks == ()

    @pytest.mark.parametrize("field", ["provider_status", "conclusion_summary"])
    def test_rejects_empty_required_strings(self, field: str) -> None:
        base = {
            "provider_status": "ok",
            "conclusion_summary": "done",
            "key_evidence": (),
            "residual_risks": (),
            "main_agent_todo": (),
        }
        base[field] = "   "
        with pytest.raises(ValueError):
            ProviderResultPayload(**base)  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["key_evidence", "residual_risks", "main_agent_todo"])
    def test_rejects_non_tuple(self, field: str) -> None:
        base: dict[str, object] = {
            "provider_status": "ok",
            "conclusion_summary": "done",
            "key_evidence": (),
            "residual_risks": (),
            "main_agent_todo": (),
        }
        base[field] = ["not", "a", "tuple"]
        with pytest.raises(TypeError):
            ProviderResultPayload(**base)  # type: ignore[arg-type]


class TestResultHandoff:
    def test_accepts_valid(self) -> None:
        assert handoff().attempt == 1

    def test_rejects_bad_branch_id(self) -> None:
        with pytest.raises(ValueError):
            ResultHandoff(
                branch_id="nope",
                agent_id=new_agent_id(),
                attempt=1,
                planned_fact=planned(),
                requested_fact=requested(),
                actual_fact=actual(),
                provider_payload=payload(),
            )

    def test_rejects_bad_agent_id(self) -> None:
        with pytest.raises(ValueError):
            ResultHandoff(
                branch_id=new_branch_id(),
                agent_id="nope",
                attempt=1,
                planned_fact=planned(),
                requested_fact=requested(),
                actual_fact=actual(),
                provider_payload=payload(),
            )

    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_bad_attempt(self, bad: int) -> None:
        with pytest.raises(ValueError):
            ResultHandoff(
                branch_id=new_branch_id(),
                agent_id=new_agent_id(),
                attempt=bad,
                planned_fact=planned(),
                requested_fact=requested(),
                actual_fact=actual(),
                provider_payload=payload(),
            )

    def test_rejects_non_fact_types(self) -> None:
        with pytest.raises(TypeError, match="planned_fact"):
            ResultHandoff(
                branch_id=new_branch_id(),
                agent_id=new_agent_id(),
                attempt=1,
                planned_fact="nope",  # type: ignore[arg-type]
                requested_fact=requested(),
                actual_fact=actual(),
                provider_payload=payload(),
            )


# ==========================================================================
# seam DTO 自身的边界（与 LR 系列的静态面互为表里）
# ==========================================================================


class TestSeamBoundaries:
    def test_no_plan_symbols_even_in_this_module(self) -> None:
        """A15.5 / LR-4：整体对象（`RoutingPlan` / `Plan` / `ModelSelection`）不得出现。

        ⚠️ **AST 级判定**：本文件的 docstring 里**故意提及**这些名字，
        子串扫描会假阳性。
        """
        symbols = resolved_symbols(HANDOFF_SOURCE)
        for forbidden in ("RoutingPlan", "Plan", "ModelSelection"):
            assert forbidden not in symbols, forbidden

    def test_no_failure_symbols(self) -> None:
        symbols = resolved_symbols(HANDOFF_SOURCE)
        for forbidden in ("AdapterFailure", "CapabilityMismatch", "AdapterError"):
            assert forbidden not in symbols, forbidden

    def test_no_clock_no_io_no_silent_defaults(self) -> None:
        assert clock_call_hits(HANDOFF_SOURCE) == []
        assert open_calls(HANDOFF_SOURCE) == []
        assert mapping_get_calls(HANDOFF_SOURCE) == []

    def test_runtime_payload_holds_the_three_owned_pieces(self) -> None:
        """`RuntimeResultPayload` 是 `[11]` 的**结果输出**：requested + actual + payload。"""
        bundle = RuntimeResultPayload(
            requested=requested(), actual=actual(), payload=payload()
        )
        assert bundle.requested.model_id == "gpt-5-codex"
        assert bundle.actual.observed is True
        assert bundle.payload.provider_status == "ok"
