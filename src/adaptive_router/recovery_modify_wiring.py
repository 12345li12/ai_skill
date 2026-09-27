"""``Approval Gate 的 MODIFY → [02] Policy 重入请求`` 的 **C2b-2b-1 语义载体**（接线层）。

架构依据：`docs/architecture.md`

- **Approval Gate 节点行**：三种输入的控制流出口（V1 冻结）——
  ``APPROVE`` → 按获批范围继续 · ``DENY`` → 终态 ``APPROVAL_DENIED`` ·
  ``MODIFY`` → 转换成新的「用户当前指令」→ **回 `[02]` Policy** → 重建 AuthorizationContext →
  从最早受影响的 Decision 节点重新计算 → Approval Judgment → 必要时再次 Gate；
  逐字禁令：**"MODIFY 永远不能直接执行旧 RoutingPlan"**；
- **§4.6 六条回流路径授权闭合表**：**R4** = 停止/重建/热切换（用户干预）——
  "当前用户明确干预**本身就是新的授权事实**" ⇒ "是否真实进入 Gate = **否**（本身即授权）"；
  "**Approval Gate 的 MODIFY 属于 R4 的一种具体入口**……**不新增 R7**"；
- **Authorization Context Builder 节点行**：输入含"**用户当前授权**"；
  "权限聚合必须在 Approval Judgment **之前**完成"；"不新增节点编号"；
- **`INV-02`**（项目只能收窄，不能放宽）· **`INV-03`**（RoutingPlan 不可变）·
  **`INV-16`**（回流后扩大范围必须重新过 Gate —— 属后续阶段，本边不实现）；
- **A13.5**（人类/外部输入进 Core 的形态 = **显式事实注入**，同 `RuntimeControlFacts`）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§126**（preflight）· **§127**（scope review）·
**§128**（冻结记录）· **§129**（实现授权）· **§130**（实现记录）。

**一条边（C2b-2b-1 只做这一件事）**：

```text
GateOutcome（C2b-2a FROZEN；本边**只读** decision 一个字段）
        │  ① 守卫：decision 非 None 且 kind is MODIFY（挂起 / APPROVE / DENY ⇒ 显式失败）
        │  ② **边界外重建后的** PolicyFacts（完整最终态；本边**只透传**）
        ▼
   ModifyRerouteRequest  ──►  交给**编排点**：带着新授权事实从 `[02]` 重新进入决策主链
```

**它是**：MODIFY 之后「从 `[02]` 重入」这件事的**机械入口**（一个显式请求事实）。
**它不是**：Policy 执行入口（不调用 `[02]`）· `PolicyFacts` 的 producer（不重建事实）·
文本解释器（不读 `modification`）· Gate（不实现第二次 Gate）· 执行器（不写运行态、不跑 attempt）。

**七条口径（§128 冻结，逐条对应 `LRM` 锁）**：

1. **唯一载体**（`LRM-1`）：`ModifyRerouteRequest(` 的构造点只有一处；
2. **只搬运 / 只组装**（`LRM-2`）：两个实参逐字来自形参 / 模块级固定措辞；
   **函数体内零局部绑定**（任何 `policy_facts = ...` 式内部覆盖在结构上必然变红）；
3. **MODIFY 专用入口 + 不重判 kind**（`LRM-3`）：`decision is None`（挂起）· 非 `GateDecision` ·
   `kind is not MODIFY`（含 `APPROVE` / `DENY`）⇒ 三者**全部显式失败**；`kind` 不是赋值目标；
4. **身份守卫**（`LRM-4`）：`gate_outcome` 必须是 `GateOutcome`；`policy_facts` 必须是 `PolicyFacts`；
5. **不重建 `PolicyFacts`**（`LRM-5`）：构造点 = 0 · `dataclasses.replace` = 0 · 其字段名不出现
   ⇒ 由「**返回对象里的 facts 与入参是同一个对象**」这条身份级证据反向证明；
6. **零取值**（`LRM-6`）：**不读** `decision.modification` —— 连 `.strip()` / 空检查都不做
   （MODIFY 的合法性已由 C2b-2a 的守卫保证；在此重复检查会**重新引入解释面**）；
7. **不重算 / 不写运行态**（`LRM-7` / `LRM-8` / `LRM-9`）：不调用 `Policy` / `resolve` ·
   不 import `router` / `planner` / `model_router` / `cost` / `risk` / `approval_judgment` /
   `routing_plan` / `agent_manager` / `scheduler` / `execution` · 无时钟 · 无状态 · 无 IO。

**明确不做（越界即缺陷）**：❌ 不解析 `modification` ❌ 不重建 / 不改写 `PolicyFacts`
❌ 不调用 `[02]` ❌ 不重算任何节点 ❌ 不实现第二次 Gate ❌ 不执行旧 `RoutingPlan`
❌ 不写任何运行态 ❌ 不接失败回流边（`RedecisionRequest` / `RegateRequest`）❌ 不接 `session` / `entrypoint`。

**可达性（如实登记）**：本边完成后，「**产出重入请求**」可达；「**回流真正发生**」**仍不可达**
（attempt 写入属 **C2c** / P2 DEFERRED · 节点分派未开放）—— 与 C1 / C2a / C2b-1 / C2b-2a 的
"接通 ≠ 变可达"同一口径。
"""

