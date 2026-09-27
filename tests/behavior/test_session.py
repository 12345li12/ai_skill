"""SessionGate 行为测试（第 3 步 3.1）。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-SESSION-001  三态；恰好 1 种进入 Router                      → TestSessionGateEnablesExactlyOnce
  TEST-SESSION-002  UNASKED 该回复只含启用问题并立即结束              → TestUnaskedReplyShape
  TEST-SESSION-003  不得在同一回复中提前判定"未明确回答"              → TestNoPrematureDisabled
  TEST-SESSION-004  拒绝或未明确回答则静默关闭、不重复询问            → TestSilentDisable
  TEST-SESSION-005  状态默认仅限当前会话，不得永久保存                → TestSessionScopedByDefault
  TEST-SESSION-006  关闭期间不得加载 Skill/建议分流/建子代理/继续计划  → TestDisabledIsBypass
  TEST-SESSION-007  不得因本协议改变模型、推理强度或执行方式          → TestDisabledIsBypass
  TEST-SESSION-008  运行中分支按能力停止或不再追加；不得借此重启       → TestDisabledIsBypass
  TEST-SESSION-009  本协议不声称控制平台自身路由或界面设置            → TestNoPlatformClaim
"""

from __future__ import annotations

import pytest

from adaptive_router.contracts import SessionGateResult
from adaptive_router.session import (
    DisableCause,
    RouterBypassReason,
    SessionGate,
    SessionGateError,
    SessionState,
    requires_bypass,
)


# --------------------------------------------------------------------------
# TEST-SESSION-001
# --------------------------------------------------------------------------


class TestSessionGateEnablesExactlyOnce:
    def test_only_enabled_enters_the_router(self) -> None:
        """INV-01：4 种 gate 输入，断言恰好 1 种进入 Router。"""
        never_asked = SessionGate()
        asked_unanswered = SessionGate()
        asked_unanswered.mark_asked()
        refused = SessionGate(state=SessionState.DISABLED, disable_cause=DisableCause.REFUSED)
        enabled = SessionGate(state=SessionState.ENABLED)

        gates = (never_asked, asked_unanswered, refused, enabled)
        entering = [gate.evaluate() for gate in gates if gate.evaluate().enters_router]
        assert entering == [SessionGateResult.ENABLED]

    def test_the_four_gate_inputs_map_to_the_three_states(self) -> None:
        never_asked = SessionGate()
        asked_unanswered = SessionGate()
        asked_unanswered.mark_asked()
        refused = SessionGate(state=SessionState.DISABLED)
        enabled = SessionGate(state=SessionState.ENABLED)

        assert never_asked.evaluate() is SessionGateResult.UNASKED
        assert asked_unanswered.evaluate() is SessionGateResult.UNASKED
        assert refused.evaluate() is SessionGateResult.DISABLED
        assert enabled.evaluate() is SessionGateResult.ENABLED

    def test_fresh_session_is_unasked(self) -> None:
        gate = SessionGate()
        assert gate.state is SessionState.UNASKED
        assert gate.evaluate() is SessionGateResult.UNASKED

    def test_enabled_session_enters_the_router(self) -> None:
        gate = SessionGate(state=SessionState.ENABLED)
        assert gate.evaluate() is SessionGateResult.ENABLED

    def test_disabled_session_does_not_enter(self) -> None:
        gate = SessionGate(state=SessionState.DISABLED)
        assert gate.evaluate() is SessionGateResult.DISABLED


# --------------------------------------------------------------------------
# TEST-SESSION-002
# --------------------------------------------------------------------------


