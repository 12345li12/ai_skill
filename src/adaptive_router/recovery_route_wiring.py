"""``[14] → 重决策请求`` 的 **C2a 载体**（接线层）。

架构依据：`docs/architecture.md`

- 2 节 ``[14] Recovery Router`` 节点行：**"它只能产生决策，由对应节点重新做正式决策"** ——
  所以"决策"与"正式重决策"之间必须有一个**显式请求**；
- **A17.2**：`FailureClass → RecoveryTarget` 的满射表（五个目标：`CAPABILITY` 重查 ·
  `PLANNER` 重拆 · `MODEL_ROUTER` 换档位 · `CONTEXT` 重打包 · `SCHEDULER` 重调度）；
- **A17.3**：V1 **可达**类别 = `COMPLETION_CRITERIA` / `MODEL` / `PERMISSION` / `EXTERNAL_SERVICE`
  ⇒ 经 A17.2 落到 **三个**可达目标：`PLANNER` / `MODEL_ROUTER` / `CAPABILITY`
  （`CONTEXT` 需 `INPUT`、`SCHEDULER` 需 `DEPENDENCY`，两者在 V1 **没有具名 producer**）；
- **A17.6 / A11.4b**：身份是**分支级** `(branch_id, attempt)`，来自**同一次配对事件**；
- **A17.4 / L49**：`carry_forward` 只带**有具名来源**的项，**不得编造**；
- **`INV-09`**：`[14]` 是失败后的**唯一合法回流控制器** ⇒ 本模块**不产生回边**。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§108**（preflight）· **§109**（scope review）·
**§110**（冻结记录）· **§111**（实现授权）· **§112**（实现记录）。

**一条边（C2a 只做这一件事）**：

```text
[14] RecoveryOutcome（已冻结的恒定载体：context + decision | None）
        │  ① 身份透传：outcome.branch_id / outcome.attempt
        │  ② 决策内容透传：outcome.decision.{route_to, minimal_correction, carry_forward}
        ▼
   RedecisionRequest  ──►  交给**编排点**去调用对应节点的正式重决策
```

**七条口径（§110 冻结，逐条对应 ``LRT`` 锁）**：

1. **唯一载体**（`LRT-1`）：`RedecisionRequest(` 的构造点只有一处；
2. **只透传，不重算**（`LRT-2`）：五个实参**逐字**是 `outcome.*` / `outcome.decision.*`；
3. **不产生决策**（`LRT-3`）：不构造 `RecoveryDecision` / `RecoveryOutcome`，**不 import `recovery.py`**
   ⇒ 本模块在结构上**无法**调用 `decide_recovery`；
4. **V1 可达守卫 + 无请求语义**（`LRT-4`）：三目标之外 ⇒ **显式失败**；
   `decision is None`（已达 `total_attempt_limit`）⇒ **返回 `None`**（**合法终态**，不是接线缺陷）；
5. **不执行节点**（`LRT-5`）：不 import / 不调用 `[03]` / `[05]` / `[06]` / `[08]` / `[09]` 的任何入口
   —— 分派属后续阶段，本边**只到请求为止**；
6. **不写运行态 · 不接授权链**（`LRT-6` / `LRT-7`）：不碰 `[10]` 的任何方法 · 不碰 Approval / 成本侧
   （重过门与重估属 C2b，回流 attempt 的写入属 C2c）；
7. **无时钟 / 无状态**（`LRT-9`）：`REDECISION_TARGETS` 是**不可变 tuple 常量**（白名单数据），
   不是运行期状态。

**明确不做（越界即缺陷）**：❌ 不重新判断策略（**Decision Carrier，不是 Decision Maker**）·
❌ 不携带 `failure_class`（它已被 A17.2 消费成 `route_to`；携带会诱导下游**重做分类**）·
❌ 不补造 `carry_forward` 的 producer（`EXCLUDED_DIRECTIONS` 在 V1 无来源，已登记为上报项）·
❌ 不执行节点 · ❌ 不重估成本 · ❌ 不重过授权门 · ❌ 不写 attempt。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import BranchId, RecoveryTarget
from .recovery_wiring import RecoveryOutcome

__all__ = [
    "REDECISION_TARGETS",
    "RecoveryRouteWiringError",
    "RedecisionRequest",
    "build_redecision_request",
]


class RecoveryRouteWiringError(RuntimeError):
    """C2a 接线级显式失败：**不是** ``[14]`` 决策的替代品。

    ⚠️ 它只在**接线自身**的前置条件不成立时抛出：对象不是本边的契约载体、
    或请求的目标**超出 A17.3 的 V1 可达集**（那是接线缺陷或 ``[14]`` 越权）。
    ⚠️ 它**不得**被降级成"给一个默认目标" —— 那会把越界写成合法的重决策请求。
    """


#: **A17.3 的 V1 可达目标**（经 A17.2 的满射表落到目标侧）—— 白名单**数据**，不是路由表。
#:
#: ⚠️ 它**只回答"哪些目标在 V1 可能出现"**，**不回答"哪类失败去哪"**（后者是 `[14]` 的 A17.2 表）。
#: ⚠️ `CONTEXT` / `SCHEDULER` **不得**加入：V1 没有产出它们的具名 producer
#: （`INPUT` / `DEPENDENCY` 属 A17.3 的不可达类别）。
REDECISION_TARGETS = (
    RecoveryTarget.PLANNER,
    RecoveryTarget.MODEL_ROUTER,
    RecoveryTarget.CAPABILITY,
)


@dataclass(frozen=True)
class RedecisionRequest:
    """一次**正式重决策请求**的载体（C2a 的唯一产出）。

    ⚠️ **pure carrier**：本类**不做值校验**（§110.2 的裁决）—— 验证职责在
    Recovery 决策层 / contract 层 / 后续目标节点；本边只负责**结构运输**。
    ⚠️ 字段**全部来自 `[14]` 已经产出的决策**（+ 配对事件的身份）：
    ``route_to`` 是 A17.2 的结果、``minimal_correction`` 是 L49 的固定措辞、
    ``carry_forward`` 是 A17.4 的**有具名来源**项 —— 本边**一个都不重算、不补造**。
    ⚠️ **不含 `failure_class`**：它已被 `[14]` 消费成 ``route_to``；再携带它就等于邀请
    下游**重做分类**（第二分类入口 / 第二张路由表）。
    """

    branch_id: BranchId
    attempt: int
    target: RecoveryTarget
    minimal_correction: str
    carry_forward: tuple[str, ...]


def _require_outcome(outcome: object) -> RecoveryOutcome:
    """类型守卫：本边只接受 ``[13] → [14]`` 的**恒定载体**（显式失败优先于 AttributeError）。"""
    if not isinstance(outcome, RecoveryOutcome):
        raise RecoveryRouteWiringError(
            "build_redecision_request 只接受 RecoveryOutcome"
            f"（[13] → [14] 的恒定载体），收到 {outcome!r}"
        )
    return outcome


def build_redecision_request(
    *, outcome: RecoveryOutcome
) -> RedecisionRequest | None:
    """把 ``[14]`` 的**已有决策**变成显式的重决策请求；**无进一步动作时返回 `None`**。

    ⚠️ ``decision is None`` ⇒ **返回 `None`**：含义是"已达 ``total_attempt_limit``，
    不得回流"（`recovery_wiring` 的冻结口径）—— 这是**合法终态**，不是接线缺陷，
    因此既不抛异常、也**不造一个假请求**。
    ⚠️ 目标必须在 ``REDECISION_TARGETS``（A17.3 的 V1 可达集）内；否则 **显式失败**
    （V1 里出现 `CONTEXT` / `SCHEDULER` 只可能是接线缺陷或 `[14]` 越权）。
    ⚠️ 本函数**不调用任何节点**：它**只到请求为止**（分派属后续阶段）。
    """
    _require_outcome(outcome)
    if outcome.decision is None:
        return None
    if outcome.decision.route_to not in REDECISION_TARGETS:
        raise RecoveryRouteWiringError(
            f"route_to={outcome.decision.route_to!r} 不在 A17.3 的 V1 可达集"
            f"（{tuple(item.value for item in REDECISION_TARGETS)}）里："
            "V1 没有产出该目标的具名 producer ⇒ 这是接线缺陷，不得静默透传"
        )
    return RedecisionRequest(
        branch_id=outcome.branch_id,
        attempt=outcome.attempt,
        target=outcome.decision.route_to,
        minimal_correction=outcome.decision.minimal_correction,
        carry_forward=outcome.decision.carry_forward,
    )
