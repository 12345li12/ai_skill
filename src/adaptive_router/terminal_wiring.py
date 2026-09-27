"""``[13]/[14] → [10]`` **接线** —— 分支级收尾 + Run 级终态收敛的**唯一载体**。

架构依据：`docs/architecture.md` 2 节 ``[10] Agent Manager`` 的 **Amendment A18**
（Terminal Convergence & Pending-Control Facts，FROZEN：A18.2 唯一写入口 + 三条禁止 ·
A18.3 两个收敛事实的来源 · A18.4 四条禁止推断 · A18.5 V1 完备性声明 · A18.6 FAIL 侧时序 ·
A18.7 决策者 ≠ 状态写者 · A18.9 `LT-1…LT-6` · A18.10 `INV-21` / `TEST-STATE-002`）·
4.5 双条件 · 6.5 六值 · A11.4b（配对事件的身份）· A13.7（分支级四个终态）。
范围、锁清单（`LTW-1…LTW-9`）与变异清单（`MB1–MB8`）见 `tests/behavior/README_step5b.md`
**§99**（Phase B scope review）与 **§100**（Phase B 实现）。

```text
[13] BranchValidationOutcome ─┬─► settle_pass      ─┐
（同一次配对事件的 PASS / FAIL 见证）└─► settle_failure ─┴─► _settle ─► [10].record_outcome(COMPLETED | FAILED)
                                                              │
编排点（**显式给出**四个事实）──────────────────────────────────┴─► converge_run ─► [10].close_run(…)
```

**三条设计性质（§99 的锁定）：**

1. **唯一收尾点**：``record_outcome(`` 在本模块只有**一处**（``_settle``），两个入口共用
   ⇒ 第二条旁路在结构上不存在（与 ``recovery_wiring._route`` 同形）；
2. **唯一终态调用点**：``close_run(`` 在本模块只有**一处**（``converge_run`` 内），且**全 repo 唯一**
   （LTW-1）—— ``[10]`` 之外没有第二个 Run 终态入口；
3. **无源可推**：本模块的签名里**不存在** Approval / Recovery 的任何对象 —— 只有裸 ``bool``、
   显式 ``terminal_state`` 与显式 ``ended_at`` ⇒ A18.4 的四条禁止在本层**没有输入可推导**
   （这是结构保证，不是约定）。

**明确不做（越界即缺陷）：**

- ❌ 不推导 ``terminal_state``（A18.2 第三条禁止）· ❌ 不伪造 / 不默认两个 pending facts（A18.4 / A18.5）；
- ❌ 不读时钟（LT-3：``ended_at`` 只透传）· ❌ 不推进 attempt（A18.7 / A13.3：不是第二个 materializer）；
- ❌ 不产恢复决策（A18.6：**先关 attempt，再产决策** —— FAIL 侧入口只收**决策前**的失败见证）；
- ❌ 不写任何运行态（单写者是 ``[10]``，A11.10）· ❌ 不复制 ``[10]`` 的六个守卫（规则只有一份）。
"""

from __future__ import annotations

from .agent_manager import AgentManager
from .contracts import BranchId, BranchState, TerminalState
from .validation_wiring import BranchValidationOutcome

__all__ = [
    "TerminalWiringError",
    "converge_run",
    "settle_failure",
    "settle_pass",
]


class TerminalWiringError(RuntimeError):
    """接线级显式失败：**不是** ``[10]`` 守卫的替代品。

    ⚠️ ``[10]`` 的六个守卫（``terminal_state`` 类型 / 两个 fact 类型 / ``ended_at`` /
    重复收口 / 分支全终止 / 双 fact 收敛）**原样生效**；本异常只覆盖**接线自身**的前置条件：
    对象不是本模块契约的载体、或走错了入口。
    ⚠️ 它**不得**被降级成"写一个默认终态" —— 那会把接线缺陷写成运行事实
    （同 ``ValidationWiringError`` 不得降级成 ``ValidationRecord(passed=False)``）。
    """


def _require_manager(manager: object, *, entry: str) -> AgentManager:
    """``manager`` 的身份检查：**显式失败**优先于 ``AttributeError``。"""
    if not isinstance(manager, AgentManager):
        raise TerminalWiringError(
            f"{entry} 的 manager 必须是 AgentManager，收到 {manager!r}"
        )
    return manager