from __future__ import annotations

from dataclasses import dataclass

from .approval_gate_wiring import GateDecision, GateDecisionKind, GateOutcome
from .policy import PolicyFacts  # **仅类型依赖**：本模块绝不构造 / 调用 Policy（LRM-7）

__all__ = [
    "REROUTE_RATIONALE",
    "ModifyRerouteRequest",
    "RecoveryModifyWiringError",
    "build_modify_reroute_request",
]


class RecoveryModifyWiringError(RuntimeError):
    """C2b-2b-1 接线级显式失败：**不是** Policy 执行入口，也**不是** Gate 的替代品。

    ⚠️ 它只在**接线自身**的前置条件不成立时抛出：对象不是本边的契约载体、
    人类授权事实**不存在**（挂起）、或它不是 `MODIFY`（`APPROVE` / `DENY` 各有自己的出口）。
    ⚠️ 它**不得**被降级成"返回 `None`"或"返回一个空请求" —— 那会把**错误调用路径**
    掩盖成正常结果（`APPROVE` 被当成回流、挂起被当成回流，都是同一类事故）。
    """


#: **固定措辞**：本次重入的**事实来源说明**。
#:
#: ⚠️ 它**不是** `modification` 的替代品 / 摘要 / 改写 —— 人类授权说明的**唯一审计权威**是
#: ``GateOutcome.decision.modification``（本模块**零取值**，LRM-6）。
#: ⚠️ 调用方**无入参可填**（与 `CostSummary` 的"全派生量、无入参"同一纪律）。
REROUTE_RATIONALE = (
    "本次授权事实来自 GateOutcome（Approval Gate 的 MODIFY 出口，属 R4）"
    "⇒ 从 [02] Policy 重新进入决策主链"
)


@dataclass(frozen=True)
class ModifyRerouteRequest:
    """「带着新授权事实从 `[02]` 重新进入决策主链」的**机械入口**（C2b-2b-1 的唯一产出）。

    ⚠️ **pure carrier**：本类**不做值校验**，也**不表达**"是否获批"（那是 Approval 链 / Gate 的事）。
    ⚠️ ``policy_facts`` 必须是**边界外重建后的完整最终态**（`Policy.resolve` 只吃完整 facts）
    —— **不做** "base + delta"：那需要合并器 ⇒ 多一个事实 owner、多一处可造假点。
    ⚠️ 本类**不含** `modification` 的副本（或任何同义字段）：一个事实源、一个审计权威、
    不产生平行状态（§128.4 冻结）。
    """

    policy_facts: PolicyFacts
    rationale: str


