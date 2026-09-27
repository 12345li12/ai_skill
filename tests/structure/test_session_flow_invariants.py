"""Step 4B — Structural CI：INV-S1 / INV-S2 的**真实运行路径**强制。

架构依据：`docs/architecture.md` v1.0 FROZEN 第 7.2 节

    INV-S1  Router 主执行链**不得自行解释 Session 状态**；必须先调用唯一
            SessionGate，只有其返回 ENABLED 才允许进入 Project Context 和
            Router Core。**检查调用/依赖路径** —— 不是 grep ``enabled``。

    INV-S2  SessionGate 未返回 ENABLED 时，**不得执行** Project Context 的
            读取、探测或加载函数。**单元测试 + mock call count** —— 比
            import 分析更可靠。

⚠️ 冻结架构对这两条**明确禁止 grep 方案**。因此本文件的全部证据来自
   **真实 ``dispatch()`` 路径上的 spy / call-count / 调用顺序**：
   不扫描源码字符串，不做 import/package-purity 断言，
   不断言 ``router.py`` 是否为空或某模块今天是否存在。

⚠️ 本文件**不覆盖** INV-S3 / S4 / S5 —— 见 ``test_invariants.py``。

状态见 ``tests/structure/README.md``。
"""

from __future__ import annotations

from typing import Any

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


# ==========================================================================
# Spies —— 真实运行路径上的观测点
# ==========================================================================


class GateSpy:
    """给真实 ``SessionGate`` 加 spy：记录 ``evaluate()`` 与其它控制动作。

    ⚠️ **不改变 gate 行为** —— 只记录，然后委托给内层真实 gate。
       因此被观测的是真实路径，不是替身逻辑。
    """

    def __init__(self, inner: SessionGate, timeline: list[str]) -> None:
        self._inner = inner
        self._timeline = timeline
        self.evaluate_calls = 0
        self.may_ask_calls = 0
        self.mark_asked_calls = 0
        self.compose_calls = 0

    # --- 被 entrypoint 使用的协议面 -----------------------------------
    def evaluate(self) -> SessionGateResult:
        self.evaluate_calls += 1
        self._timeline.append("gate.evaluate")
        return self._inner.evaluate()

    def may_ask_now(self) -> bool:
        self.may_ask_calls += 1
        self._timeline.append("gate.may_ask_now")
        return self._inner.may_ask_now()

    def compose_first_question(self):  # type: ignore[no-untyped-def]
        self.compose_calls += 1
        self._timeline.append("gate.compose_first_question")
        return self._inner.compose_first_question()

    def mark_asked(self) -> None:
        self.mark_asked_calls += 1
        self._timeline.append("gate.mark_asked")
        self._inner.mark_asked()

    # --- 透传，供测试构造与断言 ---------------------------------------
    @property
    def inner(self) -> SessionGate:
        return self._inner

    def note_user_message(self) -> None:
        """测试驱动用：模拟用户**实际**发来下一条消息（转发给真实 gate）。"""
        self._inner.note_user_message()


class LoaderSpy:
    """包装真实 ``ProjectContextLoader``：记录 ``load()`` 的 call count。

    ⚠️ 委托给**真实** loader（含其缓存行为），不是纯替身。
    """

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.inner = ProjectContextLoader(
            clock=lambda: FIXED_TIME, facts_source=_facts
        )
        self.call_count = 0
        self.roots: list[str] = []
        self.refreshes: list[bool] = []
        self.last_returned: ProjectContext | None = None

    def load(self, root: str, *, refresh: bool = False) -> ProjectContext:
        self.call_count += 1
        self.roots.append(root)
        self.refreshes.append(refresh)
        self._timeline.append("loader.load")
        self.last_returned = self.inner.load(root, refresh=refresh)
        return self.last_returned


class CoreSpy:
    """记录 Router Core 的调用与它收到的实参。"""

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.call_count = 0
        self.tasks: list[Any] = []
        self.contexts: list[ProjectContext] = []

    def __call__(self, task: Any, context: ProjectContext) -> str:
        self.call_count += 1
        self.tasks.append(task)
        self.contexts.append(context)
        self._timeline.append("router_core")
        return "routed"


