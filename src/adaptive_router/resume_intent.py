"""``恢复意图`` 的 **C2b-2c-3 授权事实 + 地址载体**（接线层）。

架构依据：`docs/architecture.md`

- **Approval Gate 节点行**：五步图 —— 暂停 → 把控制权交还给用户 → 等待明确输入 →
  **批准 / 拒绝 / 修改** → **产生新的授权事实**；"**Gate 只做三件事**"；
- **三种输入的控制流出口（V1 冻结）**：`APPROVE` → 按获批范围继续 · `DENY` → 终态
  `APPROVAL_DENIED` · `MODIFY` → 回 `[02]`（**永不执行旧 `RoutingPlan`**）；
- **§4.6**：**R4**（用户干预）"当前用户明确干预**本身就是新的授权事实**"；
- **6.4 / L47**：`[10] Agent Manager` 是 **运行态唯一写者**（`RoutingRun` / `AgentRun` / `BranchState`）；
- **9.1**：`total_attempt_limit = 2`（首次 + 最多一次恢复执行）；
- **A13.3**：`RoutingRun.branches` 是 **attempt history**；**A13.5**（显式外部事实注入先例）；
- **`INV-03`**（`RoutingPlan` 不可变）· **`INV-16`**（回流扩大范围须重过 Gate）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§149**（preflight）· **§150**（scope review）·
**§151**（范围稿冻结）· **§152**（实现授权）· **§153**（实现记录）。

**本模块的定性（冻结口径，逐字）**：

```text
C2b-2c-3 = 授权事实 → 恢复意图（Resume Intent）
         ≠ 同一 run 恢复 ≠ 继续执行 ≠ attempt 推进 ≠ execution state mutation ≠ 节点分派
```

**一条边（C2b-2c-3 只做这一件事）**：

```text
GateOutcome（**已解析**的授权结果事实 —— C2b-2c-2 产出）
        │  ① 授权事实守卫（身份 + **必须已解析**：挂起态 = 还没有授权事实）
        │
run_id / branch_id / attempt / total_attempt_limit（**调用方显式注入**的地址）
        │  ② 地址守卫（非空 str · `1 <= attempt <= total_attempt_limit`）
        ▼
   ResumeIntent  ──►  交给**未来的消费者**（`C2c` 之后的编排点）
```

**它是**：**恢复意图**的载体 —— 回答"**需要恢复什么**"。
**它不是**：恢复器（不恢复 run）· 执行器（不继续执行）· 运行态写入者（不推进 attempt）·
去向解释器（**不描述"恢复后去哪"**）。

**五条口径（§151 冻结，逐条对应 `LRS` 锁）**：

1. **Gate 三件套不变**（`LRS-1`）：`GateOutcome` 六字段 · `ApprovalQuestion` 五字段 ·
   `settle_gate` 签名**不动**；本模块**不构造** `GateOutcome` / `GateDecision`；
2. **唯一载体 / 只搬运**（`LRS-2`）：五个字段**逐字**来自形参；**函数体内零局部绑定**；
3. **零取值**（`LRS-3`）：**不读** `decision.modification`、**不引用** `decision`
   （人类答案的文本**不进入本边**）；
4. **地址显式且自洽**（`LRS-4`）：`run_id` / `branch_id` 非空 str；`attempt` 与
   `total_attempt_limit` 的关系 `1 <= attempt <= limit` **在构造时校验**；
5. **不写运行态 / 不推进 attempt**（`LRS-5`）：不调用 `[10]` 任何方法 · `attempt`
   **不是赋值目标**（只承载、只比较）· 不 import `agent_manager` / `scheduler` / `execution`。

**明确不做（越界即缺陷）**：❌ 不恢复 run ❌ 不继续执行 ❌ 不选节点 / 不分派 ❌ 不写运行态
❌ 不推进 / 不落库 attempt ❌ 不新增 `[10]` 能力 ❌ 不建注册表 ❌ 不从运行态或历史问题推导地址
❌ 不解析人类答案 ❌ 不解释三出口去向 ❌ 不新增身份字段 ❌ 不改契约。

**可达性（如实登记）**：本模块完成后，「**恢复意图可被产出**」**可达**；
「**同一 run 恢复**」「**继续执行**」「**attempt 落库**」「**节点分派**」「**真实恢复回路**」
**仍不可达**（`[10]` 无 resume 能力 · `P2` DEFERRED · 编排点不存在）——
你的裁定 ⑤ 逐字："`C2b-2c-3` 完成 **≠ 系统可恢复**"。
"""

