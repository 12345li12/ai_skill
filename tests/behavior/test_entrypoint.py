"""entrypoint dispatch 行为测试（Step 4B prerequisites ①）。

架构依据：`docs/architecture.md` v1.0 FROZEN 4.1 节（Session BYPASS 流）

```text
Host / orchestration entry
        ↓
   SessionGate
   ├─ UNASKED   → 只问启用问题，结束
   ├─ DISABLED  → Host / Main Agent direct；**不调用 Router Core**
   └─ ENABLED   → Project Context Loader → Router Core
```

⚠️ 本测试**不**是 Step 4B 的 INV-S1/S2 结构测试 ——
   它验证 composition seam 的**调用图**，为 4B 做准备。
   关键断言使用 **call-count 与调用顺序**，不使用 grep。
"""

from __future__ import annotations

import dataclasses

import pytest

from adaptive_router.contracts import ProjectContext, SessionGateResult
from adaptive_router.entrypoint import (
    EntrypointError,
    EntrypointHandlers,
    dispatch,
)
from adaptive_router.project_context import ProjectContextLoader, ProjectFacts
from adaptive_router.session import SessionGate, SessionState

FIXED_TIME = "2026-09-16T00:00:00Z"


def facts() -> ProjectFacts:
    return ProjectFacts(
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
    )


