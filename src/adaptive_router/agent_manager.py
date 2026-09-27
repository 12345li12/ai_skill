"""``[10] Agent Manager`` —— Amendment A13 的实现。

架构依据：`docs/architecture.md` 2 节 ``[10] Agent Manager``（**Amendment A13**，FROZEN）；
范围、允许修改文件、锁清单（L1–L18）与 probe 计划见
`tests/behavior/README_step5b.md` §44。

**运行态单一写者（架构 6.4 + L47）。**
``BranchState`` / ``AgentRun`` / ``RoutingRun`` **只有本模块写**；
``[08]``/``[09]``/``[11]``/``[12]``/``[13]``/``[14]``/``[15*]`` **只提供事实与决定**。

**两阶段物化（A13.2）。**

    DispatchDecision.START
        ⇒ **admission licence**：产出 AgentStartRequest，**不写任何记录**
        ⇒ P1 把它搬到 [11]（本模块**不 import、不调用** Adapter）
        ⇒ **只有**显式的"成功启动"事实到达时，才由 commit_started(...) 提交 RUNNING + handle

    DispatchDecision.QUEUE     ⇒ AgentRun(state=WAITING)（不依赖平台，可立即物化）
    DispatchDecision.REJECT    ⇒ **不创建 AgentRun**（准入拒绝 ≠ 分支失败，A13.1）
    DispatchDecision.REUSE     ⇒ **不创建 AgentRun**（根本没有 agent，不能伪造 agent_id）

**四值读数（A13.4），也是 ``[09]`` 后续 pass 的事实来源：**

    active_subagent_count        = 各 branch 最新 attempt ∈ {RUNNING, WAITING_INPUT} 的 branch 数
    pending_admission_branch_ids = 按注入的 Plan.branches 顺序、最新 attempt == WAITING 的 branch

⚠️ ``WAITING_INPUT`` **必须计入** ``active_subagent_count``：它**已经是一个存活 agent**，
只是等待输入；不计会**突破保守边界**（少算实际占用的槽位）。

**明确不做（越界即为缺陷）：**

- **不 import / 不调用 Adapter**（A13.6）：只产出 ``AgentStartRequest``，由 P1 搬运；
- **不读时钟**（A13.2）：``started_at`` / ``ended_at`` **全部由调用方显式传入**；
- **不读 ``cost.*``**（A13.2）：``[15b]`` Ledger / telemetry 只是**观察者**，
  **不得成为本模块的 authoritative state source**；
- **不 import ``policy``**：``[02]`` 的控制事实只经 ``RuntimeControlFacts`` 到达（A13.5）；
- **不发明 ``handle``**（A13.6）：它**只能**来自显式 runtime / adapter 事实；
- **不写 ``BranchState.REJECTED``**（A13.1）：该状态经冻结映射 ``_BRANCH_TERMINAL_MAP``
  落 ``TERMINAL_FAILURE``，拿它表达"准入拒绝"会**污染失败统计**。

**⏸ 两项 deferred（不在本轮，见 README §45）：**

1. ``RoutingRun.attempt`` / ``RoutingRun.terminal`` 的推进 —— Run 级 attempt 由 ``[14]`` Recovery
   推进、Run 终态由收敛条件决定；本轮 ``routing_run`` 投影固定 ``attempt=1`` / ``terminal=None``；
2. ``[11]`` 的 exact receipt DTO（A13.6 把它留给 ``[11]`` preflight）—— 本轮 ``commit_started``
   只收**显式**的 ``handle`` / ``started_at``，**不定义**任何 receipt 类型。
"""

from __future__ import annotations

from typing import Mapping

from .agent_start_requests import AgentStartRequest
from .contracts import (
    AgentRun,
    BranchId,
    BranchState,
    ContextBundle,
    RunId,
    RoutingRun,
    RunTerminal,
    TerminalState,
    new_agent_id,
)
from .contracts.common import validate_branch_id, validate_run_id
from .dispatch_decisions import DispatchAction, DispatchDecision
from .runtime_control_facts import RuntimeControlFacts

__all__ = ["AgentManager", "AgentManagerError"]

#: A13.4：占用子代理槽位的状态。``WAITING_INPUT`` **必须计入**（存活 agent，只是等待输入）。
_LIVE_STATES = (BranchState.RUNNING, BranchState.WAITING_INPUT)

#: A13.5：可以被上游 stop-all 命令停下的状态（**只有**已进入运行生命周期的记录）。
_STOPPABLE_STATES = (
    BranchState.WAITING,
    BranchState.RUNNING,
    BranchState.WAITING_INPUT,
)

#: 6.4 / A13.7：可由 ``record_outcome`` 关闭的**四个**终态。
#: ⚠️ **不含** ``BranchState.REJECTED`` —— 准入拒绝不写运行态（A13.1）；
#: ``STOPPED`` 只由 ``apply_runtime_control`` 写。
_OUTCOME_STATES = (
    BranchState.COMPLETED,
    BranchState.FAILED,
    BranchState.ADOPTED,
    BranchState.OVERRIDDEN,
)


