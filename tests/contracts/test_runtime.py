"""contracts.runtime 契约测试。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-SESSION-001..009  SessionGate 三态与 BYPASS 前提
  TEST-CAP-001/002/003   能力快照、重查、无真实子代理
  TEST-PROJECT-002/003   Project Context 不可信输入（INV-02）
  TEST-CONTEXT-001/002   最小上下文（INV-11）
  TEST-RETURN-001/002    严格 L37 的 10 项，不得返回思维过程
  TEST-STATE-001         9 个极简状态
  TEST-RECOVERY-002      分支终态 ≠ Run 终态（架构 4.5 硬说明）
  TEST-RISK-002          Adapter 能力不足显式上报（INV-03 / INV-12）
"""

from __future__ import annotations

import dataclasses

import pytest

from adaptive_router.contracts.common import (
    Confidence,
    ConfirmationSource,
    ContractError,
    EffortLevel,
    ExecutionValue,
    ModelTier,
    TerminalState,
    new_agent_id,
    new_branch_id,
    new_run_id,
)
from adaptive_router.contracts.runtime import (
    BRANCH_RETURN_FIELDS,
    AgentRun,
    AuthorizationContext,
    BranchReturn,
    BranchState,
    CapabilityMismatch,
    CapabilitySnapshot,
    ContextBundle,
    ProjectContext,
    RoutingRun,
    SessionGateResult,
    UnavailableReason,
    ValidationRecord,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def make_snapshot(**overrides: object) -> CapabilitySnapshot:
    base: dict[str, object] = dict(
        subagents=True,
        async_background=True,
        concurrency_limit=4,
        available_models=("gpt-5-codex", "gpt-5"),
        available_effort_levels=(EffortLevel.LOW, EffortLevel.HIGH),
        state_wait_append_stop=True,
        hot_switch=False,
        reliable_cost_data=False,
        probed_dimensions=("subagents", "concurrency_limit"),
        unavailable_reason=None,
        probed_at="2026-09-16T00:00:00Z",
    )
    base.update(overrides)
    return CapabilitySnapshot(**base)  # type: ignore[arg-type]


def make_agent_run(
    run_id: str,
    state: BranchState = BranchState.COMPLETED,
    branch_id: str | None = None,
    attempt: int = 1,
) -> AgentRun:
    return AgentRun(
        run_id=run_id,
        branch_id=branch_id or new_branch_id(),
        agent_id=new_agent_id(),
        attempt=attempt,
        state=state,
    )


def make_return(branch_id: str | None = None) -> BranchReturn:
    return BranchReturn(
        branch_id=branch_id or new_branch_id(),
        agent_id=new_agent_id(),
        attempt=1,
        model=ExecutionValue.unobserved(
            planned=ModelTier.BALANCED,
            requested="gpt-5-codex",
            planned_source=ConfirmationSource.PLANNER,
            requested_source=ConfirmationSource.REQUEST,
        ),
        effort=ExecutionValue.unobserved(
            planned=EffortLevel.MEDIUM,
            requested=EffortLevel.MEDIUM,
            planned_source=ConfirmationSource.PLANNER,
            requested_source=ConfirmationSource.REQUEST,
        ),
        conclusion_summary="module A has no side effects",
        key_evidence=("read module_a/__init__.py",),
        validation_state="self-checked",
        residual_risks=("untested on Windows",),
        main_agent_todo=("decide whether to refactor",),
    )


def _return_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = dict(
        branch_id=new_branch_id(),
        agent_id=new_agent_id(),
        attempt=1,
        model=ExecutionValue.unobserved(
            planned=ModelTier.BALANCED,
            requested="gpt-5-codex",
            planned_source=ConfirmationSource.PLANNER,
            requested_source=ConfirmationSource.REQUEST,
        ),
        effort=ExecutionValue.unobserved(
            planned=EffortLevel.MEDIUM,
            requested=EffortLevel.MEDIUM,
            planned_source=ConfirmationSource.PLANNER,
            requested_source=ConfirmationSource.REQUEST,
        ),
        conclusion_summary="summary",
        key_evidence=(),
        validation_state="self-checked",
        residual_risks=(),
        main_agent_todo=(),
    )
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# SessionGate
# --------------------------------------------------------------------------


class TestSessionGate:
    def test_three_states_only(self) -> None:
        assert [m.name for m in SessionGateResult] == [
            "UNASKED",
            "DISABLED",
            "ENABLED",
        ]

    def test_only_enabled_enters_the_router(self) -> None:
        """INV-01：恰好 1 种状态进入 Router 主执行链。"""
        entering = [s for s in SessionGateResult if s.enters_router]
        assert entering == [SessionGateResult.ENABLED]

    def test_disabled_is_a_bypass_not_a_mode(self) -> None:
        """DISABLED 与 UNASKED 都不得进入 Router。"""
        assert SessionGateResult.DISABLED.enters_router is False
        assert SessionGateResult.UNASKED.enters_router is False

    def test_unasked_is_stricter_than_disabled(self) -> None:
        """L5：UNASKED 该条回复只含启用问题并立即结束。"""
        assert SessionGateResult.UNASKED.must_ask_first is True
        assert SessionGateResult.DISABLED.must_ask_first is False


# --------------------------------------------------------------------------
# TEST-CAP-*
# --------------------------------------------------------------------------


class TestCapabilitySnapshot:
    def test_has_eight_capability_dimensions(self) -> None:
        """L13 的 8 项能力。"""
        names = {f.name for f in dataclasses.fields(CapabilitySnapshot)}
        assert {
            "subagents",
            "async_background",
            "concurrency_limit",
            "available_models",
            "available_effort_levels",
            "state_wait_append_stop",
            "hot_switch",
            "reliable_cost_data",
        } <= names

    def test_unknown_requires_a_reason(self) -> None:
        """unknown 是一等状态，但必须能区分"没探"与"探了但失败"。"""
        with pytest.raises(ContractError, match="unavailable_reason"):
            make_snapshot(concurrency_limit=None)

    def test_unknown_with_reason_is_legal(self) -> None:
        snapshot = make_snapshot(
            subagents=None, unavailable_reason=UnavailableReason.TOOL_FAILURE
        )
        assert snapshot.is_unknown is True
        assert snapshot.has_real_subagents is False

    def test_false_is_not_unknown(self) -> None:
        """False 是"确认不支持"，None 是"不知道" —— 不得混用。"""
        snapshot = make_snapshot(
            hot_switch=False, unavailable_reason=UnavailableReason.NOT_PROBED
        )
        assert snapshot.hot_switch is False
        assert snapshot.is_unknown is True  # 因其他维度未知

    def test_rejects_non_bool_capability(self) -> None:
        with pytest.raises(ContractError, match="subagents"):
            make_snapshot(subagents="yes")

    def test_rejects_negative_concurrency(self) -> None:
        with pytest.raises(ContractError, match="不得为负"):
            make_snapshot(concurrency_limit=-1)

    def test_no_real_subagent_snapshot_is_representable(self) -> None:
        """TEST-CAP-003 的前提：能如实表达"没有真实子代理"。"""
        snapshot = make_snapshot(subagents=False)
        assert snapshot.has_real_subagents is False
        assert snapshot.is_unknown is False


# --------------------------------------------------------------------------
# TEST-PROJECT-002 / 003 —— INV-02
# --------------------------------------------------------------------------


class TestProjectContext:
    def test_trust_is_fixed_to_untrusted(self) -> None:
        with pytest.raises(ContractError, match="untrusted_project_input"):
            ProjectContext(
                root="/repo",
                present=True,
                boundary_dirs=(),
                writable=(),
                denied=(),
                readme_refs=(),
                agents_md_present=False,
                agents_md_digest=None,
                test_commands=(),
                high_risk_files=(),
                model_preference=None,
                approval_preference=None,
                source_path=None,
                read_at="2026-09-16T00:00:00Z",
                trust="trusted",
            )

    def test_absent_context_is_legal_and_not_an_error(self) -> None:
        """L53：无配置时输出空 Context，正常工作，不得重建整套系统。"""
        context = ProjectContext.absent("/repo", "2026-09-16T00:00:00Z")
        assert context.present is False
        assert context.agents_md_present is False

    def test_declaring_agents_md_requires_a_digest(self) -> None:
        """来源标记：声称读过 AGENTS.md 就必须留下可核对摘要。"""
        with pytest.raises(ContractError, match="digest"):
            ProjectContext(
                root="/repo",
                present=True,
                boundary_dirs=(),
                writable=(),
                denied=(),
                readme_refs=(),
                agents_md_present=True,
                agents_md_digest=None,
                test_commands=(),
                high_risk_files=(),
                model_preference=None,
                approval_preference=None,
                source_path="AGENTS.md",
                read_at="2026-09-16T00:00:00Z",
            )

    def test_project_preference_is_not_a_decision(self) -> None:
        """架构 2 节：偏好是**输入**给 [02]/[06]，不是选择结果。"""
        field_names = {f.name for f in dataclasses.fields(ProjectContext)}
        assert "model_tier" not in field_names
        assert "requires_approval" not in field_names


class TestAuthorizationContext:
    def test_effective_denied_may_be_stricter_than_higher_layers(self) -> None:
        context = AuthorizationContext(
            session_authorized=True,
            user_granted_scopes=("read",),
            system_denied=("deploy",),
            project_denied=("write",),
            platform_denied=(),
            effective_denied=("deploy", "write"),
            user_read_only_override=False,
            rationale="project narrows further",
        )
        assert "write" in context.effective_denied

    def test_project_must_not_weaken_higher_layer_denials(self) -> None:
        """INV-02：项目只能收窄，不能放宽。"""
        with pytest.raises(ContractError, match="只能收窄"):
            AuthorizationContext(
                session_authorized=True,
                user_granted_scopes=("read",),
                system_denied=("deploy",),
                project_denied=(),
                platform_denied=(),
                effective_denied=(),  # ← 把系统级禁止悄悄放开了
                user_read_only_override=False,
                rationale="project says it is fine",
            )

    def test_project_denied_must_be_honored(self) -> None:
        with pytest.raises(ContractError, match="必须包含 project_denied"):
            AuthorizationContext(
                session_authorized=True,
                user_granted_scopes=("read",),
                system_denied=(),
                project_denied=("write",),
                platform_denied=(),
                effective_denied=(),
                user_read_only_override=False,
                rationale="x",
            )

    def test_read_only_override_is_exposed(self) -> None:
        context = AuthorizationContext(
            session_authorized=True,
            user_granted_scopes=(),
            system_denied=(),
            project_denied=(),
            platform_denied=(),
            effective_denied=(),
            user_read_only_override=True,
            rationale="user said read-only",
        )
        assert context.is_read_only is True


# --------------------------------------------------------------------------
# TEST-CONTEXT-001 / 002 —— INV-11
# --------------------------------------------------------------------------


class TestContextBundle:
    def test_minimal_bundle_is_representable(self) -> None:
        bundle = ContextBundle(
            goal="inspect module A",
            background=("module A handles X",),
            constraints=("no network",),
            allowed_operations=("read",),
            forbidden_operations=("write",),
            output_format="10-field compressed return",
            completion_criteria="report findings with evidence",
            confirmed_conclusions=(),
            excluded=("full session history", "large fixtures"),
            includes_history=False,
        )
        assert bundle.includes_history is False
        assert len(bundle.excluded) == 2

    def test_an_operation_cannot_be_both_allowed_and_forbidden(self) -> None:
        with pytest.raises(ContractError, match="既允许又禁止"):
            ContextBundle(
                goal="g",
                background=(),
                constraints=(),
                allowed_operations=("read", "write"),
                forbidden_operations=("write",),
                output_format="f",
                completion_criteria="c",
                confirmed_conclusions=(),
                excluded=(),
                includes_history=False,
            )


# --------------------------------------------------------------------------
# TEST-STATE-001
# --------------------------------------------------------------------------


class TestBranchState:
    def test_has_exactly_nine_states(self) -> None:
        """L47 的 9 个极简状态。"""
        assert len(BranchState) == 9
        assert [m.name for m in BranchState] == [
            "WAITING",
            "RUNNING",
            "WAITING_INPUT",
            "COMPLETED",
            "FAILED",
            "STOPPED",
            "ADOPTED",
            "REJECTED",
            "OVERRIDDEN",
        ]

    def test_waiting_and_running_are_not_terminal(self) -> None:
        for state in (
            BranchState.WAITING,
            BranchState.RUNNING,
            BranchState.WAITING_INPUT,
        ):
            run = make_agent_run(new_run_id(), state=state)
            assert run.is_terminal is False

    def test_terminal_states_are_detected(self) -> None:
        for state in (
            BranchState.COMPLETED,
            BranchState.FAILED,
            BranchState.STOPPED,
            BranchState.ADOPTED,
            BranchState.REJECTED,
            BranchState.OVERRIDDEN,
        ):
            run = make_agent_run(new_run_id(), state=state)
            assert run.is_terminal is True

    def test_attempt_starts_at_one(self) -> None:
        with pytest.raises(ContractError, match="从 1 开始"):
            make_agent_run(new_run_id(), attempt=0)


# --------------------------------------------------------------------------
# TEST-RECOVERY-002 —— 分支终态 ≠ Run 终态（架构 4.5 硬说明）
# --------------------------------------------------------------------------


class TestRoutingRunScope:
    def test_branch_success_does_not_converge_the_run(self) -> None:
        """架构 4.5：Branch A → SUCCESS 时不得立刻结束整个 RoutingRun。"""
        run_id = new_run_id()
        branches = (
            make_agent_run(run_id, BranchState.COMPLETED),
            make_agent_run(run_id, BranchState.RUNNING),
        )
        run = RoutingRun(
            run_id=run_id,
            branches=branches,
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        assert run.branch_terminals[0].state is TerminalState.SUCCESS
        assert len(run.branch_terminals) == 1  # 未终止的分支不产生终止记录
        assert run.is_converged is False

    def test_run_cannot_reach_terminal_while_branches_are_alive(self) -> None:
        """条件 ①：分支未全终止。"""
        from adaptive_router.contracts.common import RunTerminal

        run_id = new_run_id()
        with pytest.raises(ContractError, match="分支未全部终止"):
            RoutingRun(
                run_id=run_id,
                branches=(
                    make_agent_run(run_id, BranchState.COMPLETED),
                    make_agent_run(run_id, BranchState.WAITING_INPUT),
                ),
                terminal=RunTerminal(run_id=run_id, state=TerminalState.SUCCESS),
                attempt=1,
                total_attempt_limit=2,
                control_flow_converged=True,
            )

    def test_run_cannot_reach_terminal_while_control_flow_is_open(self) -> None:
        """条件 ②（第 2 步审查阻塞 B）：所有分支都终止，但 Approval 仍 WAITING。

        原实现只检查 ①，因此这个构造是合法的 —— 而架构 4.5 明令禁止。
        """
        from adaptive_router.contracts.common import RunTerminal

        run_id = new_run_id()
        with pytest.raises(ContractError, match="控制流未收敛"):
            RoutingRun(
                run_id=run_id,
                branches=(
                    make_agent_run(run_id, BranchState.COMPLETED),
                    make_agent_run(run_id, BranchState.ADOPTED),
                ),
                terminal=RunTerminal(run_id=run_id, state=TerminalState.SUCCESS),
                attempt=1,
                total_attempt_limit=2,
                control_flow_converged=False,  # ← Approval 仍 WAITING
            )

    def test_control_flow_converged_defaults_to_false(self) -> None:
        """不得把"控制流已收敛"默认当作真。"""
        run_id = new_run_id()
        run = RoutingRun(
            run_id=run_id,
            branches=(make_agent_run(run_id, BranchState.COMPLETED),),
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        assert run.control_flow_converged is False

    def test_run_converges_only_when_both_conditions_hold(self) -> None:
        from adaptive_router.contracts.common import RunTerminal

        run_id = new_run_id()
        run = RoutingRun(
            run_id=run_id,
            branches=(
                make_agent_run(run_id, BranchState.COMPLETED),
                make_agent_run(run_id, BranchState.OVERRIDDEN),
            ),
            terminal=RunTerminal(run_id=run_id, state=TerminalState.SUCCESS),
            attempt=1,
            total_attempt_limit=2,
            control_flow_converged=True,
        )
        assert run.all_branches_terminated is True
        assert run.control_flow_converged is True
        assert run.is_converged is True
        states = [t.state for t in run.branch_terminals]
        assert TerminalState.SUPERSEDED in states  # 覆盖不算失败

    def test_control_flow_converged_must_be_a_bool(self) -> None:
        run_id = new_run_id()
        with pytest.raises(ContractError, match="control_flow_converged 必须是 bool"):
            RoutingRun(
                run_id=run_id,
                branches=(make_agent_run(run_id, BranchState.COMPLETED),),
                terminal=None,
                attempt=1,
                total_attempt_limit=2,
                control_flow_converged="yes",  # type: ignore[arg-type]
            )

    def test_branch_from_another_run_is_rejected(self) -> None:
        """成本账不得跨 run 串号。"""
        with pytest.raises(ContractError, match="run_id 与 RoutingRun 不一致"):
            RoutingRun(
                run_id=new_run_id(),
                branches=(make_agent_run(new_run_id(), BranchState.COMPLETED),),
                terminal=None,
                attempt=1,
                total_attempt_limit=2,
            )


# --------------------------------------------------------------------------
# TEST-RETURN-001 / 002 —— 严格 L37
# --------------------------------------------------------------------------


class TestBranchReturn:
    def test_shape_is_exactly_the_frozen_ten_fields(self) -> None:
        """TEST-RETURN-001：多一个字段都不许。"""
        BranchReturn.assert_shape_is_frozen()
        assert len(BRANCH_RETURN_FIELDS) == 10

    def test_no_thinking_process_field_exists(self) -> None:
        """TEST-RETURN-002：不得返回完整思维过程或重复背景。"""
        names = set(BRANCH_RETURN_FIELDS) | set(BranchReturn.__dataclass_fields__)
        for forbidden in (
            "thinking",
            "reasoning_trace",
            "chain_of_thought",
            "full_context",
            "background",
        ):
            assert forbidden not in names

    def test_carries_plan_request_actual_for_model_and_effort(self) -> None:
        """L37：模型与推理强度都要带"计划/请求/实际 + 确认来源"。"""
        result = make_return()
        assert isinstance(result.model, ExecutionValue)
        assert isinstance(result.effort, ExecutionValue)
        assert result.model.observed is False
        assert result.model.actual_source is ConfirmationSource.UNAVAILABLE

    def test_unconfirmed_actual_is_not_backfilled(self) -> None:
        result = make_return()
        assert result.model.actual is None
        assert result.effort.actual is None

    def test_empty_summary_is_rejected(self) -> None:
        with pytest.raises(ContractError, match="conclusion_summary"):
            BranchReturn(**_return_kwargs(conclusion_summary=" "))  # type: ignore[arg-type]


class TestBranchReturnEnforcesInv04:
    """阻塞 A：INV-04 必须在 BranchReturn 构造期真正强制。

    ``ExecutionValue[ModelTier, str, str]`` 的泛型标注在运行时**不做检查**，
    所以"计划档位伪装成实际模型"只能在这里拦。
    """

    def test_planned_must_be_a_model_tier(self) -> None:
        model = ExecutionValue(
            planned="balanced",  # type: ignore[arg-type]  ← 字符串冒充档位
            planned_source=ConfirmationSource.PLANNER,
            requested="gpt-5-codex",
            requested_source=ConfirmationSource.REQUEST,
            actual=None,
            actual_source=ConfirmationSource.UNAVAILABLE,
            observed=False,
            confidence=Confidence.LOW,
        )
        with pytest.raises(ContractError, match="model.planned 必须是 ModelTier"):
            BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]

    def test_requested_must_not_be_an_abstract_tier(self) -> None:
        model = ExecutionValue.unobserved(
            planned=ModelTier.STRONG,
            requested="strong",  # ← 请求的是档位而不是 model ID
            planned_source=ConfirmationSource.PLANNER,
            requested_source=ConfirmationSource.REQUEST,
        )
        with pytest.raises(ContractError, match="model.requested 不得是抽象档位"):
            BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]

    def test_requested_must_not_be_empty(self) -> None:
        model = ExecutionValue.unobserved(
            planned=ModelTier.STRONG,
            requested="",
            planned_source=ConfirmationSource.PLANNER,
            requested_source=ConfirmationSource.REQUEST,
        )
        with pytest.raises(ContractError, match="model.requested"):
            BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]

    def test_actual_must_not_be_an_abstract_tier(self) -> None:
        """INV-04 的核心场景：planned 档位被填进 actual 位。

        注意这个 ExecutionValue 本身是**合法**的 —— 只有这样才说明
        光靠 ExecutionValue 拦不住，必须由 BranchReturn 的领域检查兜住。
        """
        model = ExecutionValue(
            planned=ModelTier.STRONG,
            planned_source=ConfirmationSource.PLANNER,
            requested="gpt-5-codex",
            requested_source=ConfirmationSource.REQUEST,
            actual="strong",  # ← 把计划档位当成"实际模型"
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        )
        assert model.observed is True  # ExecutionValue 自己不拦
        with pytest.raises(ContractError, match="model.actual 不得是抽象档位"):
            BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "disguise", ["strong", "STRONG", "Strong", "  strong  ", "\tSTRONG\n"]
    )
    def test_actual_tier_disguise_is_normalized_before_comparison(
        self, disguise: str
    ) -> None:
        """大小写与空白变体必须同样被拦下（最后三项之 ①）。

        比较用 ``strip().lower()`` 规范化副本；**真正保存的值不被修改**。
        """
        model = ExecutionValue(
            planned=ModelTier.STRONG,
            planned_source=ConfirmationSource.PLANNER,
            requested="gpt-5-codex",
            requested_source=ConfirmationSource.REQUEST,
            actual=disguise,
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        )
        with pytest.raises(ContractError, match="model.actual 不得是抽象档位"):
            BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "disguise",
        ["strong", "STRONG", " Strong ", "low_cost", "LOW_COST", "MAXIMUM"],
    )
    def test_requested_tier_disguise_is_normalized_before_comparison(
        self, disguise: str
    ) -> None:
        model = ExecutionValue.unobserved(
            planned=ModelTier.BALANCED,
            requested=disguise,
            planned_source=ConfirmationSource.PLANNER,
            requested_source=ConfirmationSource.REQUEST,
        )
        with pytest.raises(ContractError, match="model.requested 不得是抽象档位"):
            BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]

    def test_normalization_does_not_rewrite_the_stored_value(self) -> None:
        """规范化**只用于比较副本**，不得改动真正保存的 provider model ID。"""
        original = "  gpt-5-codex-Prod  "
        model = ExecutionValue(
            planned=ModelTier.STRONG,
            planned_source=ConfirmationSource.PLANNER,
            requested="gpt-5-codex",
            requested_source=ConfirmationSource.REQUEST,
            actual=original,
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        )
        result = BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]
        assert result.model.actual == original  # 原样保留

    def test_a_real_model_id_containing_a_tier_word_is_still_accepted(self) -> None:
        """规范化后只做**全等**比较：真实 model ID 里含档位词不算伪装。"""
        model = ExecutionValue(
            planned=ModelTier.STRONG,
            planned_source=ConfirmationSource.PLANNER,
            requested="gpt-5-codex",
            requested_source=ConfirmationSource.REQUEST,
            actual="strong-model-v2",  # 不等于 "strong"
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        )
        result = BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]
        assert result.model.actual == "strong-model-v2"

    def test_actual_must_not_be_empty_when_observed(self) -> None:
        model = ExecutionValue(
            planned=ModelTier.STRONG,
            planned_source=ConfirmationSource.PLANNER,
            requested="gpt-5-codex",
            requested_source=ConfirmationSource.REQUEST,
            actual="",
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        )
        with pytest.raises(ContractError, match="model.actual"):
            BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]

    def test_real_provider_model_id_is_accepted(self) -> None:
        model = ExecutionValue(
            planned=ModelTier.STRONG,
            planned_source=ConfirmationSource.PLANNER,
            requested="gpt-5-codex",
            requested_source=ConfirmationSource.REQUEST,
            actual="gpt-5-codex-2025-11",
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        )
        result = BranchReturn(**_return_kwargs(model=model))  # type: ignore[arg-type]
        assert result.model.actual == "gpt-5-codex-2025-11"

    @pytest.mark.parametrize("field", ["planned", "requested"])
    def test_effort_values_must_be_effort_levels(self, field: str) -> None:
        kwargs: dict[str, object] = {
            "planned": EffortLevel.MEDIUM,
            "planned_source": ConfirmationSource.PLANNER,
            "requested": EffortLevel.MEDIUM,
            "requested_source": ConfirmationSource.REQUEST,
            "actual": None,
            "actual_source": ConfirmationSource.UNAVAILABLE,
            "observed": False,
            "confidence": Confidence.LOW,
        }
        kwargs[field] = "medium"  # 字符串冒充强度
        effort = ExecutionValue(**kwargs)  # type: ignore[arg-type]
        with pytest.raises(ContractError, match=f"effort.{field} 必须是 EffortLevel"):
            BranchReturn(**_return_kwargs(effort=effort))  # type: ignore[arg-type]

    def test_effort_actual_must_be_an_effort_level_when_observed(self) -> None:
        effort = ExecutionValue(
            planned=EffortLevel.MEDIUM,
            planned_source=ConfirmationSource.PLANNER,
            requested=EffortLevel.MEDIUM,
            requested_source=ConfirmationSource.REQUEST,
            actual="high",  # type: ignore[arg-type]  ← 字符串冒充强度
            actual_source=ConfirmationSource.PROVIDER_API,
            observed=True,
            confidence=Confidence.HIGH,
        )
        with pytest.raises(ContractError, match="effort.actual 已观测时必须是"):
            BranchReturn(**_return_kwargs(effort=effort))  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# [13] Validation
