"""Approval Gate 的 **C2b-2a 语义载体**（`[10]`… 之前的 Human Interaction Boundary 的 **Core 侧事实层**）。

架构依据：`docs/architecture.md`

- **Approval Gate 节点行**（`:1305-1356`）：HUMAN INTERACTION BOUNDARY（**五层之外**）；
  输入 = ``Approval Judgment`` 的 YES + **审批四项清单**；输出 = **人类授权事实**（approve / deny / modify）；
  "**Gate 只做三件事**：暂停 → 交还控制权 → 等待人类输入；**不聚合权限、不做审批判断**"；
- **V1 三种输入的控制流出口（冻结）**：`APPROVE` → 按获批范围继续 · `DENY` → 终态 `APPROVAL_DENIED` ·
  `MODIFY` → 转换成新的"用户当前指令"回 `[02]`（**属 C2b-2b**；"永远不能直接执行旧 RoutingPlan"；
  MODIFY 属 **R4**，**不新增 R7**）；
- **L43**：审批**只列 4 项**（拟执行动作 / 影响范围 / 主要风险 / 恢复方式）· 批准后只执行获批范围；
- **`INV-10`**：高风险副作用必须经过 Gate；
- **A13.5**（先例）：人类/外部输入进 Core 的形态 = **显式事实注入**（同 `RuntimeControlFacts`）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§120**（preflight）· **§121**（scope review）·
**§122**（冻结记录）· **§123**（实现授权）· **§124**（实现记录）。

**一条边（C2b-2a 只做这一件事）**：

```text
ApprovalJudgment（FROZEN：needs_approval=True + **四项清单**）
        │  ① 四项清单**逐字透传**
        │  ② **边界外显式注入**的人类授权事实：`GateDecision | None`
        │     · None ⇒ **仍在等待**（挂起事实）
        ▼
   GateOutcome（授权结果事实 + 挂起事实）
        ▼
   交给**编排点**（DENY ⇒ `[10].close_run(APPROVAL_DENIED)`；MODIFY ⇒ C2b-2b；APPROVE ⇒ 按获批范围继续）
```

**它是**：Gate 的**语义载体**（"人类说了什么"这件事的显式事实）。
**它不是**：Gate 本身（不暂停 / 不交还控制权 / 不等待 / 不阻塞 / 不轮询 / 不弹 UI）·
不是审批判断（`needs_approval` 只**透传**）· 不是执行者（不写终态、不跑 MODIFY、不执行获批范围）。

**六条口径（§122 冻结，逐条对应 ``LGT`` 锁）**：

1. **唯一载体**（`LGT-1`）：`GateOutcome(` 的构造点只有一处；
2. **只搬运 / 只组装 + 无内部覆盖**（`LGT-2`）：六个实参**逐字**是 `judgment.*` / `decision`，
   且**函数体内不得有任何局部绑定**；
3. **不做审批判断**（`LGT-3`）：不 import / 不调用 `ApprovalJudge`，不重算 `needs_approval`，不判"是否高风险"；
4. **只做三件事、不做第四件**（`LGT-4`）：无"聚合权限 / 决策重算 / 执行获批范围"，
   **不实现真实交互**（无 IO / 无阻塞 / 无轮询 / 无时钟状态）；
5. **不写运行态**（`LGT-5`）：不 import `agent_manager` / `scheduler` / `execution`，不写终态，不写 `no_pending_approval`；
6. **MODIFY 不越权**（`LGT-6`）：不 import / 不调用 `policy` / `router` / `planner`（回流链属 C2b-2b）。

**三条显式失败守卫（本模块的核心防线）**：

```text
① judgment 身份：不是 ApprovalJudgment ⇒ 失败
② judgment.needs_approval is not True ⇒ **失败**（Gate 不该被进入 = 接线错误；
   **不得**返回空 GateOutcome —— 那会把错误调用路径掩盖成正常结果）
③ decision 一致性：MODIFY **必须**带非空 modification；APPROVE / DENY **必须不带** ⇒ 不一致即失败
```

**明确不做（越界即缺陷）**：❌ 不实现 Gate ❌ 不做审批判断 ❌ 不覆盖 Judgment ❌ 不写运行态
❌ 不执行获批范围 ❌ 不轮询等待 ❌ 不管理时钟状态 ❌ 不跑 MODIFY 链 ❌ 不接 `session` / `entrypoint` / `[16]`。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .approval_judgment import ApprovalJudgment

__all__ = [
    "ApprovalGateWiringError",
    "GateDecision",
    "GateDecisionKind",
    "GateOutcome",
    "settle_gate",
]


class ApprovalGateWiringError(RuntimeError):
    """C2b-2a 接线级显式失败：**不是** Approval 判断或 Gate 的替代品。

    ⚠️ 它只在**接线自身**的前置条件不成立时抛出：对象不是本边的契约载体、
    `needs_approval` 不为真（**Gate 不该被进入**）、或人类授权事实与其修改内容**不自洽**。
    ⚠️ 它**不得**被降级成"返回一个空 `GateOutcome`" —— 那会把**错误调用路径**掩盖成正常结果。
    """


class GateDecisionKind(Enum):
    """人类授权的**三个出口**（架构的 V1 冻结集 —— 闭集，未知取值**不可表达**）。"""

    APPROVE = "approve"
    DENY = "deny"
    MODIFY = "modify"


@dataclass(frozen=True)
class GateDecision:
    """**边界外**给出的人类授权事实（V1 = **显式注入**；Core 不阻塞、不轮询、不弹 UI）。

    ⚠️ `MODIFY` 是**唯一**允许携带 `modification` 的出口（且必须是**非空 str**）；
    `APPROVE` / `DENY` 携带它 ⇒ **不自洽** ⇒ 显式失败（由接线守卫强制）。
    ⚠️ 本类**不解释** `modification` 的业务含义 —— 那是 **C2b-2b** 的活。
    """

    kind: GateDecisionKind
    modification: str | None


@dataclass(frozen=True)
class GateOutcome:
    """Gate 的**授权结果事实 + 挂起事实**（C2b-2a 的唯一产出）。

    ⚠️ **pure carrier**：不做值校验；四项清单与 `needs_approval` 都是**透传**来的，
    本边**不重算、不补造、不覆盖 Judgment**。
    ⚠️ `decision is None` ⇒ **仍在等待人类输入**（**挂起**）—— 它既不是失败，
    也**不是**"默认批准"；`is_pending` 是**派生属性**，本类**不新增第二个布尔事实位**。
    ⚠️ 四项清单是 **L43 的全部内容**（拟执行动作 / 影响范围 / 主要风险 / 恢复方式）—— **多一个都不许**。
    """

    needs_approval: bool
    proposed_action: str
    impact_scope: str
    main_risk: str
    recovery_method: str
    decision: GateDecision | None

    @property
    def is_pending(self) -> bool:
        """**派生只读**：是否仍在等待人类输入（= `decision is None`）。"""
        return self.decision is None


def _require_judgment(judgment: object) -> ApprovalJudgment:
    """身份守卫 + **`needs_approval` 必须为真**（Gate 只在 Judgment 说 YES 时才该被进入）。

    ⚠️ `needs_approval=False` 时调用本边 = **接线错误**；返回"空结果"会让错误路径**看起来正常**。
    """
    if not isinstance(judgment, ApprovalJudgment):
        raise ApprovalGateWiringError(
            f"judgment 必须是 ApprovalJudgment，收到 {judgment!r}"
            "（Gate 的输入是 Approval Judgment 的 YES + 四项清单）"
        )
    if judgment.needs_approval is not True:
        raise ApprovalGateWiringError(
            "Approval Judgment 未要求审批（needs_approval is not True）⇒ "
            "**Gate 不该被进入**：这是接线错误，本边不得返回空结果"
            "（要判断「是否需要审批」的是 Approval Judgment，不是 Gate）"
        )
    return judgment


def _require_decision(decision: object) -> GateDecision | None:
    """人类授权事实的守卫：`None`（挂起）合法；否则必须是**自洽**的 `GateDecision`。

    ⚠️ `MODIFY` **必须**带非空 `modification`；`APPROVE` / `DENY` **必须不带** ——
    不自洽的"授权事实"必须**当场拒绝**，不得被搬运给下游。
    """
    if decision is None:
        return None
    if not isinstance(decision, GateDecision):
        raise ApprovalGateWiringError(
            f"decision 必须是 GateDecision | None，收到 {decision!r}"
            "（None 表示仍在等待人类输入 —— 这是**挂起**，不是失败）"
        )
    if decision.kind is GateDecisionKind.MODIFY:
        if type(decision.modification) is not str or not decision.modification.strip():
            raise ApprovalGateWiringError(
                "MODIFY 必须给出非空 modification（人类改了什么必须显式）"
            )
    elif decision.modification is not None:
        raise ApprovalGateWiringError(
            f"{decision.kind.value} 不得携带 modification，收到 {decision.modification!r}"
            "（只有 MODIFY 才表达「修改」这一事实）"
        )
    return decision


def settle_gate(
    *, judgment: ApprovalJudgment, decision: GateDecision | None
) -> GateOutcome:
    """把 Judgment 的 YES + 四项清单与**显式注入的人类授权事实**组装成 `GateOutcome`。

    ⚠️ 本函数**不调用** Approval 链、**不实现** Gate、**不写运行态**、**不跑 MODIFY 链** ——
    它只产出事实（谁拿这个事实做什么，是**编排点**的事）。
    ⚠️ `decision=None` ⇒ 产出的是**挂起事实**（`is_pending` 为真）；**不抛异常**。
    """
    _require_judgment(judgment)
    _require_decision(decision)
    return GateOutcome(
        needs_approval=judgment.needs_approval,
        proposed_action=judgment.proposed_action,
        impact_scope=judgment.impact_scope,
        main_risk=judgment.main_risk,
        recovery_method=judgment.recovery_method,
        decision=decision,
    )