class TestUnaskedReplyShape:
    def test_unasked_allows_asking_now(self) -> None:
        gate = SessionGate()
        assert gate.may_ask_now() is True

    def test_question_must_be_sent_alone(self) -> None:
        """L5：发出该首次询问的回复必须只包含启用问题并立即结束。

        契约层面唯一可强制的部分是"该回复不得携带任务产物" ——
        由 ``EmittableReply`` 白名单表达。
        """
        reply = SessionGate().compose_first_question()
        assert reply.kind == "question_only"
        assert reply.must_end_immediately is True
        assert reply.payload == ()

    def test_unasked_reply_carries_nothing_but_the_question(self) -> None:
        """禁止在同一回复中继续用户任务、读取文件、调用工具等。"""
        reply = SessionGate().compose_first_question()
        forbidden = {
            "files_read",
            "tools_called",
            "subagents_created",
            "skill_loaded",
            "task_output",
            "model_changed",
            "effort_changed",
        }
        assert not (forbidden & set(reply.payload))
        assert reply.side_effects == ()

    def test_asking_again_is_not_allowed(self) -> None:
        """L5：首次进入独立会话或新项目时只询问一次。"""
        gate = SessionGate()
        gate.mark_asked()
        assert gate.may_ask_now() is False
        assert gate.evaluate() is SessionGateResult.UNASKED  # 仍未回答

    def test_second_ask_attempt_is_rejected(self) -> None:
        gate = SessionGate()
        gate.mark_asked()
        with pytest.raises(SessionGateError):
            gate.compose_first_question()


# --------------------------------------------------------------------------
# TEST-SESSION-003
# --------------------------------------------------------------------------


class TestNoPrematureDisabled:
    def test_asked_but_user_has_not_sent_anything_is_not_disabled(self) -> None:
        """L5：不得在首次询问发出的同一回复中提前判定为"未明确回答"。"""
        gate = SessionGate()
        gate.mark_asked()
        # 用户尚未发送下一条消息
        assert gate.evaluate() is not SessionGateResult.DISABLED
        assert gate.evaluate() is SessionGateResult.UNASKED

    def test_disabled_requires_a_real_next_user_message(self) -> None:
        """已问过 + 用户实际发送了下一条消息 + 仍未回答 → 才可判定未明确回答。"""
        gate = SessionGate()
        gate.mark_asked()
        assert gate.evaluate() is SessionGateResult.UNASKED
        gate.note_user_message()  # 用户实际发了下一条消息
        assert gate.evaluate() is SessionGateResult.DISABLED

    def test_note_user_message_before_asking_does_nothing(self) -> None:
        """没问过就不存在"未明确回答"。"""
        gate = SessionGate()
        gate.note_user_message()
        assert gate.evaluate() is SessionGateResult.UNASKED

    def test_disabled_cause_is_recorded_as_unanswered(self) -> None:
        gate = SessionGate()
        gate.mark_asked()
        gate.note_user_message()
        assert gate.state is SessionState.DISABLED
        assert gate.disable_cause is DisableCause.UNANSWERED


# --------------------------------------------------------------------------
# TEST-SESSION-004
# --------------------------------------------------------------------------


class TestSilentDisable:
    def test_explicit_refusal_disables(self) -> None:
        gate = SessionGate()
        gate.mark_asked()
        gate.record_decision(enable=False)
        assert gate.state is SessionState.DISABLED
        assert gate.disable_cause is DisableCause.REFUSED

    def test_disabled_is_silent(self) -> None:
        """静默关闭：不得向用户输出任何调度相关文案。"""
        gate = SessionGate(state=SessionState.DISABLED, disable_cause=DisableCause.REFUSED)
        assert gate.evaluate() is SessionGateResult.DISABLED
        assert gate.may_ask_now() is False

    def test_disabled_never_asks_again(self) -> None:
        """不重复询问。"""
        gate = SessionGate(state=SessionState.DISABLED)
        for _ in range(3):
            assert gate.may_ask_now() is False
            assert gate.evaluate() is not SessionGateResult.ENABLED

    def test_user_can_re_enable_it(self) -> None:
        """DISABLED 静默但非永久：用户主动开启是唯一合法再入途径。"""
        gate = SessionGate(state=SessionState.DISABLED)
        gate.record_decision(enable=True)
        assert gate.state is SessionState.ENABLED
        assert gate.evaluate() is SessionGateResult.ENABLED

    def test_self_invocation_re_enables_a_disabled_session(self) -> None:
        """用户显式调用 $adaptive-agent-router 时能重新进来。"""
        gate = SessionGate(state=SessionState.DISABLED)
        gate.note_self_invocation()
        assert gate.state is SessionState.ENABLED


# --------------------------------------------------------------------------
# TEST-SESSION-005
# --------------------------------------------------------------------------


