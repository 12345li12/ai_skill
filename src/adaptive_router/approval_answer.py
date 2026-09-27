"""``答案回收 seam`` 的 **C2b-2c-2 授权结果事实载体**（接线层）。

架构依据：`docs/architecture.md`

- **Approval Gate 节点行**：五步图 —— 暂停 → 把控制权交还给用户 → **等待明确输入** →
  **批准 / 拒绝 / 修改** → **产生新的授权事实**；"**Gate 只做三件事**"；
- **三种输入的控制流出口（V1 冻结）**：`APPROVE` → 按获批范围继续 · `DENY` → 终态
  `APPROVAL_DENIED` · `MODIFY` → 回 `[02]`（**永远不能直接执行旧 `RoutingPlan`**）；
- **§4.4**：回流"**必须重新进入授权判定链，但不一定真正进入 Approval Gate**"；
- **`INV-10`**：高风险副作用必须经过 Gate · **`INV-16`**：回流后扩大范围必须重新过 Gate
  （⚠️ 其**对比判据** = `G6` **GAP CONFIRMED**，本边**不发明**）；
- **A18.2 / A18.3**：`no_pending_approval` 的**唯一合法来源** =
  "**Approval Judgment 结论 + Gate 解析状态**" —— 本边产出的正是"**解析状态**"那一半的事实；
- **C2b-2a**（`settle_gate` = `GateOutcome` 的**唯一语义入口**）·
  **C2b-2b-1 F1**（人类答案文本 → 结构化事实的**归一化权在边界外**）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§144**（preflight）· **§145**（scope review）·
**§146**（冻结记录 / 实现授权）· **§147**（实现记录）。

**本模块的定性（冻结口径，逐字）**：

```text
C2b-2c-2 = 答案回收 seam（"产生新的授权事实"这一段）
         ≠ 同一 run 恢复 ≠ 继续执行 ≠ 再次 Gate 判定 ≠ attempt 落库
```

**一条边（C2b-2c-2 只做这一件事）**：

```text
ApprovalJudgment（判据来源，FROZEN）
        │  ① 判据来源守卫（身份 + `needs_approval is True`）
        │
GateOutcome（**被回答的挂起卡片**）
        │  ② 挂起态守卫（`is_pending is True` ⇒ 已解析者**不得重复回收**）
        │
GateDecision（**边界外**归一化后的结构化答案）
        ▼
   settle_gate(judgment=…, decision=…)  ──►  GateOutcome（**已解析**：新的授权事实）
```

**它是**：把"人类答案"回收成"**已解析的授权结果事实**"的**接线 seam**。
**它不是**：答案解析器（不读文本）· 执行恢复器（不恢复 run）· Gate 本体（不暂停 / 不等待）。

**六条口径（§145 冻结，逐条对应 `LGA` 锁）**：

1. **`settle_gate` 是唯一语义入口**（`LGA-2`）：本模块**不构造** `GateOutcome`、
   **不重复实现** `decision` 的自洽性守卫（MODIFY 必带非空 `modification` 等）；
2. **卡片与卡片边不变**（`LGA-1`）：`GateOutcome` 六字段与 `settle_gate` 签名**不动**；
3. **零取值**（`LGA-3`）：**不读** `decision.modification` —— 人类答案的文本**不进入本边**
   （它的唯一审计权威就是 `GateDecision.modification`，C2b-2a FROZEN）；
4. **只对挂起卡片回收**（`LGA-4`）：已解析（`is_pending is False`）⇒ **显式失败**
   —— 那表示人类**已经**回答过，再回收一次会产出**第二份授权事实**；
5. **不写运行态**（`LGA-5`）：不调用 `[10]` 任何方法 · 不写终态 / attempt · 不引用 `RoutingRun`；
6. **不接另一边界**（`LGA-6`）：不 import `session` / `entrypoint` / `approval_question`
   （会话级 consent ≠ 单次动作审批；问题载体**不是**本边的输入）。

**明确不做（越界即缺陷）**：❌ 不恢复同一 run ❌ 不继续执行 ❌ 不判"再次 Gate" ❌ 不推进 attempt
❌ 不做节点分派 ❌ 不接 `C2c` ❌ 不解析人类答案 ❌ 不构造 `GateDecision` / `GateOutcome`
❌ 不新增身份字段 ❌ 不改契约。

**可达性（如实登记）**：本模块完成后，「**产生新的授权事实**」**可达**（五步图第 4 步落地）；
「**同一 run 恢复**」「**继续执行**」「**再次 Gate 判定**」「**attempt 落库**」「**节点分派**」
**仍不可达**（属 `C2b-2c-3` / `C2c` / 编排点）。
"""

