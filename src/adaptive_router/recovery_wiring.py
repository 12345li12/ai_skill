"""``[13] → [14]`` **接线** —— 把失败事实装配成 ``RecoveryContext`` 并交回**恒定载体**。

架构依据：`docs/architecture.md` 2 节 ``[13] Validation`` / ``[14] Recovery Router``
（**Amendment A16 / A17**，FROZEN）；范围、缺口裁定与锁清单见
`tests/behavior/README_step5b.md` **§86（preflight）/ §87（scope review）**。

```text
上游                                             本模块                         [14]
────                                             ──────                         ────
BranchValidationOutcome ─┐
（FAIL 配对见证）         ├─→ RecoveryContext ─→ decide_recovery ─→ RecoveryDecision | None
adapter failure 事实  ────┘        ↑                                          │
（duck-typed 三字段）        attempt / total_attempt_limit                     ↓
                           （**显式**：不反查运行态）              RecoveryOutcome(context, decision)
                                                                     ↑ **恒定返回**（含 decision=None）
```

**三条设计性质（§87.6 的锁定）：**

1. **载体唯一构造点**：`RecoveryOutcome` **只**在本模块构造，且只有**一处**（两条入口共用的 `_route`）；
   决策调用同样只有**一处** ⇒ "第二条旁路"在结构上不存在；
2. **恒定返回**：两条入口**任何**正常路径都返回载体（含 `decision is None`）——
   `None` 表示"已达 `total_attempt_limit`，不得回流"（调用方走 `TERMINAL_FAILURE`），
   **它不是"没有身份"**：`branch_id` / `attempt` 仍在载体上可读；
3. **不重建**：`decision` 就是 ``decide_recovery`` 的**同一个对象**（`is`），本模块
   **不得**构造 `RecoveryDecision`。

**明确不做（§86 G4 / G6 / G7 与复审裁决）：**

- ❌ **PASS 不进 Recovery**：由 ``RecoveryContext`` **构造期**拒收（不是"静默返回 None"）；
- ❌ 不写任何运行态（`BranchState` / `RoutingRun` 的单写者是 ``[10]``）· ❌ 不决定去路
  （`route_to` → §4.6 R1/R3 的重决策链属后续轮）· ❌ 不碰收敛事实
  （`control_flow_converged` / `no_pending_*` 归 ``[10]``；INV-20）；
- ❌ 不 import `adapters`（L11-1 / L11-2 / L12-2 —— Core 不得 import；`failure` 参数是
  **duck-typed**，只读 `failure_kind.value` / `message` / `timestamp` 三个属性）；
- ❌ 不复制 `[14]` 的映射规则（表只在 `recovery.py`）· ❌ 不做兜底（缺属性 / 双形态一律**显式失败**）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import BranchId, RecoveryDecision
from .recovery import decide_recovery
from .recovery_context import RecoveryContext
from .validation_wiring import BranchValidationOutcome

__all__ = ["RecoveryOutcome", "route_adapter_failure", "route_failed_branch"]


@dataclass(frozen=True)
class RecoveryOutcome:
    """一次失败恢复的**恒定载体**：事实上下文 + `[14]` 的决策（可为 `None`）。

    ⚠️ 为什么必须有它（§86 G2）：``RecoveryDecision`` **不带 `branch_id`**（契约七字段），
    而 `decision is None`（到上限）这条路径**同样需要身份** —— 调用方要据此把
    **这一个分支的那一次 attempt** 送进 `TERMINAL_FAILURE`。
    只返回决策会让"决策 ↔ 分支"再次丢失（与 §77 的 `BranchValidationOutcome` 同一教训）。

    ⚠️ 两个字段都是**原样引用**（不复制内容、不重建对象）⇒
    ``outcome.decision is decide_recovery(...) 的返回对象`` 在行为上可断言。
    """

    context: RecoveryContext
    decision: RecoveryDecision | None

    def __post_init__(self) -> None:
        # 先身份：不靠 duck typing
        if not isinstance(self.context, RecoveryContext):
            raise TypeError(f"context 必须是 RecoveryContext，收到 {self.context!r}")
        if self.decision is not None and not isinstance(
            self.decision, RecoveryDecision
        ):
            raise TypeError(
                f"decision 必须是 RecoveryDecision | None，收到 {self.decision!r}"
            )

    @property
    def branch_id(self) -> BranchId:
        """**派生只读**：哪个分支（`decision` 里没有这个信息）。"""
        return self.context.branch_id

    @property
    def attempt(self) -> int:
        """**派生只读**：第几次 attempt（分支级，A17.6）。"""
        return self.context.attempt

    @property
    def should_recover(self) -> bool:
        """是否应当回流：`False` ⇒ 已达 `total_attempt_limit`，走 `TERMINAL_FAILURE`。"""
        return self.decision is not None


def _field(failure: object, name: str) -> object:
    """duck-typed 取属性；**缺失即显式失败**（§87 ④：不做双形态兼容）。"""
    if not hasattr(failure, name):
        raise TypeError(
            f"failure 必须是带 {name} 的失败事实（duck-typed），收到 {failure!r}"
        )
    return getattr(failure, name)


def _kind_of(failure: object) -> str:
    """取 ``failure.failure_kind.value``（A14.4 四值的字符串投影，**不 import `adapters`**）。

    ⚠️ 只认 `.value` 这一个形态：`str(...)` / 裸枚举 / 裸字符串都**不接受** ——
    兼容层会把"输入契约错误"藏起来（§87 ④）。
    """
    kind = _field(failure, "failure_kind")
    if not hasattr(kind, "value"):
        raise TypeError(
            "failure.failure_kind 必须带 .value（A14.4 四值的字符串投影；"
            "不做 str/enum 双形态兼容）"
        )
    value = kind.value
    if type(value) is not str or not value.strip():
        raise TypeError(f"failure.failure_kind.value 必须是非空 str，收到 {value!r}")
    return value


def _text_field(failure: object, name: str) -> str:
    """取 ``failure.message`` / ``failure.timestamp``（非空 str）。"""
    value = _field(failure, name)
    if type(value) is not str or not value.strip():
        raise TypeError(f"failure.{name} 必须是非空 str，收到 {value!r}")
    return value


def _route(context: RecoveryContext) -> RecoveryOutcome:
    """**唯一**决策调用点 + **唯一**载体构造点（两条入口共用）。

    这样"第二条旁路"（自己算分类 / 自己造决策 / 返回裸决策）在**结构上**不存在。
    """
    return RecoveryOutcome(context=context, decision=decide_recovery(context=context))


def route_failed_branch(
    *,
    outcome: BranchValidationOutcome,
    total_attempt_limit: int,
) -> RecoveryOutcome:
    """**FAIL 侧入口**：从 ``[12] → [13]`` 的配对见证装配上下文并产出载体。

    ⚠️ ``attempt`` 取自 ``outcome.branch_return.attempt`` —— **同一次配对事件**，
    不额外传参、不反查运行态（A11.4b 的精神：身份必须来自同一次事件）。

    ⚠️ **PASS 会在构造期被拒收**（`RecoveryContext` 的 `passed is not False` 守卫）⇒
    "PASS 不进 Recovery"是**结构保证**，不是本函数的判断。
    """
    if not isinstance(outcome, BranchValidationOutcome):
        raise TypeError(
            f"outcome 必须是 BranchValidationOutcome，收到 {outcome!r}"
            "（FAIL 侧只接受 [12] → [13] 的配对见证）"
        )
    context = RecoveryContext(
        branch_id=outcome.branch_return.branch_id,
        attempt=outcome.branch_return.attempt,
        total_attempt_limit=total_attempt_limit,
        validation=outcome.validation,
        failure_kind=None,
        message=None,
        timestamp=None,
    )
    return _route(context)


def route_adapter_failure(
    *,
    branch_id: BranchId,
    attempt: int,
    total_attempt_limit: int,
    failure: object,
) -> RecoveryOutcome:
    """**非能力失败侧入口**：从 P1 报告的 `(branch_id, attempt, AdapterFailure)` 装配上下文并产出载体。

    ⚠️ `AdapterFailure` **自身没有 `branch_id`**（A14.2）⇒ `branch_id` 必须由调用方
    按 P1 报告的**成对关系**给出（不得靠顺序推断）。
    ⚠️ `AdapterFailure` **也不带 `attempt`** ⇒ `attempt` 必须**显式传入**。
    ⚠️ `failure` 是 **duck-typed** 的：只读 `failure_kind.value` / `message` / `timestamp`，
    **不 import `adapters`**（Core 边界）。
    """
    context = RecoveryContext(
        branch_id=branch_id,
        attempt=attempt,
        total_attempt_limit=total_attempt_limit,
        validation=None,
        failure_kind=_kind_of(failure),
        message=_text_field(failure, "message"),
        timestamp=_text_field(failure, "timestamp"),
    )
    return _route(context)