class AskSpy:
    """记录 ask-enable handler 的调用。"""

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.call_count = 0
        self.replies: list[Any] = []

    def __call__(self, reply: Any) -> str:
        self.call_count += 1
        self.replies.append(reply)
        self._timeline.append("ask_enable")
        return "asked"


class HostSpy:
    """记录 host-direct handler 的调用。"""

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.call_count = 0
        self.tasks: list[Any] = []

    def __call__(self, task: Any) -> str:
        self.call_count += 1
        self.tasks.append(task)
        self._timeline.append("host_direct")
        return "direct"


def _facts(root: str) -> ProjectFacts:
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


class Runtime:
    """一次真实 ``dispatch()`` 的全部观测点。"""

    def __init__(self, gate: SessionGate) -> None:
        self.timeline: list[str] = []
        self.gate = GateSpy(gate, self.timeline)
        self.loader = LoaderSpy(self.timeline)
        self.core = CoreSpy(self.timeline)
        self.ask = AskSpy(self.timeline)
        self.host = HostSpy(self.timeline)
        self.handlers = EntrypointHandlers(
            session_gate=self.gate,  # type: ignore[arg-type]
            project_loader=self.loader,
            ask_enable=self.ask,
            host_direct=self.host,
            router_core=self.core,
        )

    def dispatch(self, task: Any = "task", *, root: str = "/repo") -> Any:
        self.task = task
        return dispatch(task, project_root=root, handlers=self.handlers)

    def index(self, event: str) -> int:
        return self.timeline.index(event)


# ==========================================================================
# INV-S1 —— 唯一 SessionGate 最先，且只有 ENABLED 到达 Router Core
# ==========================================================================