# --------------------------------------------------------------------------


class TestValidationRecord:
    def test_pass_must_not_carry_failed_criteria(self) -> None:
        with pytest.raises(ContractError, match="不得携带 failed_criteria"):
            ValidationRecord(
                branch_id=new_branch_id(),
                passed=True,
                evidence=("evidence",),
                failed_criteria=("criteria A",),
                detail="ok",
            )

    def test_fail_must_name_the_unmet_criteria(self) -> None:
        """[14] Recovery 需要完成标准问题作为分类输入（L49）。"""
        with pytest.raises(ContractError, match="必须指出未满足的完成标准"):
            ValidationRecord(
                branch_id=new_branch_id(),
                passed=False,
                evidence=(),
                failed_criteria=(),
                detail="something went wrong",
            )

    def test_valid_pass_and_fail(self) -> None:
        ok = ValidationRecord(
            branch_id=new_branch_id(),
            passed=True,
            evidence=("evidence",),
            failed_criteria=(),
            detail="all criteria met",
        )
        assert ok.passed is True
        bad = ValidationRecord(
            branch_id=new_branch_id(),
            passed=False,
            evidence=("evidence",),
            failed_criteria=("criteria A",),
            detail="criteria A unmet",
        )
        assert bad.failed_criteria == ("criteria A",)