class Recorder:
    """记录 handler 与下游节点的调用次数与顺序。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def ask_enable(self, reply: object) -> str:
        self.calls.append("ask_enable")
        return "asked"

    def host_direct(self, task: object) -> str:
        self.calls.append("host_direct")
        return "direct"

    def router_core(self, task: object, context: ProjectContext) -> str:
        self.calls.append("router_core")
        return "routed"

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c == name)


@dataclasses.dataclass
class LoaderSpy:
    """包装真实 Loader，记录 load 调用次数与顺序。"""

    inner: ProjectContextLoader
    recorder: Recorder

    def load(self, root: str, *, refresh: bool = False) -> ProjectContext:
        self.recorder.calls.append("loader")
        return self.inner.load(root, refresh=refresh)


def make(recorder: Recorder) -> EntrypointHandlers:
    real_loader = ProjectContextLoader(
        clock=lambda: FIXED_TIME, facts_source=lambda root: facts()
    )
    return EntrypointHandlers(
        session_gate=SessionGate(),
        project_loader=LoaderSpy(inner=real_loader, recorder=recorder),
        ask_enable=recorder.ask_enable,
        host_direct=recorder.host_direct,
        router_core=recorder.router_core,
    )


def run(gate: SessionGate, *, root: str = "/repo", task: object = "do the task"):
    recorder = Recorder()
    handlers = make(recorder)
    handlers.session_gate = gate
    outcome = dispatch(task, project_root=root, handlers=handlers)
    return outcome, recorder


# ==========================================================================
# 直接透传（无包装类型）
# ==========================================================================


class TestDispatchPassesThrough:
    def test_returns_the_ask_handler_outcome_directly(self) -> None:
        outcome, _ = run(SessionGate())
        assert outcome == "asked"

    def test_returns_the_host_direct_outcome_directly(self) -> None:
        outcome, _ = run(SessionGate(state=SessionState.DISABLED))
        assert outcome == "direct"

    def test_returns_the_router_core_outcome_directly(self) -> None:
        outcome, _ = run(SessionGate(state=SessionState.ENABLED))
        assert outcome == "routed"

    def test_no_wrapper_type_is_introduced(self) -> None:
        """DISABLED BYPASS 不得污染结果通道 —— 返回值就是 Host 自己的类型。"""
        import adaptive_router.entrypoint as module

        public = [n for n in dir(module) if not n.startswith("_")]
        for forbidden in ("EntrypointResult", "EntrypointPath", "RouterOutcome"):
            assert forbidden not in public, forbidden

    def test_host_specific_result_type_is_preserved(self) -> None:
        """Host 可以返回自己的任意类型，dispatch 原样透传。"""

        @dataclasses.dataclass
        class HostReply:
            text: str

        reply = HostReply("hello")
        recorder = Recorder()
        handlers = make(recorder)
        handlers.session_gate = SessionGate(state=SessionState.DISABLED)
        handlers.host_direct = lambda task: reply
        assert dispatch("t", project_root="/r", handlers=handlers) is reply


# ==========================================================================
# UNASKED —— 只问启用问题并立即结束
# ==========================================================================


class TestUnaskedPath:
    def test_unasked_call_counts(self) -> None:
        """UNASKED：ask=1 / direct=0 / loader=0 / router_core=0。"""
        _, recorder = run(SessionGate())
        assert recorder.calls == ["ask_enable"]

    def test_unasked_does_not_read_project_context(self) -> None:
        """L5：该回复只含启用问题并立即结束 —— **不读文件**。"""
        _, recorder = run(SessionGate())
        assert recorder.count("loader") == 0

    def test_unasked_never_calls_router_core(self) -> None:
        _, recorder = run(SessionGate())
        assert recorder.count("router_core") == 0

    def test_unasked_marks_the_question_as_asked(self) -> None:
        """只询问一次：**成功发出后**必须记账（L5/L7）。"""
        gate = SessionGate()
        run(gate)
        assert gate.asked is True
        assert gate.may_ask_now() is False

    def test_the_question_is_composed_by_the_gate(self) -> None:
        """询问内容来自 ``SessionGate.compose_first_question()``（L5 冻结形状）。"""
        seen: list[object] = []
        recorder = Recorder()
        handlers = make(recorder)
        handlers.ask_enable = lambda reply: (seen.append(reply), "asked")[1]

        dispatch("t", project_root="/r", handlers=handlers)
        assert len(seen) == 1
        assert seen[0].kind == "question_only"
        assert seen[0].must_end_immediately is True
        assert seen[0].payload == ()
        assert seen[0].side_effects == ()


# ==========================================================================
# UNASKED + 已问过 —— 时间语义：必须 hard-stop，不得走 host_direct
# ==========================================================================


class TestUnaskedTimeSemantics:
    """L5 / TEST-SESSION-003：问过之后，**必须等用户实际发来下一条消息**，
    才能把"没明确回答"解释成 DISABLED。

    ⚠️ 同一 user turn 内异常地二次分派时，安全行为是**继续 hard-stop**：
       不重复提问，也绝不执行 host_direct / loader / Router Core。
    """

    def test_second_dispatch_in_the_same_turn_hard_stops(self) -> None:
        gate = SessionGate()
        run(gate)  # 第一次：提问成功
        recorder = Recorder()
        handlers = make(recorder)
        handlers.session_gate = gate

        with pytest.raises(EntrypointError, match="等待用户的下一条消息"):
            dispatch("t", project_root="/r", handlers=handlers)

        assert recorder.calls == []  # 什么都没执行

    def test_second_dispatch_does_not_go_host_direct(self) -> None:
        """⚠️ 关键：entrypoint **不得**把 ``UNASKED + already_asked``
        自行解释成 HOST_DIRECT —— 那会绕过 SessionGate 的冻结时间语义。"""
        gate = SessionGate()
        run(gate)
        recorder = Recorder()
        handlers = make(recorder)
        handlers.session_gate = gate

        with pytest.raises(EntrypointError):
            dispatch("t", project_root="/r", handlers=handlers)
        assert recorder.count("host_direct") == 0

    def test_second_dispatch_does_not_repeat_the_question(self) -> None:
        gate = SessionGate()
        run(gate)
        recorder = Recorder()
        handlers = make(recorder)
        handlers.session_gate = gate

        with pytest.raises(EntrypointError):
            dispatch("t", project_root="/r", handlers=handlers)
        assert recorder.count("ask_enable") == 0

    def test_real_user_message_lets_the_gate_transition_to_disabled(self) -> None:
        """只有 ``SessionGate.note_user_message()`` 之后才进入 DISABLED 语义。

        这条路径**不属于 entrypoint** —— 它复用 ``session.py`` 的既有接口，
        entrypoint 只读取 gate 的判定结果。
        """
        gate = SessionGate()
        run(gate)  # 提问
        assert gate.evaluate() is SessionGateResult.UNASKED

        gate.note_user_message()  # ← 用户**实际**发来了下一条消息
        assert gate.evaluate() is SessionGateResult.DISABLED

        outcome, recorder = run(gate)
        assert outcome == "direct"
        assert recorder.calls == ["host_direct"]

    def test_entrypoint_does_not_define_its_own_transition_api(self) -> None:
        """不得造第二套 Session DTO / 迁移接口。"""
        import adaptive_router.entrypoint as module

        public = [n for n in dir(module) if not n.startswith("_")]
        for forbidden in (
            "note_user_message",
            "record_decision",
            "GateInput",
            "SessionInput",
            "SessionDTO",
        ):
            assert forbidden not in public, forbidden


# ==========================================================================
# mark_asked 的位置：成功之后才记账
# ==========================================================================


class TestMarkAskedHappensOnlyOnSuccess:
    def test_missing_ask_handler_keeps_asked_false(self) -> None:
        gate = SessionGate()
        handlers = EntrypointHandlers(session_gate=gate)  # 没注入 ask_enable
        with pytest.raises(EntrypointError, match="ask_enable"):
            dispatch("t", project_root="/r", handlers=handlers)
        assert gate.asked is False
        assert gate.may_ask_now() is True  # 仍然可以问

    def test_ask_handler_raising_keeps_asked_false(self) -> None:
        gate = SessionGate()

        def boom(reply: object) -> None:
            raise RuntimeError("transport failed")

        handlers = EntrypointHandlers(session_gate=gate, ask_enable=boom)
        with pytest.raises(RuntimeError, match="transport failed"):
            dispatch("t", project_root="/r", handlers=handlers)
        assert gate.asked is False
        assert gate.may_ask_now() is True

    def test_asked_becomes_true_only_after_a_successful_ask(self) -> None:
        gate = SessionGate()
        recorder = Recorder()
        handlers = make(recorder)
        handlers.session_gate = gate

        assert gate.asked is False
        dispatch("t", project_root="/r", handlers=handlers)
        assert gate.asked is True

    def test_a_failed_ask_can_still_be_retried(self) -> None:
        """失败后仍可重试 —— 不会因为假 ``asked=True`` 而永远不再询问。"""
        gate = SessionGate()
        attempts: list[str] = []

        def flaky(reply: object) -> str:
            attempts.append("try")
            if len(attempts) == 1:
                raise RuntimeError("first attempt fails")
            return "asked"

        handlers = EntrypointHandlers(session_gate=gate, ask_enable=flaky)
        with pytest.raises(RuntimeError):
            dispatch("t", project_root="/r", handlers=handlers)
        assert gate.asked is False
        # 第二次成功
        assert dispatch("t", project_root="/r", handlers=handlers) == "asked"
        assert gate.asked is True
        assert attempts == ["try", "try"]


# ==========================================================================
# DISABLED —— 直接交还 Host，**不进入 Router**
# ==========================================================================


class TestDisabledPath:
    def test_disabled_call_counts(self) -> None:
        """DISABLED：ask=0 / direct=1 / loader=0 / router_core=0。"""
        _, recorder = run(SessionGate(state=SessionState.DISABLED))
        assert recorder.calls == ["host_direct"]

    def test_disabled_never_reads_project_context(self) -> None:
        """INV-S2 的核心：DISABLED **根本不读项目**。"""
        _, recorder = run(SessionGate(state=SessionState.DISABLED))
        assert recorder.count("loader") == 0

    def test_disabled_never_calls_router_core(self) -> None:
        """⚠️ BYPASS 在 Router **外部**：不是"进入 Router 后提前 return"。"""
        _, recorder = run(SessionGate(state=SessionState.DISABLED))
        assert recorder.count("router_core") == 0

    def test_disabled_is_not_a_router_mode(self) -> None:
        """架构 4.1：DISABLED 下"根本不经过任何后续节点，包括 [17]"。"""
        gate = SessionGate(state=SessionState.DISABLED)
        assert gate.evaluate() is SessionGateResult.DISABLED
        assert gate.evaluate().enters_router is False

    def test_missing_host_direct_handler_is_reported(self) -> None:
        handlers = EntrypointHandlers(
            session_gate=SessionGate(state=SessionState.DISABLED)
        )
        with pytest.raises(EntrypointError, match="host_direct"):
            dispatch("t", project_root="/r", handlers=handlers)


# ==========================================================================
# ENABLED —— Project Context Loader → Router Core
# ==========================================================================


class TestEnabledPath:
    def test_enabled_call_counts(self) -> None:
        """ENABLED：ask=0 / direct=0 / loader=1 / router_core=1。"""
        _, recorder = run(SessionGate(state=SessionState.ENABLED))
        assert recorder.calls == ["loader", "router_core"]

    def test_loader_happens_before_router_core(self) -> None:
        """⚠️ 顺序断言：loader 必须在 router_core **之前**发生。"""
        _, recorder = run(SessionGate(state=SessionState.ENABLED))
        assert recorder.calls.index("loader") < recorder.calls.index("router_core")

    def test_router_core_receives_the_project_context(self) -> None:
        seen: list[ProjectContext] = []

        def core(task: object, context: ProjectContext) -> str:
            seen.append(context)
            return "routed"

        recorder = Recorder()
        handlers = make(recorder)
        handlers.router_core = core
        handlers.session_gate = SessionGate(state=SessionState.ENABLED)

        dispatch("t", project_root="/repo", handlers=handlers)
        assert len(seen) == 1
        assert seen[0].root == "/repo"

    def test_project_root_is_passed_to_the_loader(self) -> None:
        seen_roots: list[str] = []

        class RootSpy:
            def __init__(self, recorder: Recorder) -> None:
                self.recorder = recorder

            def load(self, root: str, *, refresh: bool = False) -> ProjectContext:
                seen_roots.append(root)
                self.recorder.calls.append("loader")
                return ProjectContextLoader(
                    clock=lambda: FIXED_TIME, facts_source=lambda r: facts()
                ).load(root)

        recorder = Recorder()
        handlers = make(recorder)
        handlers.project_loader = RootSpy(recorder)  # type: ignore[assignment]
        handlers.session_gate = SessionGate(state=SessionState.ENABLED)

        dispatch("t", project_root="/some/project", handlers=handlers)
        assert seen_roots == ["/some/project"]

    def test_missing_loader_is_reported(self) -> None:
        handlers = EntrypointHandlers(
            session_gate=SessionGate(state=SessionState.ENABLED),
            router_core=lambda task, context: "routed",
        )
        with pytest.raises(EntrypointError, match="project_loader"):
            dispatch("t", project_root="/r", handlers=handlers)

    def test_missing_router_core_is_reported(self) -> None:
        recorder = Recorder()
        handlers = make(recorder)
        handlers.router_core = None
        handlers.session_gate = SessionGate(state=SessionState.ENABLED)
        with pytest.raises(EntrypointError, match="router_core"):
            dispatch("t", project_root="/r", handlers=handlers)

    def test_entrypoint_does_not_judge_r5(self) -> None:
        """refresh 由控制层显式提出；entrypoint 不猜，也不传 refresh。"""
        import inspect

        signature = inspect.signature(dispatch)
        assert "refresh" not in signature.parameters