class TestInvS1SingleGateFirst:
    def test_gate_is_evaluated_exactly_once_per_dispatch(self) -> None:
        """每次 dispatch 只经过**注入的这一个** Gate。

        ⚠️ 本测试只统计注入的 ``GateSpy``。若 entrypoint 内部另建一个
           ``SessionGate`` 并 evaluate，那个实例**不会**经过 GateSpy，
           本测试仍会通过 —— 因此它**不足以**单独证明"唯一 SessionGate"。
           该缺口由 ``TestInvS1ExactlyOneRealGate`` 用 monkeypatch 补上。
        """
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        assert rt.gate.evaluate_calls == 1

    @pytest.mark.parametrize(
        "state",
        [SessionState.UNASKED, SessionState.DISABLED, SessionState.ENABLED],
    )
    def test_gate_evaluated_exactly_once_for_every_state(
        self, state: SessionState
    ) -> None:
        rt = Runtime(SessionGate(state=state))
        try:
            rt.dispatch()
        except EntrypointError:
            pass  # UNASKED + 已问过 会 hard-stop，仍应只判定一次
        assert rt.gate.evaluate_calls == 1

    def test_gate_is_the_first_control_action(self) -> None:
        """Gate 必须是任何 Host / Loader / Core 动作**之前**的第一个控制动作。"""
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        assert rt.timeline[0] == "gate.evaluate"
        for later in ("loader.load", "router_core", "host_direct", "ask_enable"):
            if later in rt.timeline:
                assert rt.index("gate.evaluate") < rt.index(later), later

    def test_no_downstream_action_precedes_the_gate(self) -> None:
        """UNASKED / DISABLED 下同理：Gate 之前不得有任何动作。"""
        for state in (SessionState.UNASKED, SessionState.DISABLED):
            rt = Runtime(SessionGate(state=state))
            rt.dispatch()
            assert rt.timeline[0] == "gate.evaluate", state

    @pytest.mark.parametrize(
        "state",
        [SessionState.UNASKED, SessionState.DISABLED, SessionState.ENABLED],
    )
    def test_only_enabled_reaches_router_core(self, state: SessionState) -> None:
        """**恰好一种** gate 状态到达 Router Core。"""
        rt = Runtime(SessionGate(state=state))
        try:
            rt.dispatch()
        except EntrypointError:
            pass
        expected = 1 if state is SessionState.ENABLED else 0
        assert rt.core.call_count == expected, state

    def test_router_core_is_reached_only_via_enabled(self) -> None:
        """把三态放在一起：到达 Core 的充要条件就是 ENABLED。"""
        reached: dict[SessionState, bool] = {}
        for state in SessionState:
            rt = Runtime(SessionGate(state=state))
            try:
                rt.dispatch()
            except EntrypointError:
                pass
            reached[state] = rt.core.call_count == 1
        assert reached == {
            SessionState.UNASKED: False,
            SessionState.DISABLED: False,
            SessionState.ENABLED: True,
        }

    def test_core_receives_the_task_and_the_loaded_context(self) -> None:
        """Core 收到的必须就是本次 task 与 loader 真实返回的对象。"""
        sentinel = object()
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch(sentinel)
        assert rt.core.tasks == [sentinel]
        assert rt.core.contexts == [rt.loader.last_returned]

    def test_gate_state_is_never_interpreted_by_the_entrypoint(self) -> None:
        """entrypoint 只消费 ``evaluate()`` 的结果，不自行解释状态。

        手法：用一个**鸭子类型** gate（只实现协议面）替代真实 ``SessionGate``；
        若 entrypoint 依赖了具体类或自行解释状态，这里就会失败。
        """

        class DuckGate:
            def __init__(self) -> None:
                self.evaluated = 0

            def evaluate(self) -> SessionGateResult:
                self.evaluated += 1
                return SessionGateResult.ENABLED

            # 以下三个在 ENABLED 路径上不会被调用；实现只为满足协议

            def may_ask_now(self) -> bool:  # pragma: no cover
                raise AssertionError("ENABLED 路径不应调用 may_ask_now")

            def compose_first_question(self):  # pragma: no cover
                raise AssertionError("ENABLED 路径不应 compose")

            def mark_asked(self) -> None:  # pragma: no cover
                raise AssertionError("ENABLED 路径不应 mark_asked")

        duck = DuckGate()
        timeline: list[str] = []
        loader = LoaderSpy(timeline)
        core = CoreSpy(timeline)
        handlers = EntrypointHandlers(
            session_gate=duck,  # type: ignore[arg-type]
            project_loader=loader,
            router_core=core,
        )
        assert dispatch("t", project_root="/r", handlers=handlers) == "routed"
        assert duck.evaluated == 1
        assert core.call_count == 1


# ==========================================================================
# INV-S1 —— "全 dispatch 路径恰好一个真实 SessionGate"
# ==========================================================================