# --------------------------------------------------------------------------
# TEST-RISK-002 —— INV-03 / INV-12
# --------------------------------------------------------------------------


class TestCapabilityMismatch:
    def test_reports_requested_supported_and_alternatives(self) -> None:
        mismatch = CapabilityMismatch(
            requested="strong/high",
            supported="balanced/medium",
            alternatives=("balanced/medium", "strong/medium"),
            detail="platform does not expose high effort",
        )
        assert mismatch.has_alternatives is True

    def test_reporting_only_does_not_carry_a_decision(self) -> None:
        """INV-03：Adapter 不得自行降级 —— 契约里没有"选哪个"的字段。"""
        field_names = {f.name for f in dataclasses.fields(CapabilityMismatch)}
        assert "selected" not in field_names
        assert "downgraded_to" not in field_names
        assert "plan" not in field_names
        assert "routing_plan" not in field_names

    def test_mismatch_may_have_no_alternatives(self) -> None:
        mismatch = CapabilityMismatch(
            requested="strong/high",
            supported="none",
            alternatives=(),
            detail="platform cannot run agents at all",
        )
        assert mismatch.has_alternatives is False
        # 是否启用单代理阶段化由 Core 决定（L17），不由 Adapter 决定


# --------------------------------------------------------------------------
# Amendment A13.3 —— `RoutingRun.branches` 是 attempt history
# --------------------------------------------------------------------------