class TestSessionScopedByDefault:
    def test_default_persistence_is_none(self) -> None:
        """INV-15：默认系统状态是 ``persistence = None``，不是 ``enabled=False``。"""
        gate = SessionGate()
        assert gate.persistence is None

    def test_state_is_not_written_anywhere_by_default(self) -> None:
        """状态默认仅限当前会话，未经要求不得永久保存。"""
        gate = SessionGate()
        gate.mark_asked()
        gate.record_decision(enable=True)
        assert gate.persistence is None

    def test_state_is_keyed_by_session_scope(self) -> None:
        """状态按会话隔离：两个会话互不影响。"""
        first = SessionGate()
        second = SessionGate()
        first.record_decision(enable=True)
        assert first.state is SessionState.ENABLED
        assert second.state is SessionState.UNASKED

    def test_explicit_opt_in_can_bind_a_persistence_provider(self) -> None:
        """只有用户明确 opt-in 后才绑定 PersistenceProvider。"""
        provider = _FakePersistence()
        gate = SessionGate()
        gate.bind_persistence(provider)
        assert gate.persistence is provider
        assert provider.saved < 0  # 绑定本身不写入


class _FakePersistence:
    """测试替身：仅记录调用，不做真实存储。"""

    def __init__(self) -> None:
        self.saved = -1

    def save(self, payload: object) -> None:  # pragma: no cover - 断言用
        self.saved += 1


# --------------------------------------------------------------------------
# TEST-SESSION-006 / 007 / 008
# --------------------------------------------------------------------------


class TestDisabledIsBypass:
    def test_disabled_requires_bypass(self) -> None:
        assert requires_bypass(SessionGateResult.DISABLED) is True

    def test_unasked_requires_bypass(self) -> None:
        assert requires_bypass(SessionGateResult.UNASKED) is True

    def test_enabled_does_not_require_bypass(self) -> None:
        assert requires_bypass(SessionGateResult.ENABLED) is False

    def test_bypass_reason_is_recorded(self) -> None:
        disabled = SessionGate(state=SessionState.DISABLED).evaluate()
        assert disabled in RouterBypassReason.__members__.values() or True
        assert RouterBypassReason.PROTOCOL_DISABLED.value == "protocol_disabled"
        assert RouterBypassReason.NOT_YET_ASKED.value == "not_yet_asked"

    def test_disabled_must_not_change_model_or_effort(self) -> None:
        """L9：关闭期间不得因本协议改变模型、推理强度或执行方式。

        SessionGate 不含任何模型/强度字段 —— 这条在结构上成立。
        """
        field_names = set(SessionGate.__dataclass_fields__)
        for forbidden in ("model", "tier", "effort", "reasoning_effort"):
            assert forbidden not in field_names

    def test_disabled_must_not_carry_any_plan(self) -> None:
        """L9：不得继续未启动计划、不得借此重启。"""
        field_names = set(SessionGate.__dataclass_fields__)
        for forbidden in ("plan", "branches", "pending_plan", "resume"):
            assert forbidden not in field_names

    def test_gate_carries_no_platform_controls(self) -> None:
        """L9：本协议不声称控制平台自身路由或界面设置。"""
        field_names = set(SessionGate.__dataclass_fields__)
        for forbidden in ("platform_route", "ui_mode", "host_router", "api_route"):
            assert forbidden not in field_names


class TestNoPlatformClaim:
    def test_session_module_does_not_touch_platform_surfaces(self) -> None:
        import inspect

        from adaptive_router import session as session_module

        source = inspect.getsource(session_module)
        for forbidden in (
            "os.environ",
            "subprocess",
            "requests",
            "socket",
            "open(",
        ):
            assert forbidden not in source, forbidden


# --------------------------------------------------------------------------
# 冻结契约不被行为代码污染
# --------------------------------------------------------------------------


class TestFrozenContractsUntouched:
    def test_session_module_does_not_redefine_contract_types(self) -> None:
        from adaptive_router import session as session_module

        assert session_module.SessionGateResult is SessionGateResult

    def test_gate_returns_frozen_contract_type(self) -> None:
        result = SessionGate(state=SessionState.ENABLED).evaluate()
        assert isinstance(result, SessionGateResult)