from __future__ import annotations

from .approval_gate_wiring import GateDecision, GateOutcome, settle_gate
from .approval_judgment import ApprovalJudgment

__all__ = [
    "ApprovalAnswerError",
    "recover_gate_answer",
]


class ApprovalAnswerError(RuntimeError):
    """C2b-2c-2 接线级显式失败：**不是** Gate，也**不是**执行恢复器。

    ⚠️ 它只在**接线自身**的前置条件不成立时抛出：判据来源不是 `ApprovalJudgment` /
    未要求审批、或**被回答的卡片已经解析过**（人类已经答过 ⇒ 不得重复回收）。
    ⚠️ 它**不得**被降级成"照常回收" —— 对**已解析**的卡片再回收一次，会产出**第二份授权事实**，
    使 `no_pending_approval` 的来源（A18.2 / A18.3）出现两个互相矛盾的"结论"。
    """


def _require_judgment(judgment: object) -> ApprovalJudgment:
    """判据来源守卫：必须是 `ApprovalJudgment`，且它**要求审批**。

    ⚠️ 与 `settle_gate` 内部的同名守卫是**同一判据、不同异常类型**：那边的 `ApprovalGateWiringError`
    属 C2b-2a 的接线语义；本边的失败**归属 C2b-2c-2**（答案回收的前置条件），
    因此必须抛 `ApprovalAnswerError` —— 否则调用方无法分辨"问题在 Gate 载体"还是"在答案回收"。
    """
    if not isinstance(judgment, ApprovalJudgment):
        raise ApprovalAnswerError(
            f"recover_gate_answer 只接受 ApprovalJudgment（判定链的产物），收到 {judgment!r}"
        )
    if judgment.needs_approval is not True:
        raise ApprovalAnswerError(
            "judgment.needs_approval is not True ⇒ 该任务**本就不需要审批**，"
            "不存在'被回答的问题'可回收（要判『是否需要审批』的是 Approval Judgment，不是本边）"
        )
    return judgment


def _require_pending_outcome(answered: object) -> GateOutcome:
    """**只有仍在等待人类输入的问题**才能被回收（已解析 ⇒ 显式失败）。

    ⚠️ 已解析（`is_pending is False`）意味着 `APPROVE` / `DENY` / `MODIFY` **已经产生过**，
    再回收一次就是**重复授权事实**。
    """
    if not isinstance(answered, GateOutcome):
        raise ApprovalAnswerError(
            f"answered 必须是 GateOutcome（被回答的那张卡片），收到 {answered!r}"
        )
    if answered.is_pending is not True:
        raise ApprovalAnswerError(
            "answered 不在挂起态（is_pending is not True）⇒ 人类**已经回答过**，"
            "不得重复回收（重复回收会产出第二份授权事实）"
        )
    return answered


def recover_gate_answer(
    *, judgment: ApprovalJudgment, decision: GateDecision, answered: GateOutcome
) -> GateOutcome:
    """把**已归一化**的人类答案回收成**已解析的授权结果事实**。

    ⚠️ **`settle_gate` 是唯一语义入口**：本函数**不构造** `GateOutcome`、**不重复实现**
    `decision` 的自洽性守卫 —— 那两件事只在 ``settle_gate`` 里发生。
    ⚠️ **先守卫、后结算**：对"已答过的问题"回收时，**不会先产出任何结果再失败**。
    ⚠️ `decision` 的构造权在**边界外**（人类答案的归一化）—— 本边**只接收**，
    **不解析**答案文本（`modification` **零取值**）。
    ⚠️ 本函数**不恢复** run、**不继续执行**、**不判**"是否需要再次 Gate"、**不写**运行态。
    ⚠️ **只有一个 return**：所有非法输入都在守卫里显式失败 ⇒ 无合法的 `None` 返回路径。
    ⚠️ **函数体内零局部绑定**：三个入参的对象身份因此不可能被中途替换。
    """
    _require_judgment(judgment)
    _require_pending_outcome(answered)
    return settle_gate(judgment=judgment, decision=decision)
