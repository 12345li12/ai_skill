"""contracts.common 契约测试。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-EVIDENCE-001  严格区分计划/请求/实际及确认来源        → TestConfirmationSource
  TEST-EVIDENCE-002  未可靠提供时取 UNAVAILABLE              → TestExecutionValueUnobserved
  TEST-EVIDENCE-003  不得把计划或请求值当成实际值            → TestUnknownIsNotZero
  TEST-STATE-001     维护 9 个极简状态（分支态见 runtime）    → TestTerminalState 仅覆盖终止原因
"""

from __future__ import annotations

import dataclasses

import pytest

from adaptive_router.contracts.common import (
    BranchTerminal,
    Confidence,
    ConfirmationSource,
    ContractError,
    ExecutionValue,
    RunTerminal,
    TerminalState,
    new_agent_id,
    new_branch_id,
    new_run_id,
)

# --------------------------------------------------------------------------
# TEST-EVIDENCE-001
# --------------------------------------------------------------------------


class TestConfirmationSource:
    def test_has_exactly_seven_members(self) -> None:
        """架构 6.2.1 冻结的 7 个枚举值，顺序与拼写固定。"""
        assert [m.name for m in ConfirmationSource] == [
            "PLANNER",
            "USER",
            "REQUEST",
            "PROVIDER_API",
            "TOOL_REPORT",
            "INFERRED",
            "UNAVAILABLE",
        ]

    def test_values_are_platform_independent_tokens(self) -> None:
        """架构 6.2.2：领域层只存枚举，不得存展示文案。"""
        for member in ConfirmationSource:
            assert member.value.isascii()
            assert member.value.islower()
            assert " " not in member.value

    def test_no_display_language_leaks_into_domain_enum(self) -> None:
        """中文渲染文案属于 [16] Reporting，不得出现在领域枚举里。"""
        for member in ConfirmationSource:
            assert all(ord(ch) < 128 for ch in member.value), member


# --------------------------------------------------------------------------
# TEST-EVIDENCE-002 / 003
# --------------------------------------------------------------------------


def _valid_unobserved() -> ExecutionValue[str, str, int]:
    return ExecutionValue(
        planned="balanced",
        planned_source=ConfirmationSource.PLANNER,
        requested="gpt-5-codex",
        requested_source=ConfirmationSource.REQUEST,
        actual=None,
        actual_source=ConfirmationSource.UNAVAILABLE,
        observed=False,
        confidence=Confidence.LOW,
    )


def _valid_observed_zero() -> ExecutionValue[str, str, int]:
    """测到 0 —— 完全合法，且必须能与"没测到"区分开。"""
    return ExecutionValue(
        planned="balanced",
        planned_source=ConfirmationSource.PLANNER,
        requested="gpt-5-codex",
        requested_source=ConfirmationSource.REQUEST,
        actual=0,
        actual_source=ConfirmationSource.PROVIDER_API,
        observed=True,
        confidence=Confidence.HIGH,
    )


class TestExecutionValueUnobserved:
    """TEST-EVIDENCE-002：未可靠提供时取 UNAVAILABLE。"""

    def test_unobserved_shape_is_legal(self) -> None:
        value = _valid_unobserved()
        assert value.actual is None
        assert value.observed is False
        assert value.actual_source is ConfirmationSource.UNAVAILABLE
        assert value.actual_confirmed is False

    def test_unobserved_helper_builds_the_frozen_shape(self) -> None:
        value = ExecutionValue[str, str, int].unobserved(
            planned="strong",
            requested="gpt-5-codex",
            planned_source=ConfirmationSource.PLANNER,
            requested_source=ConfirmationSource.REQUEST,
        )
        assert value.actual is None
        assert value.observed is False
        assert value.actual_source is ConfirmationSource.UNAVAILABLE

    def test_unobserved_must_not_carry_a_value(self) -> None:
        with pytest.raises(ContractError, match="actual 必须为 None"):
            ExecutionValue(
                planned="balanced",
                planned_source=ConfirmationSource.PLANNER,
                requested="m",
                requested_source=ConfirmationSource.REQUEST,
                actual=0,
                actual_source=ConfirmationSource.UNAVAILABLE,
                observed=False,
                confidence=Confidence.LOW,
            )

    def test_unobserved_must_declare_unavailable_source(self) -> None:
        with pytest.raises(ContractError, match="actual_source 必须是 UNAVAILABLE"):
            ExecutionValue(
                planned="balanced",
                planned_source=ConfirmationSource.PLANNER,
                requested="m",
                requested_source=ConfirmationSource.REQUEST,
                actual=None,
                actual_source=ConfirmationSource.PROVIDER_API,
                observed=False,
                confidence=Confidence.LOW,
            )

    def test_observed_requires_a_value_even_when_zero(self) -> None:
        with pytest.raises(ContractError, match="observed=True 时 actual 必须给值"):
            ExecutionValue(
                planned="balanced",
                planned_source=ConfirmationSource.PLANNER,
                requested="m",
                requested_source=ConfirmationSource.REQUEST,
                actual=None,
                actual_source=ConfirmationSource.PROVIDER_API,
                observed=True,
                confidence=Confidence.LOW,
            )

    def test_observed_must_not_declare_unavailable_source(self) -> None:
        with pytest.raises(ContractError, match="不得为 UNAVAILABLE"):
            ExecutionValue(
                planned="balanced",
                planned_source=ConfirmationSource.PLANNER,
                requested="m",
                requested_source=ConfirmationSource.REQUEST,
                actual=1,
                actual_source=ConfirmationSource.UNAVAILABLE,
                observed=True,
                confidence=Confidence.LOW,
            )