from __future__ import annotations

from dataclasses import dataclass

from .approval_gate_wiring import GateOutcome
from .contracts import BranchId, RunId

__all__ = [
    "ResumeIntent",
    "ResumeIntentError",
    "build_resume_intent",
]


class ResumeIntentError(RuntimeError):
    """C2b-2c-3 接线级显式失败：**不是**恢复器，也**不是**运行态写入者。

    ⚠️ 它只在**接线自身**的前置条件不成立时抛出：授权事实还不是**已解析**的
    （挂起态 = 还没有授权事实）、地址不合法（空串 / 类型错 / `attempt` 越界）、
    或授权事实本身被塞进了**额外字段**（身份 / 恢复 / 决策类 —— 那是越界形状）。
    ⚠️ 它**不得**被降级成"照样产出意图" —— 一个没有授权事实或地址越界的"恢复意图"，
    会被下游当成可以据此**改动运行态**的依据。
    """


#: **越界字段名**（授权事实侧**不得出现**的名字）。
#:
#: ⚠️ 它们是"**身份 / 恢复 / 决策**"三类越界语义的具名集合 ——
#: 人类答案文本（`modification`）的**唯一审计权威**在 `GateDecision`（C2b-2a FROZEN），
#: run / branch 的地址则**只由调用方显式注入**（`ResumeIntent` 的地址字段），
#: 因此这些名字一旦出现在 `GateOutcome` 上，就意味着**有别的边在偷偷塞事实**。
_FORBIDDEN_FACT_FIELDS = (
    "gate_id",
    "run_id",
    "branch_id",
    "attempt",
    "answer",
    "resume",
    "resume_intent",
    "terminal",
)


@dataclass(frozen=True)
class ResumeIntent:
    """一次**恢复**的意图（C2b-2c-3 的唯一产出）。**恰好五字段**，多一个都不许。

    ⚠️ **pure carrier**：本类不做值校验，也**不执行**任何东西 —— 它回答的是
    "**需要恢复什么**"，**不是**"**恢复后去哪执行**"（去向由编排点按 V1 三出口处理）。
    ⚠️ ``gate_outcome`` 必须是**已解析**的授权结果事实（`is_pending is False`）：
    挂起态意味着**还没有授权事实**，据此形成"恢复意图"是无依据的。
    ⚠️ ``run_id`` / ``branch_id`` / ``attempt`` / ``total_attempt_limit`` 是**地址事实**，
    **全部由调用方显式注入** —— 本边**不反查、不推导、不建注册表**。
    ⚠️ ``attempt`` 是"**从哪个执行边界继续**"的地址，**不是**要推进的目标：
    `1 <= attempt <= total_attempt_limit`，**只承载、只校验**，本边**不写**它。
    """

    gate_outcome: GateOutcome
    run_id: RunId
    branch_id: BranchId
    attempt: int
    total_attempt_limit: int


def _require_authorization(gate_outcome: object) -> GateOutcome:
    """授权事实守卫：必须是 `GateOutcome`，且**必须是已解析态**。

    ⚠️ 与 C2b-2c-1 的"**只对挂起卡片产出问题**"**互补**：那边要求"还在等"，
    这边要求"**已经答完**"；两侧都不允许"状态不明"的卡片通过。
    """
    if not isinstance(gate_outcome, GateOutcome):
        raise ResumeIntentError(
            f"gate_outcome 必须是 GateOutcome（授权结果事实），收到 {gate_outcome!r}"
        )
    if gate_outcome.is_pending is not False:
        raise ResumeIntentError(
            "gate_outcome 仍在挂起态（is_pending is not False）⇒ **还没有授权事实**，"
            "不得据此形成恢复意图（先经 C2b-2c-2 回收人类答案）"
        )
    return gate_outcome