class TestInvS1ExactlyOneRealGate:
    """补上前面的证明缺口：``GateSpy`` 只统计**注入的**那个 gate。

    如果 ``dispatch()`` 内部另建一个 ``SessionGate`` 并 evaluate，
    那个实例不会经过 GateSpy，``rt.gate.evaluate_calls`` 仍会是 1。

    ⚠️ 这里用**运行时 monkeypatch** 观测 ``SessionGate.evaluate`` 本身：
       任何真实 ``SessionGate`` 实例只要被 evaluate，都会被记进 ``seen``,
       不管它是不是 ``handlers.session_gate``。

       这**不是** grep、不是源码扫描、不是 package purity、
       不是仓库状态断言 —— 而是实际运行期间"到底有几个 SessionGate 被 evaluate"。
    """

    @staticmethod
    def _install(monkeypatch: pytest.MonkeyPatch) -> list[SessionGate]:
        seen: list[SessionGate] = []
        original = SessionGate.evaluate

        def observed(self: SessionGate) -> SessionGateResult:
            seen.append(self)
            return original(self)

        monkeypatch.setattr(SessionGate, "evaluate", observed)
        return seen

    @pytest.mark.parametrize(
        "state",
        [SessionState.UNASKED, SessionState.DISABLED, SessionState.ENABLED],
    )
    def test_exactly_one_real_session_gate_is_evaluated(
        self, monkeypatch: pytest.MonkeyPatch, state: SessionState
    ) -> None:
        """无论哪条分支，真实路径上**恰好一个** ``SessionGate`` 被 evaluate。"""
        seen = self._install(monkeypatch)

        gate = SessionGate(state=state)
        rt = Runtime(gate)
        try:
            rt.dispatch()
        except EntrypointError:
            pass

        assert len(seen) == 1, f"evaluated gates: {seen}"
        assert seen[0] is gate

    def test_exactly_one_real_gate_on_the_enabled_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._install(monkeypatch)
        gate = SessionGate(state=SessionState.ENABLED)
        rt = Runtime(gate)

        assert rt.dispatch() == "routed"

        assert seen == [gate]
        assert rt.core.call_count == 1
        assert rt.loader.call_count == 1

    def test_no_gate_is_evaluated_that_was_not_injected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``seen`` 里不得出现任何**非注入**的 gate 实例。"""
        seen = self._install(monkeypatch)
        gate = SessionGate(state=SessionState.ENABLED)
        rt = Runtime(gate)
        rt.dispatch()

        strays = [g for g in seen if g is not gate]
        assert strays == [], f"dispatch 内部自建了额外的 SessionGate: {strays}"

    def test_a_hidden_internal_gate_would_be_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**非平凡性**：这个观测器确实能发现内部自建 gate。

        模拟"错误实现"：一个 handler 内部另建 ``SessionGate`` 并 evaluate。
        观测器必须记到两个实例 —— 证明上面那条断言不是空地通过。
        """
        seen = self._install(monkeypatch)

        def sneaky_core(task: Any, context: ProjectContext) -> str:
            hidden = SessionGate(state=SessionState.ENABLED)
            hidden.evaluate()  # ← 违规的第二个真实 gate
            return "routed"

        injected = SessionGate(state=SessionState.ENABLED)
        rt = Runtime(injected)
        rt.handlers.router_core = sneaky_core
        rt.dispatch()

        assert len(seen) == 2
        assert seen[0] is injected
        assert seen[1] is not injected

    def test_observer_does_not_change_gate_behaviour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """观测器透传原实现，不改变判定结果。"""
        self._install(monkeypatch)
        for state, expected in (
            (SessionState.UNASKED, SessionGateResult.UNASKED),
            (SessionState.DISABLED, SessionGateResult.DISABLED),
            (SessionState.ENABLED, SessionGateResult.ENABLED),
        ):
            assert SessionGate(state=state).evaluate() is expected


# ==========================================================================
# INV-S2 —— 非 ENABLED 时 loader call_count == 0；ENABLED 时 == 1
# ==========================================================================


class TestInvS2NonEnabledNeverReadsProjectContext:
    def test_fresh_unasked_does_not_read_project_context(self) -> None:
        """fresh UNASKED：**只问启用问题**，不读项目。"""
        rt = Runtime(SessionGate())  # state=UNASKED, asked=False
        rt.dispatch()
        assert rt.loader.call_count == 0
        assert rt.core.call_count == 0
        assert rt.host.call_count == 0
        assert rt.ask.call_count == 1

    def test_asked_unanswered_unasked_does_not_read_project_context(self) -> None:
        """``asked=True`` 但尚无下一条 user message 的 UNASKED → hard-stop，不读项目。

        ⚠️ 这是关键一档：既不能读项目，也不能走 Host direct。
        """
        rt = Runtime(SessionGate())
        rt.dispatch()  # 第一次：提问成功，gate.asked 变 True
        first_loader_count = rt.loader.call_count

        with pytest.raises(EntrypointError):
            rt.dispatch()  # 第二次：仍无真实下一条用户消息

        assert first_loader_count == 0
        assert rt.loader.call_count == 0
        assert rt.core.call_count == 0
        assert rt.host.call_count == 0

    def test_disabled_does_not_read_project_context(self) -> None:
        """DISABLED：直接交还 Host，**不读项目**。"""
        rt = Runtime(SessionGate(state=SessionState.DISABLED))
        rt.dispatch()
        assert rt.loader.call_count == 0
        assert rt.core.call_count == 0
        assert rt.host.call_count == 1

    def test_disabled_after_a_real_user_message_still_does_not_read(self) -> None:
        """经 ``note_user_message()`` 进入 DISABLED 后同样不读项目。"""
        rt = Runtime(SessionGate())
        rt.dispatch()  # 提问
        rt.gate.note_user_message()  # 用户实际发来下一条消息且未回答
        rt.dispatch()
        assert rt.loader.call_count == 0
        assert rt.core.call_count == 0
        assert rt.host.call_count == 1

    @pytest.mark.parametrize(
        "state", [SessionState.UNASKED, SessionState.DISABLED]
    )
    def test_loader_call_count_is_zero_for_every_non_enabled_state(
        self, state: SessionState
    ) -> None:
        rt = Runtime(SessionGate(state=state))
        try:
            rt.dispatch()
        except EntrypointError:
            pass
        assert rt.loader.call_count == 0, state