class AgentManagerError(RuntimeError):
    """``[10]`` 的显式失败：非法的生命周期迁移 / 越界输入 / 超过 attempt 上限。"""


def _agent_run(
    *,
    run_id: RunId,
    branch_id: BranchId,
    agent_id: str,
    attempt: int,
    state: BranchState,
    started_at: str | None = None,
    ended_at: str | None = None,
    handle: str | None = None,
) -> AgentRun:
    """**唯一**的 ``AgentRun`` 构造点（全 ``src/`` 只有这一处）。"""
    return AgentRun(
        run_id=run_id,
        branch_id=branch_id,
        agent_id=agent_id,
        attempt=attempt,
        state=state,
        started_at=started_at,
        ended_at=ended_at,
        handle=handle,
    )


def _latest_by_branch(records: tuple[AgentRun, ...]) -> dict[BranchId, AgentRun]:
    """每个 branch 的**最新 attempt** 记录（A13.3：``branches`` 是 attempt history）。

    ⚠️ 这里**不用** ``Mapping.get`` —— 本模块对映射一律显式判存在（L13：禁静默默认）。
    """
    latest: dict[BranchId, AgentRun] = {}
    for record in records:
        current = latest[record.branch_id] if record.branch_id in latest else None
        if current is None or record.attempt > current.attempt:
            latest[record.branch_id] = record
    return latest