class TestAttemptHistorySemantics:
    """L16–L18：A13.3 把"新 attempt 只能在前一 attempt 终止后产生"从**文字**
    提升为**构造期不变量**，并订正两处读取语义（最高 attempt / 每 branch 一条终止记录）。
    """

    def test_L17_branch_by_id_returns_highest_attempt(self) -> None:
        run_id = new_run_id()
        branch_id = new_branch_id()
        run = RoutingRun(
            run_id=run_id,
            branches=(
                make_agent_run(run_id, BranchState.FAILED, branch_id=branch_id, attempt=1),
                make_agent_run(run_id, BranchState.COMPLETED, branch_id=branch_id, attempt=2),
            ),
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        assert run.branch_by_id(branch_id).attempt == 2
        assert run.branch_by_id(branch_id).state is BranchState.COMPLETED

    def test_L17_branch_by_id_ignores_other_branches(self) -> None:
        run_id = new_run_id()
        wanted, other = new_branch_id(), new_branch_id()
        run = RoutingRun(
            run_id=run_id,
            branches=(
                make_agent_run(run_id, BranchState.COMPLETED, branch_id=other, attempt=1),
                make_agent_run(run_id, BranchState.RUNNING, branch_id=wanted, attempt=1),
            ),
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        assert run.branch_by_id(wanted).state is BranchState.RUNNING

    def test_L17_unknown_branch_still_raises(self) -> None:
        run_id = new_run_id()
        run = RoutingRun(
            run_id=run_id,
            branches=(make_agent_run(run_id, BranchState.COMPLETED),),
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        with pytest.raises(ContractError, match="未知分支"):
            run.branch_by_id(new_branch_id())

    def test_L16_branch_terminals_has_at_most_one_record_per_branch(self) -> None:
        """旧实现会为同一 branch 的**每个**已终止 attempt 各出一条记录。"""
        run_id = new_run_id()
        branch_id = new_branch_id()
        run = RoutingRun(
            run_id=run_id,
            branches=(
                make_agent_run(run_id, BranchState.FAILED, branch_id=branch_id, attempt=1),
                make_agent_run(run_id, BranchState.COMPLETED, branch_id=branch_id, attempt=2),
            ),
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        terminals = run.branch_terminals
        assert len(terminals) == 1
        assert terminals[0].branch_id == branch_id
        assert terminals[0].state is TerminalState.SUCCESS  # 取**最新** attempt 的终态

    def test_L16_latest_failed_attempt_wins_over_earlier_success(self) -> None:
        run_id = new_run_id()
        branch_id = new_branch_id()
        run = RoutingRun(
            run_id=run_id,
            branches=(
                make_agent_run(run_id, BranchState.COMPLETED, branch_id=branch_id, attempt=1),
                make_agent_run(run_id, BranchState.FAILED, branch_id=branch_id, attempt=2),
            ),
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        terminals = run.branch_terminals
        assert len(terminals) == 1
        assert terminals[0].state is TerminalState.TERMINAL_FAILURE

    def test_L16_unterminated_latest_attempt_yields_no_record(self) -> None:
        run_id = new_run_id()
        branch_id = new_branch_id()
        run = RoutingRun(
            run_id=run_id,
            branches=(
                make_agent_run(run_id, BranchState.FAILED, branch_id=branch_id, attempt=1),
                make_agent_run(run_id, BranchState.RUNNING, branch_id=branch_id, attempt=2),
            ),
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        assert run.branch_terminals == ()
        assert run.all_branches_terminated is False

    def test_L18_duplicate_branch_attempt_is_rejected(self) -> None:
        run_id = new_run_id()
        branch_id = new_branch_id()
        with pytest.raises(ContractError, match="不得在 branches 内重复"):
            RoutingRun(
                run_id=run_id,
                branches=(
                    make_agent_run(run_id, BranchState.COMPLETED, branch_id=branch_id, attempt=1),
                    make_agent_run(run_id, BranchState.FAILED, branch_id=branch_id, attempt=1),
                ),
                terminal=None,
                attempt=1,
                total_attempt_limit=2,
            )

    def test_L18_higher_attempt_requires_previous_terminated(self) -> None:
        """**新 attempt 只能在前一 attempt 已终止后产生** —— 否则运行态会永久悬挂。"""
        run_id = new_run_id()
        branch_id = new_branch_id()
        with pytest.raises(ContractError, match="不得存在更高的 attempt"):
            RoutingRun(
                run_id=run_id,
                branches=(
                    make_agent_run(run_id, BranchState.RUNNING, branch_id=branch_id, attempt=1),
                    make_agent_run(run_id, BranchState.WAITING, branch_id=branch_id, attempt=2),
                ),
                terminal=None,
                attempt=1,
                total_attempt_limit=2,
            )

    def test_L18_second_attempt_after_terminal_is_allowed(self) -> None:
        run_id = new_run_id()
        branch_id = new_branch_id()
        run = RoutingRun(
            run_id=run_id,
            branches=(
                make_agent_run(run_id, BranchState.FAILED, branch_id=branch_id, attempt=1),
                make_agent_run(run_id, BranchState.RUNNING, branch_id=branch_id, attempt=2),
            ),
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        assert len(run.branches) == 2  # attempt history 两条都在
        assert run.is_converged is False

    def test_L18_error_message_names_the_branch(self) -> None:
        run_id = new_run_id()
        branch_id = new_branch_id()
        with pytest.raises(ContractError, match=branch_id):
            RoutingRun(
                run_id=run_id,
                branches=(
                    make_agent_run(run_id, BranchState.RUNNING, branch_id=branch_id, attempt=1),
                    make_agent_run(run_id, BranchState.COMPLETED, branch_id=branch_id, attempt=2),
                ),
                terminal=None,
                attempt=1,
                total_attempt_limit=2,
            )

    def test_single_attempt_runs_keep_old_behavior(self) -> None:
        """回归护栏：单 attempt（V1 现状）语义**逐位不变**。"""
        run_id = new_run_id()
        branches = (
            make_agent_run(run_id, BranchState.COMPLETED),
            make_agent_run(run_id, BranchState.RUNNING),
        )
        run = RoutingRun(
            run_id=run_id,
            branches=branches,
            terminal=None,
            attempt=1,
            total_attempt_limit=2,
        )
        assert len(run.branch_terminals) == 1
        assert run.branch_terminals[0].state is TerminalState.SUCCESS
        assert run.branch_by_id(branches[0].branch_id).state is BranchState.COMPLETED