class TestInvS2EnabledReadsExactlyOnce:
    def test_enabled_reads_project_context_exactly_once(self) -> None:
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        assert rt.loader.call_count == 1

    def test_enabled_does_not_refresh_implicitly(self) -> None:
        """entrypoint 不得自行判断 R5 —— ``refresh`` 必须保持默认。"""
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        assert rt.loader.refreshes == [False]

    def test_enabled_passes_the_requested_project_root(self) -> None:
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch(root="/some/project")
        assert rt.loader.roots == ["/some/project"]

    def test_loader_is_called_once_even_if_singletons_are_reused(self) -> None:
        """同一 Runtime 连续两次 ENABLED 分派 → 各一次（不是零次，也不是三次）。"""
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        rt.dispatch()
        assert rt.loader.call_count == 2
        assert rt.gate.evaluate_calls == 2


# ==========================================================================
# 调用顺序 —— gate < loader < router_core
# ==========================================================================


class TestInvS2CallOrdering:
    def test_gate_then_loader_then_router_core(self) -> None:
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        assert rt.timeline == ["gate.evaluate", "loader.load", "router_core"]

    def test_loader_precedes_router_core(self) -> None:
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        assert rt.index("loader.load") < rt.index("router_core")

    def test_gate_precedes_loader(self) -> None:
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        assert rt.index("gate.evaluate") < rt.index("loader.load")

    def test_disabled_ordering_is_gate_then_host_only(self) -> None:
        rt = Runtime(SessionGate(state=SessionState.DISABLED))
        rt.dispatch()
        assert rt.timeline == ["gate.evaluate", "host_direct"]

    def test_unasked_ordering_is_gate_then_ask_only(self) -> None:
        rt = Runtime(SessionGate())
        rt.dispatch()
        assert rt.timeline == [
            "gate.evaluate",
            "gate.may_ask_now",
            "gate.compose_first_question",
            "ask_enable",
            "gate.mark_asked",
        ]


# ==========================================================================
# 三态总表（S1 + S2 的合并矩阵）
# ==========================================================================