def _require_gate_outcome(gate_outcome: object) -> GateOutcome:
    """身份守卫：本边只接受 C2b-2a 的**恒定载体** ``GateOutcome``。

    ⚠️ 只读 ``decision`` 一个字段 —— 四项清单与 ``needs_approval`` 属 Gate 的披露面，与回流无关。
    """
    if not isinstance(gate_outcome, GateOutcome):
        raise RecoveryModifyWiringError(
            f"build_modify_reroute_request 只接受 GateOutcome（C2b-2a 的恒定载体），收到 {gate_outcome!r}"
        )
    return gate_outcome


def _require_modify_decision(gate_outcome: GateOutcome) -> GateDecision:
    """**MODIFY 专用入口守卫**：挂起 / 非 `GateDecision` / 非 `MODIFY` ⇒ **三者全部显式失败**。

    ⚠️ 入参是**已经过身份守卫**的 `GateOutcome`（本函数不重复 `isinstance` 检查 —— 那是
    `_require_gate_outcome` 的职责）；本边只读它的 ``decision`` **一个字段**。
    ⚠️ `None`（**挂起**）不是回流的事实：它表示"仍在等待人类输入"。
    ⚠️ `APPROVE` 的含义是"按获批范围继续"、`DENY` 的含义是"终态 `APPROVAL_DENIED`"
    —— 两者**都不是**回流 ⇒ 在这里被接受就等于把另外两条出口**当成同一条**。
    """
    if gate_outcome.decision is None:
        raise RecoveryModifyWiringError(
            "GateOutcome.decision is None（**仍在等待人类输入**）⇒ 这是**挂起**，不是回流事实；"
            "不得产出重入请求"
        )
    if not isinstance(gate_outcome.decision, GateDecision):
        raise RecoveryModifyWiringError(
            f"decision 必须是 GateDecision，收到 {gate_outcome.decision!r}"
            "（Approve / Deny / Modify 三个出口各有自己的去向，本边只处理 MODIFY）"
        )
    if gate_outcome.decision.kind is not GateDecisionKind.MODIFY:
        raise RecoveryModifyWiringError(
            f"只有 MODIFY 才构成回流，收到 kind={gate_outcome.decision.kind!r}"
            "（APPROVE ⇒ 按获批范围继续；DENY ⇒ 终态 APPROVAL_DENIED —— 两者都不是回流）"
        )
    return gate_outcome.decision


def _require_policy_facts(policy_facts: object) -> PolicyFacts:
    """身份守卫：本边只接受**已重建的** ``PolicyFacts``（显式失败优先于 AttributeError）。"""
    if not isinstance(policy_facts, PolicyFacts):
        raise RecoveryModifyWiringError(
            f"policy_facts 必须是 PolicyFacts（重入 [02] 的载荷），收到 {policy_facts!r}"
            "（本边**不重建** facts：那是上游 producer 的职责）"
        )
    return policy_facts


def build_modify_reroute_request(
    *, gate_outcome: GateOutcome, policy_facts: PolicyFacts
) -> ModifyRerouteRequest:
    """把 ``MODIFY`` 的授权结果事实与**边界外重建后的** facts 组装成重入请求。

    ⚠️ 本函数**不读** ``modification``（零取值）· **不重建** facts · **不调用** `[02]` ·
    **不重算**任何节点 · **不写**运行态 · **不实现**第二次 Gate。
    ⚠️ **只有一个 return**：三类非法输入（挂起 / 非 `GateDecision` / 非 `MODIFY`）都在守卫里
    显式失败 ⇒ 本边**没有**合法的 `None` 返回路径。
    """
    _require_modify_decision(_require_gate_outcome(gate_outcome))
    return ModifyRerouteRequest(
        policy_facts=_require_policy_facts(policy_facts),
        rationale=REROUTE_RATIONALE,
    )