def _require_run_id(run_id: object) -> RunId:
    """地址守卫：`run_id` 必须是**非空 str**（**严格 `type is str`**）。"""
    if type(run_id) is not str or run_id == "":
        raise ResumeIntentError(
            f"run_id 必须是非空 str（要恢复哪个 run），收到 {run_id!r}"
            "（地址只能由调用方显式注入，本边不反查运行态）"
        )
    return run_id


def _require_branch_id(branch_id: object) -> BranchId:
    """地址守卫：`branch_id` 必须是**非空 str**（**严格 `type is str`**）。"""
    if type(branch_id) is not str or branch_id == "":
        raise ResumeIntentError(
            f"branch_id 必须是非空 str（从哪个分支边界继续），收到 {branch_id!r}"
        )
    return branch_id


def _require_attempt_and_limit(
    attempt: object, total_attempt_limit: object
) -> tuple[int, int]:
    """地址守卫：`attempt` 与 `total_attempt_limit` 的类型与**边界关系**。

    ⚠️ 用 `type(...) is not int` 而不是 `isinstance` —— Python 的 `bool` 是 `int` 的子类
    （A18.2 同一纪律）。
    ⚠️ `1 <= attempt <= total_attempt_limit`：`attempt` 是"**从哪个执行边界继续**"的地址
    （`FIRST_ATTEMPT = 1` 是合法取值），**不是**"已经失败过几次"——后者是
    `ReentryRequest.attempt >= 2` 的语义（C2b-2b-2）。
    """
    if type(total_attempt_limit) is not int or total_attempt_limit < 1:
        raise ResumeIntentError(
            f"total_attempt_limit 必须是 >= 1 的 int，收到 {total_attempt_limit!r}"
        )
    if type(attempt) is not int or attempt < 1 or attempt > total_attempt_limit:
        raise ResumeIntentError(
            f"attempt 必须是 1 <= attempt <= total_attempt_limit 的 int，收到 {attempt!r}"
            f"（上限 {total_attempt_limit!r}）—— 本边只承载地址，**不推进** attempt"
        )
    return attempt, total_attempt_limit


def _require_forbidden_fields(gate_outcome: GateOutcome) -> None:
    """形状守卫：授权事实**不得**携带"身份 / 恢复 / 决策"类的**越界字段**。

    ⚠️ 这是 **C2b-2c-3 的边界声明**：地址与授权事实**分属两侧**
    （地址由调用方注入、授权事实由 C2b-2c-2 产出）。若 `GateOutcome` 上出现了
    `run_id` / `attempt` / `answer` 这类名字，说明**有别的边把两侧的责任混在了一起** ——
    那必须显式失败，而不是被本边默认接受。
    """
    for name in _FORBIDDEN_FACT_FIELDS:
        if getattr(gate_outcome, name, None) is not None:
            raise ResumeIntentError(
                f"授权事实不得携带越界字段 {name!r}（身份 / 恢复 / 决策类）—— "
                "地址只能由调用方显式注入，两者不得混在同一个载体上"
            )


def build_resume_intent(
    *,
    gate_outcome: GateOutcome,
    run_id: RunId,
    branch_id: BranchId,
    attempt: int,
    total_attempt_limit: int,
) -> ResumeIntent:
    """把**已解析的授权事实**与**显式注入的地址**组装成恢复意图。

    ⚠️ 本函数**不恢复** run、**不继续执行**、**不推进** attempt、**不写**运行态、
    **不解析**人类答案、**不判**"恢复后去哪"（去向属编排点）。
    ⚠️ **只有一个 return**：所有非法输入都在守卫里显式失败 ⇒ 无合法的 `None` 返回路径。
    ⚠️ **函数体内零局部绑定**：五个字段的对象身份与值因此不可能被中途替换。
    """
    _require_forbidden_fields(_require_authorization(gate_outcome))
    return ResumeIntent(
        gate_outcome=_require_authorization(gate_outcome),
        run_id=_require_run_id(run_id),
        branch_id=_require_branch_id(branch_id),
        attempt=_require_attempt_and_limit(attempt, total_attempt_limit)[0],
        total_attempt_limit=_require_attempt_and_limit(attempt, total_attempt_limit)[1],
    )