class TestCombinedMatrix:
    def test_the_three_paths_have_disjoint_call_signatures(self) -> None:
        """UNASKED / DISABLED / ENABLED 三路的调用计数矩阵。"""
        matrix: dict[str, dict[str, int]] = {}

        for label, gate in (
            ("UNASKED", SessionGate()),
            ("DISABLED", SessionGate(state=SessionState.DISABLED)),
            ("ENABLED", SessionGate(state=SessionState.ENABLED)),
        ):
            rt = Runtime(gate)
            try:
                rt.dispatch()
            except EntrypointError:
                pass
            matrix[label] = {
                "gate": rt.gate.evaluate_calls,
                "ask": rt.ask.call_count,
                "host": rt.host.call_count,
                "loader": rt.loader.call_count,
                "core": rt.core.call_count,
            }

        assert matrix == {
            "UNASKED": {"gate": 1, "ask": 1, "host": 0, "loader": 0, "core": 0},
            "DISABLED": {"gate": 1, "ask": 0, "host": 1, "loader": 0, "core": 0},
            "ENABLED": {"gate": 1, "ask": 0, "host": 0, "loader": 1, "core": 1},
        }

    def test_loader_and_core_are_never_both_zero_when_enabled(self) -> None:
        """ENABLED 下 loader 与 core 必须都发生 —— 不允许"静默吞掉"。"""
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        assert (rt.loader.call_count, rt.core.call_count) == (1, 1)

    def test_non_enabled_paths_never_touch_loader_or_core(self) -> None:
        """非 ENABLED 的**任何**组合都不得到达 loader / core。"""
        for gate in (
            SessionGate(),
            SessionGate(state=SessionState.DISABLED),
        ):
            rt = Runtime(gate)
            try:
                rt.dispatch()
            except EntrypointError:
                pass
            assert rt.loader.call_count == 0
            assert rt.core.call_count == 0


# ==========================================================================
# 非平凡性 —— 证明上面的 0 是有鉴别力的，不是空跑
# ==========================================================================


class TestTheZerosAreMeaningful:
    """⚠️ ``loader.call_count == 0`` 只有在**同一观测体系**能读到非零时才可信。

    因此这里用**同一个 LoaderSpy / CoreSpy**，先证明它能读到非零，
    再证明非 ENABLED 下读到的是零 —— 排除"spy 装错位置、什么都没观测到"
    这种假绿灯。

    不修改生产代码，不做源码字符串扫描。
    """

    def test_the_same_spy_observes_nonzero_on_the_enabled_path(self) -> None:
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        assert rt.loader.call_count == 1
        assert rt.core.call_count == 1
        assert rt.loader.inner.is_cached("/repo") is True, (
            "真实 loader 确实被调用过（内部缓存已被填充）"
        )

    def test_the_same_spy_observes_zero_on_non_enabled_paths(self) -> None:
        for gate in (
            SessionGate(),
            SessionGate(state=SessionState.DISABLED),
        ):
            rt = Runtime(gate)
            try:
                rt.dispatch()
            except EntrypointError:
                pass
            assert rt.loader.call_count == 0
            assert rt.loader.inner.is_cached("/repo") is False, (
                "真实 loader 的内部缓存必须仍为空 —— 说明确实没被调用过"
            )

    def test_spy_discriminates_between_two_different_call_sequences(self) -> None:
        """检测器有鉴别力：两种不同调用序列产生不同读数。"""
        enabled = Runtime(SessionGate(state=SessionState.ENABLED))
        enabled.dispatch()

        disabled = Runtime(SessionGate(state=SessionState.DISABLED))
        disabled.dispatch()

        assert enabled.timeline != disabled.timeline
        assert enabled.loader.call_count != disabled.loader.call_count
        assert enabled.core.call_count != disabled.core.call_count

    def test_timeline_assertion_would_fail_on_a_different_order(self) -> None:
        """顺序断言本身有鉴别力：把三元组换个顺序就不相等。"""
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        observed = list(rt.timeline)

        assert observed == ["gate.evaluate", "loader.load", "router_core"]
        for wrong in (
            ["loader.load", "gate.evaluate", "router_core"],
            ["gate.evaluate", "router_core", "loader.load"],
            ["router_core", "loader.load", "gate.evaluate"],
        ):
            assert observed != wrong

    def test_loader_events_equal_both_call_count_and_cache_state(self) -> None:
        """三种独立读数必须一致：时间线事件数、call_count、真实 loader 缓存。"""
        rt = Runtime(SessionGate(state=SessionState.ENABLED))
        rt.dispatch()
        events = sum(1 for e in rt.timeline if e == "loader.load")
        assert events == rt.loader.call_count == 1
        assert rt.loader.inner.is_cached("/repo") is True
