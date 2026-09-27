"""``C2c 恢复写路径``（`恢复意图 → 至多一次运行态写入 + 写入事实报告`）。

架构依据：`docs/architecture.md` **6.4 / L47**（`[10]` 运行态唯一写者）·
**A13.2**（两阶段物化 · `[10]` 唯一写者）· **A13.3**（`branches` = attempt history）·
**A13.6**（`commit_started` 是 `RUNNING` 的唯一写入口）· **A18.7**（决策者 ≠ 状态写者）·
**A13.4**（"absence of observation ≠ fact"）· **INV-10**（高风险必须过 Gate）·
**INV-16**（升级重流必须重过 Gate）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§155**（Scope Review）/ **§156**（范围稿冻结）
/ **§157**（实现授权）/ **§158**（Lock Amendment）。

**本模块要守住的五条主张：**

  1. **只解决"恢复意图如何合法改变运行态"**：不选下一节点 · 不执行节点 · 不接 Scheduler ·
     不成为新的 Orchestrator（`LRE-7`）；
  2. **用户恢复 ≠ 失败恢复**：**不推进** attempt（`LRE-3` / `LRE-10`）；唯一 `+1` 路径是
     `[14] Recovery` → `commit_started`；
  3. **三出口共用同一载体**：`APPROVE` ⇒ 恰好一次写入；`DENY` / `MODIFY` ⇒ **零写入**
     （报告里 `new_state` **等于** `previous_state`，是**可断言**的强证据，而非"没写"的弱证据）；
  4. **显式失败优先于静默默认**：**零 `try`**（`LRE-5`）· 未覆盖出口与非法前置一律抛错，
     **不得**降级为空返回；
  5. **零时钟**（`LRE-6`）：`resumed_at` **由调用方显式传入**，本模块**不读时钟**。

⚠️ **运行态事实只从 `[10]` 取**：本模块**不**自行推导状态、**不**建反查注册表、**不**从历史问题推导地址；
   无记录 ⇒ 显式失败（**不**把"没观测到"读成"可以继续"）。
⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `scheduler` / `dispatch` / 下一节点 / `close_run`
   等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。
⚠️ **不构造** `GateOutcome` / `GateDecision` / `RoutingRun` / `RunTerminal` / `RecoveryDecision`；
   `GateDecisionKind` 只作**枚举比较**，**不** import `approval_gate_wiring`（`LRE-8`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .agent_manager import AgentManager
from .approval_gate_wiring import GateDecisionKind
from .contracts import AgentRun, BranchId, BranchState, RunId
from .resume_intent import ResumeIntent

__all__ = ["ResumeWiringError", "ResumeWriteReport", "apply_resume_intent"]


class ResumeWiringError(RuntimeError):
    """``C2c`` 的显式失败：非法恢复意图 / 非法前置运行态 / 未覆盖的授权出口。"""


@dataclass(frozen=True)
class ResumeWriteReport:
    """一次恢复写路径的**结果证明**（`§157.5` 六字段锁死；多一个都不许）。

    ⚠️ ``run_id`` 是**结果归属证明字段**（**不是**恢复语义字段）：同一 Session 内多个 run 的
    ``branch_id`` 可以相同 ⇒ 没有它，报告在多 run 会话下**不可归属**。
    ⚠️ 本载体**不含**任何 ``node`` / ``dispatch`` / ``scheduler`` / 下一执行目标 —— 那条边界
    属**节点分派**（未开放）。
    """

    branch_id: BranchId
    previous_state: BranchState
    new_state: BranchState
    attempt: int
    resumed_at: str
    run_id: RunId


def _require_intent(
    intent: object,
    *,
    manager: AgentManager,
    resumed_at: object,
) -> ResumeIntent:
    """把**恢复意图**收窄为**可安全消费**的形态；任一条不成立 ⇒ :class:`ResumeWiringError`。

    守卫逐条（与 `§156.5` 的 g1–g7 一一对应）：

      g1 ``intent`` 必须是 :class:`ResumeIntent`；
      g2 授权必须是**已解析**的（``is_pending is False``）—— 挂起卡片**没有**可消费的授权事实；
      g3 ``resumed_at`` 必须是**非空** ``str``（**由调用方传入**；本模块**零时钟**）；
      g4 授权出口必须落在**三出口之一**（`APPROVE` / `DENY` / `MODIFY`）
         —— 未覆盖 ⇒ **显式失败**，**不得** fallback；
      g5 `MODIFY` 必须携带**非空** ``decision.modification``（"改什么"是待审动作，不得为空）；
      g6 运行态事实必须**存在**且为 ``WAITING_INPUT``（取自 `[10]` 的只读事实接口）
         —— 无记录 ⇒ 失败；``RUNNING`` ⇒ 失败（**重复消费**一个已用过的授权事实）；
      g7 ``attempt`` 必须与该 branch 最新记录**一致** —— 不等即失败
         （**不得**改写入参成最新值，**不得** ``+1``）。

    ⚠️ ``branch_record`` 的**内联调用**是**有意**的：
       `approval_question.py` 曾因"守卫从未被调用"留下**死守卫**（§142.7 ②）⇒
       本模块的 read seam 一律**嵌在守卫表达式内**，**不**先赋给局部名。
    """
    if not isinstance(intent, ResumeIntent):
        raise ResumeWiringError(f"intent 必须是 ResumeIntent，收到 {intent!r}")
    if intent.gate_outcome.is_pending is not False:
        raise ResumeWiringError(
            "授权事实仍在挂起态（is_pending is not False）⇒ 人类还没回答，"
            "不存在可消费的恢复授权"
        )
    if type(resumed_at) is not str or not resumed_at.strip():
        raise ResumeWiringError(f"resumed_at 必须是非空 str（调用方显式传入），收到 {resumed_at!r}")
    if intent.gate_outcome.decision.kind not in (
        GateDecisionKind.APPROVE,
        GateDecisionKind.DENY,
        GateDecisionKind.MODIFY,
    ):
        raise ResumeWiringError(
            f"未覆盖的授权出口 {intent.gate_outcome.decision.kind!r} ⇒ 显式失败（不得 fallback）"
        )
    if intent.gate_outcome.decision.kind is GateDecisionKind.MODIFY and (
        type(intent.gate_outcome.decision.modification) is not str
        or not intent.gate_outcome.decision.modification.strip()
    ):
        raise ResumeWiringError("MODIFY 必须携带非空的 modification（待审动作不得为空）")
    if not isinstance(manager, AgentManager):
        raise ResumeWiringError(f"manager 必须是 AgentManager，收到 {manager!r}")
    if manager.branch_record(intent.branch_id) is None:
        raise ResumeWiringError(
            f"{intent.branch_id} 在 [10] 里没有任何 attempt 记录 ⇒ 没有可恢复的运行态事实"
        )
    if manager.branch_record(intent.branch_id).state is not BranchState.WAITING_INPUT:
        raise ResumeWiringError(
            f"{intent.branch_id} 的 attempt {intent.attempt} "
            f"处于 {manager.branch_record(intent.branch_id).state.value}，"
            "只有 WAITING_INPUT 才能被恢复（已 RUNNING ⇒ 重复消费一个已用过的授权事实）"
        )
    if manager.branch_record(intent.branch_id).attempt != intent.attempt:
        raise ResumeWiringError(
            f"{intent.branch_id} 的最新 attempt 是 "
            f"{manager.branch_record(intent.branch_id).attempt}，不是 {intent.attempt}"
            "（用户恢复**不推进** attempt：必须与最新记录一致）"
        )
    return intent


def apply_resume_intent(
    *,
    intent: ResumeIntent,
    manager: AgentManager,
    resumed_at: str,
) -> ResumeWriteReport:
    """把**已授权**的恢复意图落成**至多一次**运行态写入，并返回**写入事实报告**。

    ⚠️ **唯一写入点**：`APPROVE` 走 ``[10].resume_from_waiting``（**恰好一次**）；
       `DENY` / `MODIFY` **零写入**（报告里 ``new_state`` 等于 ``previous_state``）。
    ⚠️ **函数体内零局部绑定**：所有守卫与读事实一律**内联嵌套**（`§156.5` / `§157.13 ②`）。
    ⚠️ **只有一个 return**：非法输入全在 ``_require_intent`` 里显式失败 ⇒ **无**合法的
       ``None`` 返回路径。
    ⚠️ **不推进 attempt**：``attempt=`` 写的是 ``intent.attempt``（**原值**），
       **不**是 ``intent.attempt + 1``；`[10]` 侧亦**只**写回同一个 attempt。
    """
    return ResumeWriteReport(
        branch_id=_require_intent(intent, manager=manager, resumed_at=resumed_at).branch_id,
        previous_state=manager.branch_record(intent.branch_id).state,
        new_state=(
            manager.resume_from_waiting(branch_id=intent.branch_id, attempt=intent.attempt).state
            if intent.gate_outcome.decision.kind is GateDecisionKind.APPROVE
            else manager.branch_record(intent.branch_id).state
        ),
        attempt=intent.attempt,
        resumed_at=resumed_at,
        run_id=intent.run_id,
    )
