"""P1 —— **执行半边 compositor**（`[10] ↔ [11]` 的唯一接线层）。

架构依据：`docs/architecture.md` 的 **A11.10 / A13.5 / A13.6 / A14.6**；
范围、锁清单（L12-1 – L12-16）与 probe 计划见 `tests/behavior/README_step5b.md` §53–§56。

**归属**：**Core 包内的执行半边**（A14.6）—— **不是** ``adapters/`` 的一部分。
它是 **Core 内唯一允许 import adapters 的模块**（L11-2 / L12-2），
因为它是"把 Core 的决定交给平台、再把平台的显式结果交回 Core"的那条缝。

**五项职责（A14.6，逐字）：**

    ① 装配 [09] 的输入（Plan / branches_under_admission / bundles / CapabilitySnapshot /
       显式 AdmissionFacts / reusable_results_by_branch / 只读运行态）并调用 [09]
    ② 把 DispatchDecision 交给 [10]
    ③ 把 [02] 的运行期控制事实经 RuntimeControlFacts 交给 [10]
    ④ 把 [10] 的 AgentStartRequest 搬到 [11]，**并调用 [11]**
    ⑤ 把 [11] 的**显式结果**交回 [10] 或 Core

**明确不做（越界即缺陷）：**

- ❌ 不重新规划（[05]）· 不重新审批（Approval Judgment / Gate）· 不重新调度（[09]）；
- ❌ 不重算成本（[15a] 在 4.2 的 Core 流程里、**严格早于**执行半边）；
- ❌ 不做 recovery 分类 / 不决定重试 / 不决定降级（[14] / [06]）；
- ❌ **不写任何 Core 运行态**（单写者是 ``[10]``，A11.10）；
- ❌ **不是任何事实的 owner**：不比较 ``goal``、不计算"独立价值"、不解释成本收益、
  **不自行寻找 / 配对 reusable result**（A11.10 逐字）；
- ❌ **不读时钟**（L12-8）：所有时间戳都由上游显式给出；
- ❌ 不创建真实 provider 适配器；**不建任何接口抽象层**（无 ``Protocol`` / ``ABC`` / ``*Source`` 类）。

**两处形状要点：**

1. ``run_pass`` 收 **``routing: RoutingPlan``**（不是 ``Plan``）：``[11]`` 的冻结输入是
   ``RoutingPlan``，而 ``[09]`` 与分支顺序需要 ``Plan`` —— 二者是**同一个对象的两层**
   （``RoutingPlan.plan`` 就是那个 ``Plan``，契约已校验 ``branch_models`` 精确覆盖 ``plan.branches``）
   ⇒ 收一个参数即可同时满足两侧，**不引入第二个计划参数**；
2. ``control_facts_source()`` 返回 **``(RuntimeControlFacts, str)``** 二元组 ——
   第二个元素是**上游命令的时间戳**（A13.5 / §56.2）。P1 **只解包并转交**，
   **不生成时间、不读时钟、不推断结束时间**。

**⏸ 三项 deferred（不在本轮）：** 真实 provider 适配器 · ``[12]`` 回传接线 ·
``[10]`` 的 ``WAITING_INPUT`` 入口（``append`` 的消费端）。

**回程组装（A15.3，本轮补上）：** ``assemble_result_handoff(...)`` 把 ``[11]`` 的
``RuntimeResultPayload``（**已规范化事实**，不是 raw provider response）组装成
``ResultHandoff``，交给调用方 / Core。它**只装配、不判断**：

    branch_id / attempt   ← 调用方给出（**不推断**）
    agent_id              ← **[10] 的既有记录**（A14.2：绝不由 Adapter 提供；必须与本 attempt 一致）
    planned_fact          ← `RoutingPlan.model_for_branch(branch_id)` 的**档位投影**
    requested_fact / actual_fact / provider_payload
                          ← 逐项来自 ``[11]`` 的 payload（**原样搬运**）

⚠️ 本方法**不调用** ``[12]``（``build_branch_return``）：本模块**不 import** ``execution_results``
—— handoff 的消费属调用方 / 后续轮，``[12]`` 与 P1 因此保持**单向**依赖。
⚠️ 它**天然无状态**：四个入参都在调用时给出，不缓存任何跨调用信息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from .adapters import AdapterFailure, AdapterStartReceipt
from .admission_facts import AdmissionFacts
from .agent_manager import AgentManager
from .agent_start_requests import AgentStartRequest
from .contracts import (
    BranchId,
    CapabilityMismatch,
    CapabilitySnapshot,
    ConfirmationSource,
    ContextBundle,
    RoutingPlan,
)
from .contracts.common import ContractError
from .dispatch_decisions import DispatchDecision
from .result_handoff import PlannedFacts, ResultHandoff, RuntimeResultPayload
from .reusable_results import ReusableResultRef
from .runtime_control_facts import RuntimeControlFacts
from .scheduler import Scheduler
from .scheduling_runtime_facts import SchedulingRuntimeFacts

__all__ = ["ExecutionCompositor", "ExecutionPassReport"]


@dataclass(frozen=True)
class ExecutionPassReport:
    """一次 pass 的**结果**（L12-10：恰好五个字段；L12-11：元素形状固定）。

    ⚠️ 本类型**只承载结果**：不得再计算 policy、再决定 routing、再生成 cost。
    ⚠️ ``mismatches`` 的元素仍是 ``(branch_id, CapabilityMismatch)`` **二元组**；
    ``failures`` 的元素是 ``(branch_id, attempt, AdapterFailure)`` **三元组**
    （`P6 / D-P6C″`：`attempt` 必须**随 failure witness 一起**传播 —— ⛔ 不得再经
    ``started`` 反查、⛔ 不得猜 ``attempt=1``）。``CapabilityMismatch`` /
    ``AdapterFailure`` 自身**没有**任何身份字段 ⇒ 归属**不得**靠顺序、下标或调用次数推断。
    """

    decisions: tuple[DispatchDecision, ...]
    started: tuple[tuple[BranchId, int, str], ...]
    mismatches: tuple[tuple[BranchId, CapabilityMismatch], ...]
    failures: tuple[tuple[BranchId, int, AdapterFailure], ...]
    stopped: tuple[BranchId, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.decisions, tuple):
            raise TypeError(f"decisions 必须是 tuple，收到 {self.decisions!r}")
        if not isinstance(self.started, tuple):
            raise TypeError(f"started 必须是 tuple，收到 {self.started!r}")
        if not isinstance(self.mismatches, tuple):
            raise TypeError(f"mismatches 必须是 tuple，收到 {self.mismatches!r}")
        if not isinstance(self.failures, tuple):
            raise TypeError(f"failures 必须是 tuple，收到 {self.failures!r}")
        if not isinstance(self.stopped, tuple):
            raise TypeError(f"stopped 必须是 tuple，收到 {self.stopped!r}")


class ExecutionCompositor:
    """把 ``[09]`` → ``[10]`` → ``[11]`` 串起来，并把结果如实交回 Core。

    ``adapter`` 是 **duck-typed** 的（裁定 ④：不建 ``Protocol``）：
    必须有 ``start`` / ``append`` / ``stop`` 三个 keyword-only 动作面
    （形状见 `tests/behavior/test_adapter_fake.py` 的形状锁）。
    """

    def __init__(
        self,
        *,
        scheduler: Scheduler,
        agent_manager: AgentManager,
        adapter: object,
        admission_facts_source: Callable[[BranchId], AdmissionFacts],
        reusable_result_source: Callable[[BranchId], ReusableResultRef | None],
        continuation_source: Callable[[], int | None],
    ) -> None:
        self._scheduler = scheduler
        self._agent_manager = agent_manager
        self._adapter = adapter
        self._admission_facts_source = admission_facts_source
        self._reusable_result_source = reusable_result_source
        self._continuation_source = continuation_source

    def run_pass(
        self,
        *,
        routing: RoutingPlan,
        bundles: Mapping[BranchId, ContextBundle],
        capability: CapabilitySnapshot,
        runtime_facts: SchedulingRuntimeFacts,
        control_facts_source: Callable[[], tuple[RuntimeControlFacts, str]],
        first_pass: bool,
    ) -> ExecutionPassReport:
        """跑一次执行半边 pass。

        ⚠️ ``first_pass`` 由**调用方显式**给出（裁定 ⑥）：A11.3c 的两种来源是**两种不同输入**，
        **不得**用"``pending`` 为空"之类的状态集合反推业务事实。
        """
        # ① 上游运行期控制事实（含**命令时间戳**）—— 原样解包、原样转交（L12-16）
        facts, ended_at = control_facts_source()
        stopped = self._agent_manager.apply_runtime_control(
            facts=facts, ended_at=ended_at
        )
        if facts.stop_branches_requested:
            # ② 显式用户控制命令：**本 pass 不再派发**
            #    （L45「立即应用于未开始工作」；"先启动再停止"会制造无意义的 attempt）
            return ExecutionPassReport(
                decisions=(), started=(), mismatches=(), failures=(), stopped=stopped
            )

        # `P2` 冻结时序：**stop 短路之后**、**selection / scheduling / dispatch 之前**；
        # ⛔ `first_pass=True` **不得消费** continuation fact。
        if not first_pass:
            continuation = self._continuation_source()
            if continuation is not None:
                self._agent_manager.commit_run_attempt(attempt=continuation)

        branches = self._branches_under_admission(routing, first_pass)
        admission_facts_by_branch, reusable_results_by_branch = self._collect_inputs(branches)

        decisions = self._scheduler.schedule(
            plan=routing.plan,
            branches_under_admission=branches,
            bundles=bundles,
            admission_facts_by_branch=admission_facts_by_branch,
            reusable_results_by_branch=reusable_results_by_branch,
            capability=capability,
            runtime=runtime_facts,
        )
        starts = self._agent_manager.materialize(decisions=decisions, bundles=bundles)
        started, mismatches, failures = self._dispatch(starts, routing)
        return ExecutionPassReport(
            decisions=decisions,
            started=started,
            mismatches=mismatches,
            failures=failures,
            stopped=stopped,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _branches_under_admission(
        self, routing: RoutingPlan, first_pass: bool
    ) -> tuple[BranchId, ...]:
        """A11.3c 的**两个来源**：首次 = ``Plan.branches`` 全部；后续 = ``[10]`` 的 pending。"""
        if first_pass:
            return tuple(spec.branch_id for spec in routing.plan.branches)
        return self._agent_manager.pending_admission_branch_ids

    def _collect_inputs(
        self, branches: tuple[BranchId, ...]
    ) -> tuple[dict[BranchId, AdmissionFacts], dict[BranchId, ReusableResultRef]]:
        """逐 child 取两份上游事实 —— **只搬运**，不判断"有没有可复用的"（A11.10）。"""
        admission: dict[BranchId, AdmissionFacts] = {}
        reusable: dict[BranchId, ReusableResultRef] = {}
        for branch_id in branches:
            admission[branch_id] = self._admission_facts_source(branch_id)
            reference = self._reusable_result_source(branch_id)
            if reference is not None:
                reusable[branch_id] = reference
        return admission, reusable

    def _dispatch(
        self, starts: tuple[AgentStartRequest, ...], routing: RoutingPlan
    ) -> tuple[
        tuple[tuple[BranchId, int, str], ...],
        tuple[tuple[BranchId, CapabilityMismatch], ...],
        tuple[tuple[BranchId, int, AdapterFailure], ...],
    ]:
        """把 ``[10]`` 的派发许可搬到 ``[11]``，并处理三种**显式**结局。

        ⚠️ ``AdapterError``（不可归类）**不在这里被捕获**（L12-14）—— 它向上抛出并中断本 pass；
        **已经 ``commit_started`` 的分支保持已写入状态**：P1 **不回滚、不补偿**（那是 ``[14]`` 的活）。
        """
        started: list[tuple[BranchId, int, str]] = []
        mismatches: list[tuple[BranchId, CapabilityMismatch]] = []
        failures: list[tuple[BranchId, AdapterFailure]] = []

        for request in starts:
            outcome = self._adapter.start(
                branch_id=request.branch_id,
                attempt=request.attempt,
                plan=routing,
                bundle=request.bundle,
            )
            if isinstance(outcome, AdapterStartReceipt):
                # **唯一拆包点**（A14.6 / L12-3）：只交出两个显式标量，
                # `[10]` 因此永远不需要认识适配层的类型。
                self._agent_manager.commit_started(
                    branch_id=request.branch_id,
                    attempt=request.attempt,
                    handle=outcome.handle,
                    started_at=outcome.started_at,
                )
                started.append((request.branch_id, request.attempt, outcome.handle))
            elif isinstance(outcome, CapabilityMismatch):
                mismatches.append((request.branch_id, outcome))
            elif isinstance(outcome, AdapterFailure):
                # `§255.1` 冻结形状：**先** settlement（`[10]` 的**公开状态命令**），
                # **再**登记三元组 witness（`ended_at` = `AdapterFailure.timestamp`）。
                # ⛔ 本模块仍不写 `AgentRun`、⛔ 不做 recovery 判断。
                self._agent_manager.commit_start_failure(
                    branch_id=request.branch_id,
                    attempt=request.attempt,
                    ended_at=outcome.timestamp,
                )
                failures.append((request.branch_id, request.attempt, outcome))
            else:
                # 禁静默默认（L12-15）：**内建**异常，不新增异常类型
                raise TypeError(f"未覆盖的 adapter 结局：{outcome!r}")

        return tuple(started), tuple(mismatches), tuple(failures)

    # ------------------------------------------------------------------
    # 回程组装（A15.3）—— **单一装配入口**
    # ------------------------------------------------------------------

    def assemble_result_handoff(
        self,
        *,
        routing: RoutingPlan,
        branch_id: BranchId,
        attempt: int,
        payload: RuntimeResultPayload,
    ) -> ResultHandoff:
        """把 ``[11]`` 的**已规范化结果事实**组装成 ``ResultHandoff``（A15.3）。

        **只装配，不判断**：不解释 provider 格式、不评估结果、不触发任何 downstream
        （`[13]` Validation / `[14]` Recovery / `[15b]` / `[15c]`）。

        ⚠️ **身份只能来自既有事实**：
        ``agent_id`` 取 **[10] 的记录**（A14.2：绝不由 Adapter 提供），
        且该记录的 ``attempt`` **必须**与本次结果一致 —— 否则**显式失败**（不得静默绑到别的 agent）。
        """
        if not isinstance(payload, RuntimeResultPayload):
            raise ContractError(f"payload 必须是 RuntimeResultPayload，收到 {payload!r}")

        record = self._agent_manager.routing_run.branch_by_id(branch_id)
        if record.attempt != attempt:
            raise ContractError(
                f"{branch_id} 的记录是 attempt {record.attempt}，结果却报 attempt {attempt}"
                "（身份必须一致，不得静默绑定到另一个 agent）"
            )

        selection = routing.model_for_branch(branch_id)
        return ResultHandoff(
            branch_id=branch_id,
            agent_id=record.agent_id,
            attempt=attempt,
            planned_fact=PlannedFacts(
                tier=selection.tier,
                effort=selection.effort,
                source=ConfirmationSource.PLANNER,
            ),
            requested_fact=payload.requested,
            actual_fact=payload.actual,
            provider_payload=payload.payload,
        )