class TestUnknownIsNotZero:
    """TEST-EVIDENCE-003 / INV-07：unknown ≠ 0，两者永不等价。"""

    def test_zero_observed_and_none_unobserved_are_different_values(self) -> None:
        unknown = _valid_unobserved()
        zero = _valid_observed_zero()
        # 0 是"真的免费"，None 是"不知道" —— 语义反义，必须不等
        assert unknown.actual != zero.actual
        assert unknown != zero
        assert unknown.observed != zero.observed

    def test_zero_observed_is_explicitly_allowed(self) -> None:
        zero = _valid_observed_zero()
        assert zero.actual == 0
        assert zero.observed is True
        assert zero.actual_confirmed is True

    def test_the_two_cannot_be_collapsed_to_a_single_representation(self) -> None:
        """任何"用默认值填坑"的写法都必须在构造期失败。

        反面样例：实际未观测，却用 actual=0 占位。
        """
        with pytest.raises(ContractError):
            ExecutionValue(
                planned="t",
                planned_source=ConfirmationSource.PLANNER,
                requested="m",
                requested_source=ConfirmationSource.REQUEST,
                actual=0,  # ← 用 0 冒充 unknown（L55 禁止的最隐蔽虚构形式）
                actual_source=ConfirmationSource.UNAVAILABLE,
                observed=False,
                confidence=Confidence.LOW,
            )


class TestUnavailableIsNotALazyDefault:
    """第 2 步裁决 3：不允许三个 *_source 同时为 UNAVAILABLE。"""

    def test_planned_source_must_not_be_unavailable(self) -> None:
        with pytest.raises(ContractError, match="planned_source 不得为 UNAVAILABLE"):
            ExecutionValue(
                planned="balanced",
                planned_source=ConfirmationSource.UNAVAILABLE,
                requested="m",
                requested_source=ConfirmationSource.REQUEST,
                actual=None,
                actual_source=ConfirmationSource.UNAVAILABLE,
                observed=False,
                confidence=Confidence.LOW,
            )

    def test_requested_source_must_not_be_unavailable(self) -> None:
        with pytest.raises(ContractError, match="requested_source 不得为 UNAVAILABLE"):
            ExecutionValue(
                planned="balanced",
                planned_source=ConfirmationSource.PLANNER,
                requested="m",
                requested_source=ConfirmationSource.UNAVAILABLE,
                actual=None,
                actual_source=ConfirmationSource.UNAVAILABLE,
                observed=False,
                confidence=Confidence.LOW,
            )

    def test_all_three_unavailable_is_rejected(self) -> None:
        with pytest.raises(ContractError):
            ExecutionValue(
                planned=None,
                planned_source=ConfirmationSource.UNAVAILABLE,
                requested=None,
                requested_source=ConfirmationSource.UNAVAILABLE,
                actual=None,
                actual_source=ConfirmationSource.UNAVAILABLE,
                observed=False,
                confidence=Confidence.LOW,
            )