def _require_outcome(
    outcome: object, *, passed: bool, entry: str
) -> BranchValidationOutcome:
    """守卫 + 收窄：只接受**同一次配对事件**的见证，且入口必须与判定一致（§99.5）。

    ``passed`` 是**这一次调用期望**的判定值：``settle_pass`` 传 ``True``、``settle_failure``
    传 ``False``。走错入口 ⇒ **显式失败**，而不是写下一个错误的终态。
    """
    if not isinstance(outcome, BranchValidationOutcome):
        raise TerminalWiringError(
            f"{entry} 只接受 BranchValidationOutcome（[12] → [13] 的配对见证），"
            f"收到 {outcome!r}"
        )
    if outcome.validation.passed is not passed:
        raise TerminalWiringError(
            f"{entry} 收到 passed={outcome.validation.passed!r} 的见证："
            "入口与判定不一致 = 接线缺陷（显式失败，不得写下错误的终态）"
        )
    return outcome


def _settle(
    *,
    branch_id: BranchId,
    attempt: int,
    state: BranchState,
    manager: AgentManager,
    ended_at: str,
) -> BranchId:
    """**唯一** ``record_outcome`` 调用点（LTW-4）：两个入口共用 ⇒ 第二条旁路不存在。

    ⚠️ 只调用 ``[10]`` 的既有方法，**不新增**任何状态字段、**不判断**该不该收尾
    （那是 ``[13]`` / ``[14]`` / 编排点的活）。
    """
    checked = _require_manager(manager, entry="_settle")
    checked.record_outcome(
        branch_id=branch_id,
        attempt=attempt,
        state=state,
        ended_at=ended_at,
    )
    return branch_id


def settle_pass(
    *,
    outcome: BranchValidationOutcome,
    manager: AgentManager,
    ended_at: str,
) -> BranchId:
    """**PASS 侧入口**：把该分支**这一次** attempt 收尾为 ``COMPLETED``（A18.5：分支级 V1 完备）。

    ⚠️ ``branch_id`` / ``attempt`` 取自 ``outcome.branch_return`` —— **同一次配对事件**
    （A11.4b），**不反查运行态**、不按顺序推断。
    ⚠️ PASS 是**分支执行事实**，**不是** approval fact：本入口**不产生**任何 pending fact
    （A18.4 禁止 1 的「PASS ⇒ ``no_pending_approval = True``」永远不会在这里发生）。
    """
    checked = _require_outcome(outcome, passed=True, entry="settle_pass")
    return _settle(
        branch_id=checked.branch_return.branch_id,
        attempt=checked.branch_return.attempt,
        state=BranchState.COMPLETED,
        manager=manager,
        ended_at=ended_at,
    )


def settle_failure(
    *,
    outcome: BranchValidationOutcome,
    manager: AgentManager,
    ended_at: str,
) -> BranchId:
    """**FAIL 侧入口**：**先**把该 attempt 收尾为 ``FAILED``（A18.6 的第一步）。

    ⚠️ **顺序冻结（A18.6）**：``record_outcome(FAILED)`` **先于** Recovery Decision ⇒
    本入口只收**决策前**的失败见证（``BranchValidationOutcome``），**不 import** ``[14]``、
    **不产出** ``RecoveryDecision``；决策**随后**由 ``recovery_wiring.route_failed_branch``
    用**同一个**见证产出（§99.7 的裁定：选项 A）。
    ⚠️ 本入口**不创建新 attempt**（A18.7 / A13.3：attempt 推进不是接线层的职责，
    回流路径属已登记的遗留 P2）。
    """
    checked = _require_outcome(outcome, passed=False, entry="settle_failure")
    return _settle(
        branch_id=checked.branch_return.branch_id,
        attempt=checked.branch_return.attempt,
        state=BranchState.FAILED,
        manager=manager,
        ended_at=ended_at,
    )


def converge_run(
    *,
    manager: AgentManager,
    terminal_state: TerminalState,
    no_pending_approval: bool,
    no_pending_recovery: bool,
    ended_at: str,
) -> None:
    """**Run 级收敛**：把四个**显式事实**原样交给 ``[10]`` 的唯一终态写入口（A18.2 / LTW-1）。

    ⚠️ 四个入参**全部原样透传**：本函数**不推导** ``terminal_state``、**不伪造 / 不默认**
    两个 pending facts、**不读时钟**。四个事实的 owner 分别是：编排点（``terminal_state``）·
    Approval 链（``no_pending_approval``）· 持有未决恢复的编排点（``no_pending_recovery``）·
    调用方（``ended_at``）（A18.3 · §99.6）。
    ⚠️ ``[10]`` 的六个守卫在此**原样生效**（缺 fact / 分支未终止 / 重复收口 ⇒ ``AgentManagerError``）：
    本函数**不复制**它们，也**不**把它们提前吸收成静默返回。
    """
    checked = _require_manager(manager, entry="converge_run")
    checked.close_run(
        terminal_state=terminal_state,
        no_pending_approval=no_pending_approval,
        no_pending_recovery=no_pending_recovery,
        ended_at=ended_at,
    )