class AgentManager:
    """``[10]`` 的运行态写者：把调度决定**物化**为 ``AgentRun`` 生命周期。"""

    def __init__(
        self,
        *,
        run_id: RunId,
        branch_order: tuple[BranchId, ...],
        total_attempt_limit: int,
    ) -> None:
        """``branch_order`` = **注入的 ``Plan.branches`` 顺序**（唯一顺序来源）。

        ``[10]`` 不重新规划、不从 ``goal`` 推导顺序、不依赖 Adapter 返回顺序。
        ``DIRECT`` / ``STAGED_SINGLE_AGENT``（0 child）⇒ 空 tuple 是**正常输入**。
        """
        validate_run_id(run_id)
        if not isinstance(branch_order, tuple):
            raise AgentManagerError(f"branch_order 必须是 tuple，收到 {branch_order!r}")
        for branch_id in branch_order:
            validate_branch_id(branch_id)
        if len(set(branch_order)) != len(branch_order):
            raise AgentManagerError("branch_order 不得含重复 branch_id（Plan.branches 顺序）")
        if type(total_attempt_limit) is not int or total_attempt_limit < 1:
            raise AgentManagerError(
                f"total_attempt_limit 必须是 >= 1 的 int，收到 {total_attempt_limit!r}"
            )
        self._run_id = run_id
        self._branch_order = branch_order
        self._total_attempt_limit = total_attempt_limit
        #: **P2**：Run 级 attempt 计数（初值 1；**唯一推进点** = `commit_run_attempt`）。
        #: ⛔ 不读 `total_attempt_limit`、⛔ 不由 payload / 恢复决策派生。
        self._run_attempt: int = 1
        self._records: list[AgentRun] = []
        #: **A18.2**：Run 级终态（``RunTerminal | None``）。**只有 ``close_run`` 能写**。
        self._terminal: RunTerminal | None = None
        #: **A18.2 / A17.7**：两个收敛事实之 AND。**只有 ``close_run`` 能写**（不得由 PASS / 决策 / 默认推断）。
        self._control_flow_converged: bool = False

    # ------------------------------------------------------------------
    # 只读投影
    # ------------------------------------------------------------------

    @property
    def routing_run(self) -> RoutingRun:
        """当前运行态的**不可变快照**（``branches`` = attempt history，A13.3）。

        ``terminal`` / ``control_flow_converged`` 由 :meth:`close_run` 写入（**A18.2**）：
        投影**只读**这两个内部状态，二者**都不是**本属性的判断对象（4.5 的双条件在写入前校验）。

        ⏸ Run 级 ``attempt`` 的推进**仍不在本轮**（Phase A 不处理 attempt lifecycle ——
        A18 授权边界明令禁止触碰 attempt lifecycle）⇒ 这里固定 ``attempt=1``。
        """
        return RoutingRun(
            run_id=self._run_id,
            branches=tuple(self._records),
            terminal=self._terminal,
            attempt=self._run_attempt,
            total_attempt_limit=self._total_attempt_limit,
            control_flow_converged=self._control_flow_converged,
        )

    @property
    def active_subagent_count(self) -> int:
        """A13.4：各 branch 最新 attempt 中状态为 ``RUNNING`` 或 ``WAITING_INPUT`` 的数量。"""
        latest = _latest_by_branch(tuple(self._records))
        return sum(
            1
            for branch_id in self._branch_order
            if branch_id in latest and latest[branch_id].state in _LIVE_STATES
        )

    @property
    def pending_admission_branch_ids(self) -> tuple[BranchId, ...]:
        """A13.4：**后续 pass** 的 ``branches_under_admission`` 来源。

        按注入的 ``Plan.branches`` 顺序投影、取"最新 attempt 状态为 ``WAITING``"的 branch。
        ⚠️ **首次 pass 不是这个投影**（A11.3c：首次 = ``Plan.branches`` 全部 child）。

        ``REJECT`` / ``REUSE`` 没有 ``AgentRun`` ⇒ **天然不会重新进入 pending**；
        已发出 ``START`` 许可但尚未收到启动事实的 branch **也没有记录** ⇒ 同样不在 pending，
        因此**不会**被重复派发。
        """
        latest = _latest_by_branch(tuple(self._records))
        return tuple(
            branch_id
            for branch_id in self._branch_order
            if branch_id in latest and latest[branch_id].state is BranchState.WAITING
        )

    # ------------------------------------------------------------------
    # 物化
    # ------------------------------------------------------------------

    def materialize(
        self,
        *,
        decisions: tuple[DispatchDecision, ...],
        bundles: Mapping[BranchId, ContextBundle],
    ) -> tuple[AgentStartRequest, ...]:
        """把一批调度决定物化；返回需要 P1 派发的 ``AgentStartRequest``（按 ``branch_order``）。

        ``START`` ⇒ 不写记录、产出一个请求；``QUEUE`` ⇒ 写 ``WAITING``；
        ``REJECT`` / ``REUSE`` ⇒ 不写记录（A13.1 / A13.2）。
        """
        if not isinstance(decisions, tuple):
            raise AgentManagerError(f"decisions 必须是 tuple，收到 {decisions!r}")
        by_branch: dict[BranchId, DispatchDecision] = {}
        for decision in decisions:
            if not isinstance(decision, DispatchDecision):
                raise AgentManagerError(
                    f"decisions 元素必须是 DispatchDecision，收到 {decision!r}"
                )
            if decision.branch_id not in self._branch_order:
                raise AgentManagerError(
                    f"决定指向 Plan.branches 之外的 child（越界 {decision.branch_id}）"
                )
            if decision.branch_id in by_branch:
                raise AgentManagerError(
                    f"同一 pass 内不得对 {decision.branch_id} 重复裁决（A11.3c）"
                )
            by_branch[decision.branch_id] = decision

        # 输出顺序**一律投影注入的 Plan.branches 顺序**，与输入顺序无关（A11.3c ②）
        starts: list[AgentStartRequest] = []
        for branch_id in self._branch_order:
            if branch_id not in by_branch:
                continue
            decision = by_branch[branch_id]
            if decision.action is DispatchAction.START:
                starts.append(self._licence_start(branch_id, bundles))
            elif decision.action is DispatchAction.QUEUE:
                self._materialize_queue(branch_id)
            elif decision.action is DispatchAction.REJECT:
                self._drop_never_dispatched(branch_id, decision, label="REJECT")
            elif decision.action is DispatchAction.REUSE:
                self._drop_never_dispatched(branch_id, decision, label="REUSE")
            else:  # pragma: no cover - DispatchAction 是闭集，新增成员必须在这里显式表态
                raise AgentManagerError(f"未覆盖的 DispatchAction：{decision.action!r}")
        return tuple(starts)

    def _licence_start(
        self, branch_id: BranchId, bundles: Mapping[BranchId, ContextBundle]
    ) -> AgentStartRequest:
        """``START``：只产出**派发许可**，**不写任何记录**（A13.2）。

        - 无记录 ⇒ attempt 1（首次执行）；
        - 最新为 ``WAITING`` ⇒ **同一个 attempt**（提升，**不得**新建 attempt）；
        - 最新已终止 ⇒ 下一个 attempt（重建 / 重试），**不越过 ``total_attempt_limit``**；
        - 最新仍在运行 ⇒ 显式失败（同一 child 不得并发运行）。
        """
        latest = self._latest(branch_id)
        if latest is None:
            attempt = 1
        elif latest.state is BranchState.WAITING:
            attempt = latest.attempt
        elif not latest.is_terminal:
            raise AgentManagerError(
                f"{branch_id} 的 attempt {latest.attempt} 仍在 {latest.state.value}，"
                "不得再次派发（同一 child 不得并发运行）"
            )
        elif latest.attempt >= self._total_attempt_limit:
            raise AgentManagerError(
                f"{branch_id} 已达 attempt 上限 {self._total_attempt_limit}"
                "（A13.7：达到上限即为终态，不得再开新 attempt）"
            )
        else:
            attempt = latest.attempt + 1

        if branch_id not in bundles:
            raise AgentManagerError(
                f"START 的 child 必须有 prepared ContextBundle（缺少 {branch_id}）"
            )
        return AgentStartRequest(
            branch_id=branch_id, attempt=attempt, bundle=bundles[branch_id]
        )

    def _materialize_queue(self, branch_id: BranchId) -> None:
        """``QUEUE`` ⇒ ``AgentRun(state=WAITING)``（不依赖平台，可立即物化）。"""
        latest = self._latest(branch_id)
        if latest is None:
            self._records.append(
                _agent_run(
                    run_id=self._run_id,
                    branch_id=branch_id,
                    agent_id=new_agent_id(),
                    attempt=1,
                    state=BranchState.WAITING,
                )
            )
            return
        if latest.state is BranchState.WAITING:
            return  # 已经排队 ⇒ 幂等
        raise AgentManagerError(
            f"{branch_id} 的最新 attempt 是 {latest.state.value}，不得再物化 QUEUE"
        )

    def _drop_never_dispatched(
        self, branch_id: BranchId, decision: DispatchDecision, *, label: str
    ) -> None:
        """``REJECT`` / ``REUSE`` ⇒ 该 child **不得留下** ``AgentRun``（A13.1）。

        两者都不产生 agent，因此**都不建记录**。若此前因 ``QUEUE`` 建过一条
        **从未派发**的 ``WAITING`` 占位，则那个 attempt 从未成为 agent ⇒ **移除它**，
        使 A13.1 的不变量成立，也避免 pending 投影**永不收敛**。

        **已经进入运行生命周期**（``RUNNING`` / ``WAITING_INPUT``）或已终止的记录
        **不得**被抹掉 ⇒ 显式失败。
        """
        latest = self._latest(branch_id)
        if latest is None:
            return
        if latest.state is BranchState.WAITING:
            self._records = [
                record
                for record in self._records
                if not (record.branch_id == branch_id and record.attempt == latest.attempt)
            ]
            return
        raise AgentManagerError(
            f"{branch_id} 已有 {latest.state.value} 的运行记录，{label} 不得抹掉它"
            f"（只有从未派发的 WAITING 占位可移除；{decision.reason}）"
        )

    # ------------------------------------------------------------------
    # 生命周期提交（唯一写 RUNNING 的入口）
    # ------------------------------------------------------------------

    def commit_started(
        self,
        *,
        branch_id: BranchId,
        attempt: int,
        handle: str,
        started_at: str,
    ) -> None:
        """**唯一**把某个 attempt 写成 ``RUNNING`` 的入口（A13.6）。

        ``handle`` **只能**来自显式 runtime / adapter 事实；``started_at`` 由调用方传入
        （本模块**不读时钟**）。没有启动事实时，本方法**不应被调用**。
        """
        validate_branch_id(branch_id)
        if type(attempt) is not int or attempt < 1:
            raise AgentManagerError(f"attempt 必须是 >= 1 的 int，收到 {attempt!r}")
        if type(handle) is not str or not handle.strip():
            raise AgentManagerError(
                f"handle 必须是非空 str（来自显式 adapter 事实），收到 {handle!r}"
            )
        if type(started_at) is not str or not started_at.strip():
            raise AgentManagerError(
                f"started_at 必须是非空 str（显式传入），收到 {started_at!r}"
            )

        latest = self._latest(branch_id)
        if latest is None:
            # 首次派发：START 阶段**没有**建记录，成功事实到达时才创建（A13.2）
            if attempt != 1:
                raise AgentManagerError(
                    f"{branch_id} 没有更早 attempt 的历史，不得从 attempt {attempt} 开始"
                )
            self._records.append(
                _agent_run(
                    run_id=self._run_id,
                    branch_id=branch_id,
                    agent_id=new_agent_id(),
                    attempt=attempt,
                    state=BranchState.RUNNING,
                    started_at=started_at,
                    handle=handle,
                )
            )
            return

        if latest.attempt == attempt:
            if latest.state is not BranchState.WAITING:
                raise AgentManagerError(
                    f"{branch_id} 的 attempt {attempt} 处于 {latest.state.value}，"
                    "只有 WAITING 才能被提交为 RUNNING"
                )
            # WAITING → RUNNING：**更新同一个 attempt**，沿用 agent_id（不重新 mint）
            self._replace(
                latest,
                _agent_run(
                    run_id=self._run_id,
                    branch_id=branch_id,
                    agent_id=latest.agent_id,
                    attempt=attempt,
                    state=BranchState.RUNNING,
                    started_at=started_at,
                    handle=handle,
                ),
            )
            return

        if latest.is_terminal and attempt == latest.attempt + 1:
            # **重建 / 重试**：前一 attempt 已终止 ⇒ 追加**新** attempt 记录（A13.3 第 5 条）
            if attempt > self._total_attempt_limit:
                raise AgentManagerError(
                    f"{branch_id} 已达 attempt 上限 {self._total_attempt_limit}"
                    "（A13.7：达到上限即为终态，不得再开新 attempt）"
                )
            self._records.append(
                _agent_run(
                    run_id=self._run_id,
                    branch_id=branch_id,
                    agent_id=new_agent_id(),
                    attempt=attempt,
                    state=BranchState.RUNNING,
                    started_at=started_at,
                    handle=handle,
                )
            )
            return

        raise AgentManagerError(
            f"{branch_id} 的最新 attempt 是 {latest.attempt}，不是 {attempt}"
            "（只允许提交最新 attempt，或在它已终止后开下一个 attempt）"
        )

    def record_outcome(
        self,
        *,
        branch_id: BranchId,
        attempt: int,
        state: BranchState,
        ended_at: str,
    ) -> None:
        """把**正在运行**的 attempt 关闭为四个终态之一（6.4 / A13.7）。

        ⚠️ 本方法**只写状态**：终态**分类**来自 ``[12]`` / ``[13]`` / ``[14]``，
        ``[10]`` **不决定** retry policy、**不解析**外部状态、**不生成** adapter receipt。
        """
        validate_branch_id(branch_id)
        if state not in _OUTCOME_STATES:
            raise AgentManagerError(
                f"record_outcome 只接受四个终态 {[item.value for item in _OUTCOME_STATES]}，"
                f"收到 {state!r}（准入拒绝见 A13.1；STOPPED 只由 stop-all 写）"
            )
        if type(ended_at) is not str or not ended_at.strip():
            raise AgentManagerError(f"ended_at 必须是非空 str（显式传入），收到 {ended_at!r}")

        latest = self._latest(branch_id)
        if latest is None:
            raise AgentManagerError(f"{branch_id} 没有任何 attempt 记录，无法关闭")
        if latest.attempt != attempt:
            raise AgentManagerError(
                f"{branch_id} 的最新 attempt 是 {latest.attempt}，不是 {attempt}"
            )
        if latest.state not in _LIVE_STATES:
            raise AgentManagerError(
                f"{branch_id} 的 attempt {attempt} 处于 {latest.state.value}，"
                "只有 RUNNING / WAITING_INPUT 才能关闭为终态"
            )
        self._replace(
            latest,
            _agent_run(
                run_id=self._run_id,
                branch_id=branch_id,
                agent_id=latest.agent_id,
                attempt=attempt,
                state=state,
                started_at=latest.started_at,
                ended_at=ended_at,
                handle=latest.handle,
            ),
        )

    def apply_runtime_control(
        self,
        *,
        facts: RuntimeControlFacts,
        ended_at: str,
    ) -> tuple[BranchId, ...]:
        """执行上游运行期控制命令（A13.5）；返回被停下的 branch（按 ``branch_order``）。

        ``stop_branches_requested is False`` ⇒ **不产生任何停止动作**
        （**不得**读成"禁止停止"）。``True`` ⇒ **当前 RoutingRun 的 stop-all**：
        对**已存在**的 ``WAITING`` / ``RUNNING`` / ``WAITING_INPUT`` 记录写 ``STOPPED``。

        ⚠️ ``[10]`` 执行 STOP **不是第二个 Scheduler** —— 它是**显式用户控制命令**；
        ``REJECT`` / ``REUSE`` **没有运行记录可写**。
        """
        if not isinstance(facts, RuntimeControlFacts):
            raise AgentManagerError(f"facts 必须是 RuntimeControlFacts，收到 {facts!r}")
        if type(ended_at) is not str or not ended_at.strip():
            raise AgentManagerError(f"ended_at 必须是非空 str（显式传入），收到 {ended_at!r}")
        if facts.stop_branches_requested is False:
            return ()

        stopped: list[BranchId] = []
        for branch_id in self._branch_order:
            latest = self._latest(branch_id)
            if latest is None or latest.state not in _STOPPABLE_STATES:
                continue
            self._replace(
                latest,
                _agent_run(
                    run_id=self._run_id,
                    branch_id=branch_id,
                    agent_id=latest.agent_id,
                    attempt=latest.attempt,
                    state=BranchState.STOPPED,
                    started_at=latest.started_at,
                    ended_at=ended_at,
                    handle=latest.handle,
                ),
            )
            stopped.append(branch_id)
        return tuple(stopped)

    # ------------------------------------------------------------------
    # C2c 只读事实接口 + 用户恢复写路径（`5B.6.28`；A13.6 边界内的**独立** seam）
    #
    # ⚠️ 这两个成员**刻意**集中在此（**不**插进上面的只读投影区）：既有 PA-7a 锁
    #    `test_terminal.py::TestPA7FrozenRegionsUnchanged::test_declared_updates_are_the_declared_ones`
    #    里有一个**朴素子串解析器** `SOURCE.split("    def routing_run(")[1].split("\n    @property")[0]`，
    #    它假定 `routing_run` 与下一个 `@property` 之间**只有** `routing_run` 本体。
    #    把新成员插在那两者之间会**改读**该解析器的语义 ⇒ 按"只更新被授权改动所必然改变的常量"的口径，
    #    改为**调整插入位置**（**不动**该解析器）。
    # ------------------------------------------------------------------

    def branch_record(self, branch_id: BranchId) -> AgentRun | None:
        """该 branch 的**最新 attempt 记录**，**只读**事实接口（C2c 的 read seam）。

        ⚠️ **只读、不做判定**：不解释该记录处于什么状态、**不**替调用方下结论
        （"absence of observation ≠ fact" ⇒ 无记录返回 ``None``，由调用方**显式**处理）。

        ⚠️ **不是**批量查询、**不是**面向 Scheduler 的投影：一次只取一个 branch 的现状。
        """
        validate_branch_id(branch_id)
        return self._latest(branch_id)

    def resume_from_waiting(
        self,
        *,
        branch_id: BranchId,
        attempt: int,
    ) -> AgentRun:
        """**唯一**把同一个 attempt 从 ``WAITING_INPUT`` 写回 ``RUNNING`` 的入口（C2c）。

        ⚠️ **不是** attempt 推进入口：入参 ``attempt`` 必须**等于**该 branch 最新记录的
        ``attempt`` ⇒ 写回**同一个** attempt，**不新建** attempt 记录（``_records`` 长度不变）。
        Run 级 ``attempt`` 亦**不在此推进**（仍在 ``routing_run`` 投影里固定为 1；见 `P2`）。

        ⚠️ **用户恢复 ≠ 失败恢复**：失败回流的 attempt ``+1`` 只走 ``[14] Recovery`` →
        ``commit_started``；本方法**不共用** ``commit_started``，**不消耗**重试预算。

        沿用原值（**只有 ``state`` 改变**）：``agent_id``（不重新 mint）· ``started_at`` ·
        ``handle``；``ended_at`` 保持原值（恢复不是结束）。本模块**不读时钟**。

        ``WAITING_INPUT`` 的**唯一**合法前置；其余状态（含 ``RUNNING`` ⇒ 重复消费）
        ⇒ :class:`AgentManagerError`。
        """
        validate_branch_id(branch_id)
        if type(attempt) is not int or attempt < 1:
            raise AgentManagerError(f"attempt 必须是 >= 1 的 int，收到 {attempt!r}")

        latest = self._latest(branch_id)
        if latest is None:
            raise AgentManagerError(
                f"{branch_id} 没有任何 attempt 记录，无法恢复（没有可恢复的运行态事实）"
            )
        if latest.attempt != attempt:
            raise AgentManagerError(
                f"{branch_id} 的最新 attempt 是 {latest.attempt}，不是 {attempt}"
                "（用户恢复**不推进** attempt：必须与最新记录一致）"
            )
        if latest.state is not BranchState.WAITING_INPUT:
            raise AgentManagerError(
                f"{branch_id} 的 attempt {attempt} 处于 {latest.state.value}，"
                "只有 WAITING_INPUT 才能被恢复为 RUNNING"
                "（已 RUNNING ⇒ 重复消费一个已用过的授权事实）"
            )

        resumed = _agent_run(
            run_id=self._run_id,
            branch_id=branch_id,
            agent_id=latest.agent_id,
            attempt=attempt,
            state=BranchState.RUNNING,
            started_at=latest.started_at,
            ended_at=latest.ended_at,
            handle=latest.handle,
        )
        self._replace(latest, resumed)
        return resumed

    # ------------------------------------------------------------------
    # `5B.6.51.3`（`P6` 前置）· **start-side failure commit**（A13.6 边界内的**窄** seam）
    #
    # ⚠️ 与 `commit_started` **对偶**，但**只**承接"已派发、从未进入 `RUNNING`"的 non-capability
    #    失败：封闭状态面 = `latest is None`（T1）或 `latest.state is WAITING`（T2）；
    #    `RUNNING` / `WAITING_INPUT` / 任一 terminal ⇒ **显式失败**（那些属 `record_outcome`
    #    的语义 ⇒ ⛔ 本入口**不得**成为第二套通用 terminal writer）。
    # ⚠️ `[10]` ⛔ **不 import `adapters`**、⛔ **不判定** failure 分类 —— 只接受调用方
    #    显式传入的 settlement scalar facts（失败事实的 `timestamp → ended_at` 映射发生在
    #    **调用边界**，属 wiring 的职责）。
    # ⚠️ 源码里 ⛔ **不得出现**适配层类型名（既有冻结锁
    #    `test_adapter_contract.py::TestL11_7_agent_manager_stays_clean` 是**子串**扫描）⇒
    #    本注释与 docstring 一律只描述"标量事实"，⛔ 不写任何适配层符号名。
    # ⚠️ 插入位置（硬约束）：⛔ 不得插在 `routing_run` 与下一个 `@property` 之间
    #    （`test_terminal.py` 的朴素子串解析器）⇒ 与 `branch_record` / `resume_from_waiting` 同区。
    # ------------------------------------------------------------------

    def commit_start_failure(
        self,
        *,
        branch_id: BranchId,
        attempt: int,
        ended_at: str,
    ) -> None:
        """把一次**已派发但从未进入 RUNNING**的 non-capability 失败落实为 terminal FAILED attempt。

        ⚠️ **sealed state surface**：只接受 `latest is None` 与 `latest.state is WAITING`；
           `RUNNING` / `WAITING_INPUT` / 任一 terminal ⇒ **显式失败**（那些属 `record_outcome` 的语义）。
        ⚠️ `agent_id`：**仅当该 attempt 尚不存在历史记录时**由 `[10]` mint；已有 `WAITING` record 时**沿用**。
        ⚠️ `ended_at`：**只收显式 scalar**（调用边界已把失败事实的时间戳映射进来）。
        """
        validate_branch_id(branch_id)
        if type(attempt) is not int or attempt < 1:
            raise AgentManagerError(f"attempt 必须是 >= 1 的 int（拒 bool），收到 {attempt!r}")
        if type(ended_at) is not str or not ended_at.strip():
            raise AgentManagerError(f"ended_at 必须是非空 str（显式传入），收到 {ended_at!r}")

        latest = self._latest(branch_id)
        if latest is None:
            # T1 · NO RECORD：**先**判 first-attempt 语义（在 licence 规则下必为 1）
            if attempt != 1:
                raise AgentManagerError(
                    f"{branch_id} 没有任何更早 attempt 的历史，"
                    f"不得从 attempt {attempt} 开始（T1 只接受 attempt == 1）"
                )
            self._records.append(
                _agent_run(
                    run_id=self._run_id,
                    branch_id=branch_id,
                    agent_id=new_agent_id(),
                    attempt=attempt,
                    state=BranchState.FAILED,
                    started_at=None,
                    ended_at=ended_at,
                    handle=None,
                )
            )
            return

        # T2 · 已有 record：**先** exact attempt identity，**再** WAITING（冻结顺序）
        if latest.attempt != attempt:
            raise AgentManagerError(
                f"{branch_id} 的最新 attempt 是 {latest.attempt}，不是 {attempt}"
            )
        if latest.state is not BranchState.WAITING:
            raise AgentManagerError(
                f"{branch_id} 的 attempt {attempt} 处于 {latest.state.value}；"
                "本入口只接受从未进入 RUNNING 的已派发失败（WAITING）"
            )
        self._replace(
            latest,
            _agent_run(
                run_id=self._run_id,
                branch_id=branch_id,
                agent_id=latest.agent_id,
                attempt=attempt,
                state=BranchState.FAILED,
                started_at=latest.started_at,
                ended_at=ended_at,
                handle=latest.handle,
            ),
        )

    def commit_run_attempt(
        self,
        *,
        attempt: int,
    ) -> None:
        """把**已判定**的 Run 级推进（→ attempt N）落实为运行态（P2 · `[10]` 唯一写点）。

        ⚠️ **不判定**：是否应当推进由 `[14]` 的决策 + 调用方显式动作给出；本方法 ⛔ 不 import / 不读
           `RecoveryOutcome` / `RecoveryDecision`、⛔ 不自行判 `should_recover`、⛔ **不 +1** / ⛔ 不 clamp /
           ⛔ 不 dedupe（caller 送来的 `N` **原样**落入运行态）。
        ⚠️ `RoutingRun.attempt` **不受** `total_attempt_limit` 约束（该预算是**分支级**的）。
        ⚠️ 四条守卫顺序固定：形状 → 未收口 → 分支全终止（4.5 条件①）→ **单调 +1**。
        """
        if type(attempt) is not int or attempt < 1:
            raise AgentManagerError(f"attempt 必须是 >= 1 的 int（拒 bool），收到 {attempt!r}")
        if self._terminal is not None:
            raise AgentManagerError(
                f"本 Run 已收口为 {self._terminal.state.value}，不得再推进 attempt"
                "（terminal 之后不得再开新轮 —— INV-21 / LT-5）"
            )
        unfinished = [record.branch_id for record in self._records if not record.is_terminal]
        if unfinished:
            raise AgentManagerError(
                f"分支尚未全部终止，不得推进 Run 级 attempt（未终止：{unfinished}）（4.5 条件①）"
            )
        if attempt != self._run_attempt + 1:
            raise AgentManagerError(
                f"Run 级 attempt 必须单调 +1：当前 {self._run_attempt}，收到 {attempt}"
                "（⛔ 不跳号 · ⛔ 不回退）"
            )
        self._run_attempt = attempt

    def close_run(
        self,
        *,
        terminal_state: TerminalState,
        no_pending_approval: bool,
        no_pending_recovery: bool,
        ended_at: str,
    ) -> None:
        """Run 级收口 —— **A18.2 的唯一终态写入口**（Phase A：frozen artifact extension）。

        **输入全部显式、一个都不推导**（A18.2 / A18.4）：

            ``terminal_state``        调用方（编排点）给出；``[10]`` **永不自算**
            ``no_pending_approval``   Approval 链（Judgment 结论 + Gate 解析状态）
            ``no_pending_recovery``   持有未决恢复的编排点
            ``ended_at``              调用方给出（**只校验、不落库** —— 契约未含 Run 级时间字段）

        **三条禁止（逐字，来自 A18.2）：**

            ❌ ``close_run(success=True)`` / ``close_run()``  —— 不得由布尔或空参推导终态
            ❌ ``[10]`` 自己看 branch 状态"算出" terminal_state —— state writer ≠ decision maker
            ❌ 缺任一 fact 仍写 terminal —— 4.5 双条件在此**写入前**校验

        ⚠️ **为什么在写入前校验**：若依赖"读取投影时由契约报错"，就会出现**写入成功、读取爆炸**
        的半合法状态（``RoutingRun.__post_init__`` 的强制发生在**构造时**，而投影是后置读取）。

        ⚠️ **Run 级 ``attempt`` 不在本方法内推进**（Phase A 授权边界禁止触碰 attempt lifecycle）⇒
        ``routing_run`` 仍投影 ``attempt=1``；A18.7 的"``[14]`` 决策 → ``[10]`` 写状态"
        **尚无方法承载**，已登记为遗留。
        """
        if not isinstance(terminal_state, TerminalState):
            raise AgentManagerError(
                f"terminal_state 必须是 TerminalState，收到 {terminal_state!r}"
                "（不接受 str / BranchState / 其它枚举 —— A18.2）"
            )
        for name, value in (
            ("no_pending_approval", no_pending_approval),
            ("no_pending_recovery", no_pending_recovery),
        ):
            if type(value) is not bool:
                raise AgentManagerError(
                    f"{name} 必须是 bool（严格 type is bool），收到 {value!r}"
                    "（「缺省即真」与 0/1/「true」都不是收敛事实 —— A18.4 禁止 3 / 4）"
                )
        if type(ended_at) is not str or not ended_at.strip():
            raise AgentManagerError(
                f"ended_at 必须是非空 str（显式传入），收到 {ended_at!r}"
            )

        # C2：重复收口 ⇒ 显式失败（LT-5：terminal 之后不得再有 pending / 不得再次收口）
        if self._terminal is not None:
            raise AgentManagerError(
                f"本 Run 已收口为 {self._terminal.state.value}，不得再次 close_run"
                "（terminal 之后不得存在 pending control state —— INV-21 / LT-5）"
            )

        # C1：4.5 双条件 —— **写入前**显式校验（C3：0 分支时条件①真空成立，合法）
        unfinished = [record.branch_id for record in self._records if not record.is_terminal]
        if unfinished:
            raise AgentManagerError(
                f"分支尚未全部终止，不得进入 Run 终态（未终止：{unfinished}）"
                "（4.5 条件①）"
            )
        if not (no_pending_approval and no_pending_recovery):
            raise AgentManagerError(
                "控制流尚未收敛，不得进入 Run 终态"
                f"（no_pending_approval={no_pending_approval} · "
                f"no_pending_recovery={no_pending_recovery} —— 4.5 条件② / A18.2）"
            )

        # 唯一写点：收敛位 + Run 终态（RunTerminal( 的**唯一构造点**，LT-1）
        self._control_flow_converged = no_pending_approval and no_pending_recovery
        self._terminal = RunTerminal(run_id=self._run_id, state=terminal_state)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _latest(self, branch_id: BranchId) -> AgentRun | None:
        """该 branch 的**最新 attempt** 记录；没有则 ``None``。"""
        found: AgentRun | None = None
        for record in self._records:
            if record.branch_id != branch_id:
                continue
            if found is None or record.attempt > found.attempt:
                found = record
        return found

    def _replace(self, previous: AgentRun, updated: AgentRun) -> None:
        """原地替换**同一条** attempt 记录（保持 attempt history 的插入顺序）。"""
        for index, record in enumerate(self._records):
            if record.branch_id == previous.branch_id and record.attempt == previous.attempt:
                self._records[index] = updated
                return
        raise AgentManagerError(
            f"内部错误：未找到 {previous.branch_id} 的 attempt {previous.attempt} 记录"
        )