class TestExecutionValueTypeDiscipline:
    def test_three_values_are_not_forced_to_share_a_type(self) -> None:
        """架构 6.1：TPlan / TRequest / TActual 可以完全不同。"""

        class Tier:
            pass

        class ModelId:
            pass

        class CostActual:
            pass

        value: ExecutionValue[Tier, ModelId, CostActual] = ExecutionValue(
            planned=Tier(),
            planned_source=ConfirmationSource.PLANNER,
            requested=ModelId(),
            requested_source=ConfirmationSource.REQUEST,
            actual=CostActual(),
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.MEDIUM,
        )
        assert isinstance(value.planned, Tier)
        assert isinstance(value.requested, ModelId)
        assert isinstance(value.actual, CostActual)

    def test_is_frozen(self) -> None:
        value = _valid_observed_zero()
        with pytest.raises(dataclasses.FrozenInstanceError):
            value.actual = 99  # type: ignore[misc]

    @pytest.mark.parametrize(
        "field", ["planned_source", "requested_source", "actual_source", "confidence"]
    )
    def test_rejects_wrong_enum_types(self, field: str) -> None:
        kwargs = {
            "planned": "balanced",
            "planned_source": ConfirmationSource.PLANNER,
            "requested": "m",
            "requested_source": ConfirmationSource.REQUEST,
            "actual": None,
            "actual_source": ConfirmationSource.UNAVAILABLE,
            "observed": False,
            "confidence": Confidence.LOW,
        }
        kwargs[field] = "not-an-enum"
        with pytest.raises(ContractError, match=field):
            ExecutionValue(**kwargs)  # type: ignore[arg-type]

    def test_rejects_non_bool_observed(self) -> None:
        with pytest.raises(ContractError, match="observed 必须是 bool"):
            ExecutionValue(
                planned="t",
                planned_source=ConfirmationSource.PLANNER,
                requested="m",
                requested_source=ConfirmationSource.REQUEST,
                actual=None,
                actual_source=ConfirmationSource.UNAVAILABLE,
                observed=0,  # type: ignore[arg-type]
                confidence=Confidence.LOW,
            )


# --------------------------------------------------------------------------
# TEST-STATE-001（终止原因部分）
# --------------------------------------------------------------------------


class TestTerminalState:
    def test_has_exactly_six_members(self) -> None:
        """架构 6.5 + INV-17：终态只能取 6 个枚举值之一。"""
        assert len(TerminalState) == 6

    def test_frozen_spelling(self) -> None:
        assert [m.name for m in TerminalState] == [
            "SUCCESS",
            "USER_CANCELLED",
            "APPROVAL_DENIED",
            "TERMINAL_FAILURE",
            "ABORTED_NO_CAPABILITY",
            "SUPERSEDED",
        ]

    def test_superseded_is_not_a_failure(self) -> None:
        """架构 6.5：被重新规划替代不是失败，否则污染失败率。"""
        assert TerminalState.SUPERSEDED is not TerminalState.TERMINAL_FAILURE


class TestTerminalScopeDiscipline:
    """第 2 步裁决 1：共享枚举，分开终止对象类型。"""

    def test_branch_and_run_terminals_are_distinct_types(self) -> None:
        branch = BranchTerminal(branch_id=new_branch_id(), state=TerminalState.SUCCESS)
        run = RunTerminal(run_id=new_run_id(), state=TerminalState.SUCCESS)
        assert not isinstance(branch, RunTerminal)
        assert not isinstance(run, BranchTerminal)
        assert type(branch) is not type(run)

    def test_a_branch_terminal_cannot_be_used_as_a_run_terminal(self) -> None:
        """类型层阻止 Branch SUCCESS 被当成 RoutingRun SUCCESS。"""
        branch = BranchTerminal(branch_id=new_branch_id(), state=TerminalState.SUCCESS)
        with pytest.raises(ContractError, match="RunId"):
            RunTerminal(run_id=branch.branch_id, state=branch.state)  # type: ignore[arg-type]

    def test_terminal_wrappers_reject_wrong_id_prefixes(self) -> None:
        with pytest.raises(ContractError, match="BranchId"):
            BranchTerminal(branch_id=new_run_id(), state=TerminalState.SUCCESS)
        with pytest.raises(ContractError, match="RunId"):
            RunTerminal(run_id=new_branch_id(), state=TerminalState.SUCCESS)

    def test_terminals_require_a_terminal_state_enum(self) -> None:
        with pytest.raises(ContractError, match="state"):
            BranchTerminal(branch_id=new_branch_id(), state="success")  # type: ignore[arg-type]

    def test_terminals_are_frozen(self) -> None:
        run = RunTerminal(run_id=new_run_id(), state=TerminalState.SUCCESS)
        with pytest.raises(dataclasses.FrozenInstanceError):
            run.state = TerminalState.TERMINAL_FAILURE  # type: ignore[misc]


# --------------------------------------------------------------------------
# ID
# --------------------------------------------------------------------------


class TestIdentifiers:
    def test_generators_are_prefixed_and_unique(self) -> None:
        assert new_run_id().startswith("run_")
        assert new_branch_id().startswith("br_")
        assert new_agent_id().startswith("ag_")
        assert new_run_id() != new_run_id()

    def test_empty_or_unprefixed_values_are_rejected(self) -> None:
        for factory in (RunTerminal,):
            with pytest.raises(ContractError):
                factory(run_id="", state=TerminalState.SUCCESS)  # type: ignore[arg-type]
        with pytest.raises(ContractError):
            BranchTerminal(branch_id="   ", state=TerminalState.SUCCESS)


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------


class TestConfidence:
    def test_three_ordinal_levels_only(self) -> None:
        assert [m.name for m in Confidence] == ["LOW", "MEDIUM", "HIGH"]
