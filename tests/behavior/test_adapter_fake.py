"""``[11]`` 动作面（start / append / stop）的 in-memory 假适配器行为测试。

架构依据：`docs/architecture.md` 2 节 ``[11] Runtime Adapter``（**Amendment A14**，FROZEN）。
范围与调用链：`tests/behavior/README_step5b.md` §50.6 / §50.9。

**为什么假适配器在测试里而不是生产里**（裁定 ⑤）：真实 provider 需要 SDK / 凭证 /
生命周期语义 / 错误模型，**没有真实环境无法产生有效断言** ⇒ V1 **只交付 `adapters/contract.py`**，
三个动作面的**形状与语义**由一个 in-memory 假适配器承载并在此锁住。

假适配器同时是三条纪律的**可执行样本**：

1. **注入时钟**（L11-5）：时间戳只能来自注入的 ``clock``，模块内不许读时钟；
2. **显式穷尽分类，禁兜底**（裁定 ③）：无法归类的平台异常 ⇒ ``AdapterError``，**绝不**静默算成某个 kind；
3. **`handle` 只由适配器持有**（A14.2）：调用方只用 ``(branch_id, attempt)`` 定位，**不保存 handle**。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from adaptive_router.adapters import (
    AdapterError,
    AdapterFailure,
    AdapterFailureKind,
    AdapterStartReceipt,
)
from adaptive_router.agent_manager import AgentManager
from adaptive_router.contracts import (
    BranchState,
    CapabilityMismatch,
    ContextBundle,
    EffortLevel,
    ExecutionMode,
    ModelSelection,
    ModelTier,
    Plan,
    RoutingPlan,
    new_branch_id,
    new_run_id,
)
from adaptive_router.dispatch_decisions import DispatchAction, DispatchDecision

from _adapter_locks import signature_shape

FAKE_PATH = pathlib.Path(__file__).resolve()
FAKE_SOURCE = FAKE_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# 平台侧异常（仅用于驱动分类；真实 provider 有各自的错误模型）
# --------------------------------------------------------------------------


class PlatformRefused(Exception):
    """平台拒绝执行（策略 / 配额 / 权限）。"""


class PlatformNetworkError(Exception):
    """传输层失败，且没有任何平台侧执行开始的证据。"""


class PlatformTimeout(Exception):
    """等待预算内没有收到结果。"""


class PlatformCrash(Exception):
    """已派发后运行环境崩溃。"""


class PlatformWeirdError(Exception):
    """**无法归类**的异常 —— 必须显式失败，不得兜底成任何 kind。"""


def _classify(exc: BaseException, *, timestamp: str) -> AdapterFailure:
    """**显式穷尽**分类（裁定 ③）：四个分支 + 一条显式失败，**没有兜底 branch**。"""
    if isinstance(exc, PlatformRefused):
        kind = AdapterFailureKind.REFUSED
    elif isinstance(exc, PlatformNetworkError):
        kind = AdapterFailureKind.NETWORK
    elif isinstance(exc, PlatformTimeout):
        kind = AdapterFailureKind.TIMEOUT
    elif isinstance(exc, PlatformCrash):
        kind = AdapterFailureKind.CRASH
    else:
        # ⚠️ 兜底是**禁止**的：未知异常 → 任何 kind 都是发明事实
        raise AdapterError(f"无法归类的平台异常：{exc!r}") from exc
    return AdapterFailure(failure_kind=kind, message=str(exc), timestamp=timestamp)


# --------------------------------------------------------------------------
# in-memory 假适配器
# --------------------------------------------------------------------------


class FakeAdapter:
    """承载 ``[11]`` 三个动作面形状的假实现（duck typing，无 Protocol —— 裁定 ④）。"""

    def __init__(self, *, clock, start_exc=None, append_exc=None, stop_exc=None) -> None:
        #: 注入的时钟（L11-5）：适配层自己**不读**时钟
        self._clock = clock
        self._start_exc = start_exc
        self._append_exc = append_exc
        self._stop_exc = stop_exc
        #: ``handle`` 的生命周期**属于适配器**（A14.2）；调用方不持有它
        self._handles: dict[tuple[str, int], str] = {}
        self.started: list[tuple[str, int]] = []
        self.appended: list[tuple[str, int, str]] = []
        self.stopped: list[tuple[str, int]] = []

    @property
    def open_handle_count(self) -> int:
        """仍然开着的平台句柄数（供测试观察，不暴露 handle 本体）。"""
        return len(self._handles)

    def start(self, *, branch_id, attempt, plan, bundle):
        """A14.3：成功回 ``AdapterStartReceipt``；能力不足回 ``CapabilityMismatch``；否则 ``AdapterFailure``。"""
        if not isinstance(plan, RoutingPlan):
            raise AdapterError(f"plan 必须是 RoutingPlan，收到 {plan!r}")
        if not isinstance(bundle, ContextBundle):
            raise AdapterError(f"bundle 必须是 ContextBundle，收到 {bundle!r}")
        timestamp = self._clock()
        if self._start_exc is not None:
            # 分类器自己负责"不可归类 ⇒ AdapterError"（裁定 ③），本处**不**再包 try/except：
            # 早先的 `try: raise … except (四个具体类)` 写法会让**其它**异常漏成平台异常。
            return _classify(self._start_exc, timestamp=timestamp)
        handle = f"handle-{branch_id}-{attempt}"
        self._handles[(branch_id, attempt)] = handle
        self.started.append((branch_id, attempt))
        return AdapterStartReceipt(handle=handle, started_at=timestamp)

    def append(self, *, branch_id, attempt, instruction) -> None:
        """A13.6 的 ``WAITING_INPUT → RUNNING`` 执行侧；**成功 = 无异常**（裁定 ②）。"""
        self._append_exc_or_none(branch_id, attempt)
        if type(instruction) is not str or not instruction.strip():
            raise AdapterError(f"instruction 必须是非空 str，收到 {instruction!r}")
        self.appended.append((branch_id, attempt, instruction))

    def stop(self, *, branch_id, attempt) -> None:
        """尽力而为的平台侧终止；**成功 = 无异常**（A14.5 / N1）。"""
        self._stop_exc_or_none(branch_id, attempt)
        self.stopped.append((branch_id, attempt))
        self._handles.pop((branch_id, attempt), None)

    # -- 内部：把"平台异常 → 异常通道"的规则写在一处 ------------------------

    def _append_exc_or_none(self, branch_id, attempt) -> None:
        if self._append_exc is None:
            return
        self._raise_classified(self._append_exc, action="append")

    def _stop_exc_or_none(self, branch_id, attempt) -> None:
        if self._stop_exc is None:
            return
        self._raise_classified(self._stop_exc, action="stop")

    def _raise_classified(self, exc: BaseException, *, action: str) -> None:
        """``append`` / ``stop`` 的签名是 ``-> None`` ⇒ 失败只能走**异常通道**。"""
        failure = _classify(exc, timestamp=self._clock())
        raise AdapterError(f"{action} 失败：{failure.failure_kind.value}", failure=failure)


# --------------------------------------------------------------------------
# 替身工厂
# --------------------------------------------------------------------------


def clock() -> str:
    return "2026-09-18T00:00:00Z"


def bundle(goal: str = "g") -> ContextBundle:
    return ContextBundle(
        goal=goal,
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


def routing_plan() -> RoutingPlan:
    """构造一个**合法**的 ``RoutingPlan``（适配器只读它，不改它 —— INV-03）。

    ⚠️ 这里刻意用 **0 child 的 ``DIRECT``**：它正是 H1 的活证据 ——
    ``RoutingPlan`` 里**没有任何字段**说明"本次派发属于哪个 branch"
    （``branch_models`` 只覆盖 ``Plan.branches``，且必须引用真实存在的 child），
    因此身份**只能**由派发调用携带 ``(branch_id, attempt)``（A14.2）。
    """
    return RoutingPlan(
        run_id=new_run_id(),
        plan=Plan(
            mode=ExecutionMode.DIRECT,
            main_thread_duty="duty",
            reason="test",
            branches=(),
        ),
        main_thread_branch_id=new_branch_id(),
        main_thread_model=ModelSelection(
            tier=ModelTier.BALANCED, effort=EffortLevel.MEDIUM, reason="test"
        ),
        branch_models=(),
    )


# ==========================================================================
# 形状锁（裁定 ② / §50.6 —— 三个动作面的精确签名）
# ==========================================================================


class TestActionSurfaceShape:
    def test_start_shape(self) -> None:
        assert signature_shape(FAKE_SOURCE, "FakeAdapter", "start") == {
            "positional": ["self"],
            "kwonly": ["branch_id", "attempt", "plan", "bundle"],
            "defaults": [False, False, False, False],
            "vararg": None,
            "kwarg": None,
        }

    def test_append_shape(self) -> None:
        assert signature_shape(FAKE_SOURCE, "FakeAdapter", "append") == {
            "positional": ["self"],
            "kwonly": ["branch_id", "attempt", "instruction"],
            "defaults": [False, False, False],
            "vararg": None,
            "kwarg": None,
        }

    def test_stop_shape(self) -> None:
        assert signature_shape(FAKE_SOURCE, "FakeAdapter", "stop") == {
            "positional": ["self"],
            "kwonly": ["branch_id", "attempt"],
            "defaults": [False, False],
            "vararg": None,
            "kwarg": None,
        }

    def test_no_protocol_layer(self) -> None:
        """裁定 ④：不引入 ``typing.Protocol``（也不引入继承层）。

        ⚠️ **AST 级判定**：本文件自己的 docstring 里就写着 ``Protocol`` 这个词，
        子串扫描会**假阳性**（与 `[10]` 的 L5 同类陷阱）。
        """
        bases = {
            ast.unparse(base)
            for node in ast.walk(ast.parse(FAKE_SOURCE))
            if isinstance(node, ast.ClassDef)
            for base in node.bases
        }
        assert not any("Protocol" in base or "ABC" in base for base in bases), bases

    def test_fake_is_duck_typed(self) -> None:
        """三个动作面存在即可被替换 —— 不需要共同基类。"""
        adapter = FakeAdapter(clock=clock)
        for name in ("start", "append", "stop"):
            assert callable(getattr(adapter, name))


# ==========================================================================
# start —— 三种结局
# ==========================================================================


class TestStartOutcomes:
    def test_success_returns_receipt_with_platform_handle(self) -> None:
        adapter = FakeAdapter(clock=clock)
        branch_id = new_branch_id()
        outcome = adapter.start(
            branch_id=branch_id, attempt=1, plan=routing_plan(), bundle=bundle()
        )
        assert isinstance(outcome, AdapterStartReceipt)
        assert outcome.handle == f"handle-{branch_id}-1"
        assert outcome.started_at == clock()
        assert adapter.started == [(branch_id, 1)]

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (PlatformRefused("policy"), AdapterFailureKind.REFUSED),
            (PlatformNetworkError("reset"), AdapterFailureKind.NETWORK),
            (PlatformTimeout("no reply"), AdapterFailureKind.TIMEOUT),
            (PlatformCrash("oom"), AdapterFailureKind.CRASH),
        ],
    )
    def test_each_failure_kind_is_returned_not_raised(
        self, exc: BaseException, expected: AdapterFailureKind
    ) -> None:
        """`start` 用**返回值**表达非能力失败（A14.4）。"""
        adapter = FakeAdapter(clock=clock, start_exc=exc)
        branch_id = new_branch_id()
        outcome = adapter.start(
            branch_id=branch_id, attempt=1, plan=routing_plan(), bundle=bundle()
        )
        assert isinstance(outcome, AdapterFailure)
        assert outcome.failure_kind is expected
        assert outcome.timestamp == clock()
        assert adapter.started == []  # 没有启动，就没有 handle

    def test_unclassifiable_platform_error_is_explicit(self) -> None:
        """裁定 ③：无法归类 ⇒ **显式失败**，绝不兜底成某个 kind。"""
        adapter = FakeAdapter(clock=clock, start_exc=PlatformWeirdError("???"))
        branch_id = new_branch_id()
        with pytest.raises(AdapterError, match="无法归类"):
            adapter.start(
                branch_id=branch_id, attempt=1, plan=routing_plan(), bundle=bundle()
            )

    def test_capability_mismatch_is_not_an_adapter_failure(self) -> None:
        """A14.4：能力问题走**已冻结**的 ``CapabilityMismatch``，**不得**塞进 ``AdapterFailure``。"""
        mismatch = CapabilityMismatch(
            requested="strong/high",
            supported="balanced/medium",
            alternatives=("balanced/medium",),
            detail="platform does not expose high effort",
        )
        assert not isinstance(mismatch, AdapterFailure)
        assert mismatch.has_alternatives is True

    def test_adapter_refuses_wrong_input_types(self) -> None:
        adapter = FakeAdapter(clock=clock)
        branch_id = new_branch_id()
        with pytest.raises(AdapterError, match="plan 必须是 RoutingPlan"):
            adapter.start(branch_id=branch_id, attempt=1, plan="nope", bundle=bundle())
        with pytest.raises(AdapterError, match="bundle 必须是 ContextBundle"):
            adapter.start(
                branch_id=branch_id, attempt=1, plan=routing_plan(), bundle="nope"
            )


# ==========================================================================
# append / stop —— 成功 = 无异常；失败 = 异常通道（裁定 ②）
# ==========================================================================


class TestAppendAndStop:
    def test_append_success_returns_none(self) -> None:
        adapter = FakeAdapter(clock=clock)
        branch_id = new_branch_id()
        assert adapter.append(branch_id=branch_id, attempt=1, instruction="do more") is None
        assert adapter.appended == [(branch_id, 1, "do more")]

    def test_stop_success_returns_none(self) -> None:
        adapter = FakeAdapter(clock=clock)
        branch_id = new_branch_id()
        adapter.start(branch_id=branch_id, attempt=1, plan=routing_plan(), bundle=bundle())
        assert adapter.stop(branch_id=branch_id, attempt=1) is None
        assert adapter.stopped == [(branch_id, 1)]

    def test_append_rejects_blank_instruction(self) -> None:
        adapter = FakeAdapter(clock=clock)
        with pytest.raises(AdapterError, match="instruction"):
            adapter.append(branch_id=new_branch_id(), attempt=1, instruction="   ")

    @pytest.mark.parametrize("action", ["append", "stop"])
    def test_failure_uses_the_exception_channel_with_kind(self, action: str) -> None:
        """``-> None`` 的签名没有返回通道 ⇒ 失败必须携带**已分类**的 ``AdapterFailure``。"""
        exc = PlatformNetworkError("reset")
        adapter = FakeAdapter(
            clock=clock,
            append_exc=exc if action == "append" else None,
            stop_exc=exc if action == "stop" else None,
        )
        branch_id = new_branch_id()
        with pytest.raises(AdapterError) as caught:
            if action == "append":
                adapter.append(branch_id=branch_id, attempt=1, instruction="x")
            else:
                adapter.stop(branch_id=branch_id, attempt=1)
        assert caught.value.failure is not None
        assert caught.value.failure.failure_kind is AdapterFailureKind.NETWORK
        assert caught.value.failure.timestamp == clock()

    @pytest.mark.parametrize("action", ["append", "stop"])
    def test_unclassifiable_failure_has_no_kind(self, action: str) -> None:
        exc = PlatformWeirdError("???")
        adapter = FakeAdapter(
            clock=clock,
            append_exc=exc if action == "append" else None,
            stop_exc=exc if action == "stop" else None,
        )
        branch_id = new_branch_id()
        with pytest.raises(AdapterError, match="无法归类") as caught:
            if action == "append":
                adapter.append(branch_id=branch_id, attempt=1, instruction="x")
            else:
                adapter.stop(branch_id=branch_id, attempt=1)
        assert caught.value.failure is None

    def test_handles_live_in_the_adapter_not_the_caller(self) -> None:
        """A14.2：调用方只用 ``(branch_id, attempt)`` 定位 —— handle 由适配器自己保管。"""
        adapter = FakeAdapter(clock=clock)
        branch_id = new_branch_id()
        adapter.start(branch_id=branch_id, attempt=1, plan=routing_plan(), bundle=bundle())
        # 调用方**没有**拿到过 handle 之外的任何状态；stop 只按身份定位
        adapter.stop(branch_id=branch_id, attempt=1)
        assert adapter.open_handle_count == 0  # 适配器内部已释放句柄


# ==========================================================================
# 端到端：receipt → 显式标量 → [10] 提交 RUNNING（A13.2 + A14.3 + N2）
# ==========================================================================


class TestReceiptClosesTheTwoPhaseCommit:
    def _manager_and_request(self) -> tuple[AgentManager, str, str]:
        branch_id = new_branch_id()
        manager = AgentManager(
            run_id=new_run_id(), branch_order=(branch_id,), total_attempt_limit=2
        )
        starts = manager.materialize(
            decisions=(
                DispatchDecision(
                    branch_id=branch_id, action=DispatchAction.START, reason="licence"
                ),
            ),
            bundles={branch_id: bundle()},
        )
        assert len(starts) == 1
        return manager, starts[0], branch_id

    def test_start_writes_no_state_until_the_receipt_arrives(self) -> None:
        manager, request, _ = self._manager_and_request()
        assert manager.routing_run.branches == ()  # START 只是 licence
        assert manager.active_subagent_count == 0

    def test_receipt_closes_the_commit_with_explicit_scalars(self) -> None:
        manager, request, branch_id = self._manager_and_request()
        adapter = FakeAdapter(clock=clock)
        outcome = adapter.start(
            branch_id=request.branch_id,
            attempt=request.attempt,
            plan=routing_plan(),
            bundle=request.bundle,
        )
        assert isinstance(outcome, AdapterStartReceipt)
        # ⚠️ N2：P1 **拆包成显式标量**，[10] 不知道适配层类型的存在
        manager.commit_started(
            branch_id=request.branch_id,
            attempt=request.attempt,
            handle=outcome.handle,
            started_at=outcome.started_at,
        )
        record = manager.routing_run.branches[0]
        assert record.state is BranchState.RUNNING
        assert record.handle == outcome.handle
        assert record.started_at == outcome.started_at
        assert manager.active_subagent_count == 1

    def test_adapter_failure_leaves_the_branch_uncommitted(self) -> None:
        """A13.2 + A14.4：非能力失败 ⇒ **不得**提交 ``RUNNING``（根本没有 agent 被启动）。"""
        manager, request, branch_id = self._manager_and_request()
        adapter = FakeAdapter(clock=clock, start_exc=PlatformTimeout("no reply"))
        outcome = adapter.start(
            branch_id=request.branch_id,
            attempt=request.attempt,
            plan=routing_plan(),
            bundle=request.bundle,
        )
        assert isinstance(outcome, AdapterFailure)
        # 没有成功事实 ⇒ 不调用 commit
        assert manager.routing_run.branches == ()
        assert manager.active_subagent_count == 0

    def test_capability_mismatch_leaves_the_branch_uncommitted(self) -> None:
        manager, request, branch_id = self._manager_and_request()
        mismatch = CapabilityMismatch(
            requested="strong/high",
            supported="none",
            alternatives=(),
            detail="platform cannot run agents",
        )
        assert mismatch.has_alternatives is False
        assert manager.routing_run.branches == ()

    def test_queued_branch_stays_waiting_when_the_adapter_fails(self) -> None:
        """§50.9 ②：原为 ``WAITING`` 者，启动失败后**仍是** ``WAITING``（可再次 admission）。"""
        branch_id = new_branch_id()
        manager = AgentManager(
            run_id=new_run_id(), branch_order=(branch_id,), total_attempt_limit=2
        )
        manager.materialize(
            decisions=(
                DispatchDecision(
                    branch_id=branch_id, action=DispatchAction.QUEUE, reason="no slot"
                ),
            ),
            bundles={branch_id: bundle()},
        )
        assert manager.pending_admission_branch_ids == (branch_id,)
        adapter = FakeAdapter(clock=clock, start_exc=PlatformCrash("boom"))
        plan = routing_plan()
        # 后续 pass：拿 START 许可（提升，仍是 attempt 1）
        starts = manager.materialize(
            decisions=(
                DispatchDecision(
                    branch_id=branch_id, action=DispatchAction.START, reason="slot freed"
                ),
            ),
            bundles={branch_id: bundle()},
        )
        outcome = adapter.start(
            branch_id=branch_id, attempt=starts[0].attempt, plan=plan, bundle=bundle()
        )
        assert isinstance(outcome, AdapterFailure)
        assert manager.routing_run.branch_by_id(branch_id).state is BranchState.WAITING
        assert manager.pending_admission_branch_ids == (branch_id,)
