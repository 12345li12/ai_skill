"""``C2d-1 DENY 收口执行链``（`已解析的 DENY 授权结果 → Run 收口为 APPROVAL_DENIED`）。

架构依据：`docs/architecture.md` **2 节 Approval Gate 节点行**（"三种输入的控制流出口（V1 冻结）"：
`DENY → 终态 APPROVAL_DENIED`）· **6.5 终态枚举**（`APPROVAL_DENIED` = 审批未通过，依据 L43）·
**`INV-17`**（终态取自 6 值枚举）· **4.5 双条件**（分支全终止 + 控制流收敛）·
**A18.2**（`[10]` 是 `RunTerminal` 的唯一写者）· **A18.3**（两个收敛事实的**唯一合法来源**）·
**A13.1 / A13.6**（branch 级终态的写入口边界）· **6.4 / L47**（`[10]` 运行态唯一写者）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§164**（Scope Review）/ **§165**（范围稿冻结）
/ **§166**（实现授权）/ **§167**（实现）。

**本模块要守住的五条主张：**

  1. **不重新定义 DENY 语义**：`DENY → APPROVAL_DENIED` 早已冻结；本模块**只补机械执行者**，
     **不引入**新的 `TerminalState`、**不扩** `RouterOutcome`、**不新增**组合根（`LDW-5`）；
  2. **单一收敛路径**：只经 `terminal_wiring.converge_run` 进入 `[10].close_run`
     —— `close_run` 的**机械调用点全仓唯一**（在 `terminal_wiring.py` 内，`LTW-1` 不变量），
     本模块**不**成为第二个调用点（`LDW-3`，裁定 = `C2d-1 implementation seam adjustment`）；
  3. **两个收敛事实都是"观测"**：分别取自 `gate_outcome.is_pending` 与 `manager.routing_run.branches`
     ⇒ **不是** caller assertion，**也不是**写死的常量（`LDW-6'`；架构 `:2351` 的硬禁令）；
     且**只对"派发前 DENY"这一格成立**，**不**声称"系统已具备恢复跟踪"；
  4. **架构时序保护**：`branches == ()` 是**机械断言**（Approval Gate 位于执行派发之前）
     —— 将来若 Gate 被移到 `dispatch` 之后，本模块会**报错**而**不是静默收口**（`LDW-12`）；
  5. **零出边 / 零时钟 / 零 `try`**：不选下一节点、不接 Scheduler、不执行节点；`ended_at` 由调用方传入。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `dispatch` / `scheduler` / `router` / `attempt`
   等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。
⚠️ **不构造** `GateOutcome` / `GateDecision` / `RoutingRun` / `RunTerminal` / `RecoveryDecision`；
   `GateDecisionKind` 只作**枚举比较**（`LDW-11`）。
⚠️ **不写 branch 级终态**：`TerminalState.APPROVAL_DENIED` **不在** `_BRANCH_TERMINAL_MAP` 里，
   且 DENY 发生在派发之前（无 branch）⇒ 本模块**不碰** `record_outcome`（`LDW-3`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .agent_manager import AgentManager
from .approval_gate_wiring import GateDecisionKind, GateOutcome
from .contracts import RunId, TerminalState
from .terminal_wiring import converge_run

__all__ = ["DenyWiringError", "DenySettlement", "settle_denied_run"]


class DenyWiringError(RuntimeError):
    """``C2d-1`` 的显式失败：非法授权结果 / 非法时序 / 非法入参。"""


@dataclass(frozen=True)
class DenySettlement:
    """``DENY → Run 收口`` 的**结果 + 验证回执**（`§165` 冻结的**五字段**，多一个都不许）。

    ⚠️ ``gate_settled`` 与 ``branch_count`` 是**验证回执字段**（**不是**业务数据）：
    ``gate_settled`` 证明 DENY 收敛动作**确实执行**；``branch_count`` 证明 **C3 真空条件实际成立**
    （0 分支 ⇒ 4.5 条件①真空成立）。
    ⚠️ 本载体**不含**任何 ``next_node`` / ``dispatch`` / ``scheduler`` 语义 —— `C2d-1` 只收口，**不续跑**。
    """

    run_id: RunId
    terminal_state: TerminalState
    gate_settled: bool
    ended_at: str
    branch_count: int


def settle_denied_run(
    *,
    gate_outcome: GateOutcome,
    manager: AgentManager,
    ended_at: str,
) -> DenySettlement:
    """把**已解析的 `DENY` 授权结果**落成一次 Run 收口（`APPROVAL_DENIED`）。

    ⚠️ **入参恰好 3 个**（`LDW-6`）：**没有** `no_pending_*` —— 两个收敛事实由**观测**得出
       （`LDW-6'`），不得由调用方断言（架构采用 A18.5 批准的 producer：Approval 侧 = 结论 + 解析状态）。
    ⚠️ **唯一写入**是 `terminal_wiring.converge_run` → `[10].close_run`（`RunTerminal` 的唯一写入口，
       A18.2 / `LTW-1`）；`converge_run` **原样透传**四个事实、**不推导 / 不伪造 / 不读时钟**；
       本函数**恰好 1 个 return**。
    ⚠️ **函数体内零局部绑定**：所有守卫与观测一律**内联嵌套**（沿用 `approval_answer.py` /
       `resume_wiring.py` 的纪律）。
    ⚠️ **`APPROVE` / `MODIFY` ⇒ 显式失败**：三者各有自己的出口（`APPROVE` ⇒ 按获批范围继续；
       `MODIFY` ⇒ 回 `[02]`），**不得** fallback 到收口。
    """
    if not isinstance(gate_outcome, GateOutcome):
        raise DenyWiringError(f"gate_outcome 必须是 GateOutcome，收到 {gate_outcome!r}")
    if gate_outcome.is_pending is not False:
        raise DenyWiringError(
            "授权事实仍在挂起态（is_pending is not False）⇒ 人类还没回答，"
            "不存在可收口的 DENY 事实"
        )
    if gate_outcome.decision.kind is not GateDecisionKind.DENY:
        raise DenyWiringError(
            f"本边只收口 DENY，收到 {gate_outcome.decision.kind.value}"
            "（APPROVE ⇒ 按获批范围继续；MODIFY ⇒ 回 [02] —— 三者各有自己的出口，不得 fallback）"
        )
    if type(ended_at) is not str or not ended_at.strip():
        raise DenyWiringError(f"ended_at 必须是非空 str（调用方显式传入），收到 {ended_at!r}")
    if not isinstance(manager, AgentManager):
        raise DenyWiringError(f"manager 必须是 AgentManager，收到 {manager!r}")
    if manager.routing_run.branches != ():
        raise DenyWiringError(
            f"DENY 收口要求运行态**没有任何 branch**，实测 {len(manager.routing_run.branches)} 条"
            "（Approval Gate 位于执行派发之前 ⇒ 此刻不应存在 attempt 记录；"
            "若 Gate 被移到 dispatch 之后，本守卫会报错而**不是**静默收口）"
        )
    converge_run(
        manager=manager,
        terminal_state=TerminalState.APPROVAL_DENIED,
        no_pending_approval=(gate_outcome.is_pending is False),
        no_pending_recovery=(manager.routing_run.branches == ()),
        ended_at=ended_at,
    )
    return DenySettlement(
        run_id=manager.routing_run.run_id,
        terminal_state=TerminalState.APPROVAL_DENIED,
        gate_settled=True,
        ended_at=ended_at,
        branch_count=0,
    )
