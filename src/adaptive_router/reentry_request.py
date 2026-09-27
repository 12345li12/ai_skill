"""``MODIFY 重入载荷`` 的 **C2b-2b-2 协议层载体**（接线层）。

架构依据：`docs/architecture.md`

- **Approval Gate 节点行**：三种输入的控制流出口（V1 冻结）——``MODIFY`` →
  转换成新的「用户当前指令」→ **回 `[02]` Policy** → 重建 AuthorizationContext →
  从最早受影响的 Decision 节点重新计算 → Approval Judgment → 必要时再次 Gate；
  逐字禁令：**"MODIFY 永远不能直接执行旧 RoutingPlan"**；
- **§4.6**：**R4**（用户干预）"当前用户明确干预**本身就是新的授权事实**" ⇒ MODIFY 属 R4，
  **不新增 R7**；
- **Authorization Context Builder 节点行**：输入含"用户当前授权"；
  "权限聚合必须在 Approval Judgment **之前**完成"；"**不新增节点编号**"；
- **`INV-03`**（RoutingPlan 不可变）· **`INV-16`**（回流后扩大范围必须重新过 Gate —— 属后续阶段）；
- **A13.5**（人类/外部输入进 Core 的形态 = **显式事实注入**）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§126**（preflight）· **§127**（scope review 草案）·
**§132**（C2b-2b-2 preflight）· **§133**（scope review）· **§134**（范围稿冻结）· **§135**（实现授权）·
**§136**（实现记录）。

**本模块的定性（冻结口径，逐字）**：

```text
C2b-2b-2 = 再入**协议层**  ≠  再入**执行层**
```

**一条边（C2b-2b-2 只做这一件事）**：

```text
ModifyRerouteRequest（C2b-2b-1 FROZEN：完整最终态 policy_facts + rationale）
        │  ① 整体作为 `request` 字段（**不拆散**、不复制）
        │  ② **同源** task / context（identity-preserving：`is` 关系，不 copy / 不 normalize）
        │  ③ **显式** attempt（≥ 2；本边**不算、不写**）
        ▼
   ReentryRequest  ──►  交给**未来的再入入口本体**（属 `C2b-2c` / `C2c`）
```

**它是**：再入所需的**载荷**（为后续入口提供稳定协议）。
**它不是**：再入执行器 —— 不实现入口本体 · 不执行再入 · 不推进 attempt · 不驱动任何节点。

**七条口径（§134 冻结，逐条对应 `LRE` 锁）**：

1. **入口形状不变**（`LRE-1`）：本模块**不 import `router`**、不构造 `AdaptiveRouter`
   ⇒ `AdaptiveRouter.__call__(self, task, context)` 逐字节不变；
2. **facts 来源**（`LRE-2`）：`PolicyFacts` **只作类型**出现；构造点 = 0 · `replace` = 0；
3. **facts 身份**（`LRE-3`）：`request` **按对象身份**透传 ⇒ 其 `policy_facts` 与入参**同一对象**；
4. **Policy 入口唯一性**（`LRE-4`）：`Policy(` = 0 · `resolve` = 0 ⇒ 全仓 `Policy.resolve`
   调用点仍**恰好一处**（`router.py`）；
5. **attempt 边界**（`LRE-5`）：`attempt` **只作形参 / 只在守卫里被比较**；
   **不是赋值目标**；不 import `agent_manager` / `scheduler` / `execution`；不引用运行态；
6. **只搬运 / 零局部绑定**（`LRE-6`）：构造点 = 1；函数体内**没有任何局部绑定**
   ⇒ `task` / `context` 的**身份**在结构上不可能被 copy / normalize 替换；
7. **无时钟 / 无状态**（`LRE-8` / `LRE-9`）：依赖白名单 = `{contracts, recovery_modify_wiring}`。

**明确不做（越界即缺陷）**：❌ 不实现再入入口本体 ❌ 不执行再入 ❌ 不推进 attempt ❌ 不写运行态
❌ 不解析 `task` 的内容 ❌ 不重建 `PolicyFacts` ❌ 不实现第二次 Gate ❌ 不驱动 `[02]`/`[06]`
❌ 不做任何"受影响节点计算" ❌ 不执行旧 `RoutingPlan`。

**可达性（如实登记）**：本模块完成后，「**再入载荷可被产出**」**可达**；
「**真实再入执行**」**仍不可达**（入口本体与 attempt 落库属 `C2b-2c` / `C2c`）——
与既有全部载体的"接通 ≠ 变可达"同一口径。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import ProjectContext
from .recovery_modify_wiring import ModifyRerouteRequest

__all__ = [
    "ReentryRequest",
    "ReentryWiringError",
    "build_reentry_request",
]


class ReentryWiringError(RuntimeError):
    """C2b-2b-2 接线级显式失败：**不是**再入执行器，也**不是**入口本体的替代品。

    ⚠️ 它只在**接线自身**的前置条件不成立时抛出：对象不是 C2b-2b-1 的恒定载体、
    `task` 缺失、`context` 不是 `ProjectContext`、或 `attempt` 不是**第二个** attempt。
    ⚠️ 它**不得**被降级成"补一个默认值" —— 补出来的 `attempt=1` 会把**首次执行**
    写成**再入**（"同一请求生命周期中的重新进入"这一事实被伪造）。
    """


@dataclass(frozen=True)
class ReentryRequest:
    """一次**再入**的载荷（C2b-2b-2 协议层的唯一产出）。

    ⚠️ **pure carrier**：本类**不做值校验**，也**不执行**任何东西 —— 它为**未来的入口本体**
    （属 `C2b-2c` / `C2c`）提供稳定协议。
    ⚠️ ``request`` 是 C2b-2b-1 的**完整载体**（`policy_facts` + `rationale`），**不拆散**：
    `policy_facts` 是唯一事实来源（"不新增第二事实源"）。
    ⚠️ ``task`` / ``context`` 是**同源**对象（`is` 关系）：再入不是新的请求，而是
    **同一请求生命周期中的重新进入** ⇒ 本边**不 copy / 不 normalize / 不 rebuild / 不 merge**。
    ⚠️ ``attempt`` 是**显式事实**：本边**不计算**、**不写运行态**、**不从 runtime 推导**。
    """

    request: ModifyRerouteRequest
    task: object
    context: ProjectContext
    attempt: int


def _require_modify_request(request: object) -> ModifyRerouteRequest:
    """身份守卫：本边只接受 C2b-2b-1 的**恒定载体** ``ModifyRerouteRequest``。

    ⚠️ 返回**入参本身**（不复制、不重建）—— 这是 `LRE-3` 身份级证明的机制来源。
    """
    if not isinstance(request, ModifyRerouteRequest):
        raise ReentryWiringError(
            f"build_reentry_request 只接受 ModifyRerouteRequest（C2b-2b-1 的恒定载体），收到 {request!r}"
        )
    return request


def _require_task_and_context(task: object, context: object) -> ProjectContext:
    """守卫 `task` 的**存在性**与 `context` 的**身份**；返回**原对象** `context`。

    ⚠️ `task` 只做"**非空缺**"检查：本边**不解析** `task`（规范化属宿主 / `FactSource`），
    也不对它做任何类型断言 —— 它的形状由上游负责。
    ⚠️ 返回入参 `context` 而**不是**重建的对象：`LRE-6` 的零局部绑定 + 身份保留由此成立
    （若在此 copy，`out.context is context` 会立刻判红）。
    """
    if task is None:
        raise ReentryWiringError(
            "task 不得为空：再入必须携带**同源**任务对象（再入不是新的请求）"
        )
    if not isinstance(context, ProjectContext):
        raise ReentryWiringError(
            f"context 必须是 ProjectContext（同源项目上下文），收到 {context!r}"
            "（本边不重建上下文：那会引入身份漂移）"
        )
    return context


def _require_attempt(attempt: object) -> int:
    """**严格 `type is int`** 且 `attempt >= 2`（`LRE-5` 的边界）。

    ⚠️ 用 `type(...) is not int` 而不是 `isinstance` —— Python 的 `bool` 是 `int` 的子类，
    `True` 会被 `isinstance` 接受（A18.2 同一纪律）。
    ⚠️ `attempt < 2` ⇒ 显式失败：`1` 是**首次执行**，不是再入；本边**不补默认值**。
    """
    if type(attempt) is not int or attempt < 2:
        raise ReentryWiringError(
            f"attempt 必须是**第二个及以后**的 attempt（int 且 >= 2），收到 {attempt!r}"
            "（1 是首次执行，不是再入；本边不从 runtime 推导、不补默认值）"
        )
    return attempt


def build_reentry_request(
    *,
    request: ModifyRerouteRequest,
    task: object,
    context: ProjectContext,
    attempt: int,
) -> ReentryRequest:
    """把 C2b-2b-1 的请求 + **同源** task/context + **显式** attempt 组装成再入载荷。

    ⚠️ 本函数**不实现**再入入口、**不执行**再入、**不推进** attempt、**不写**运行态、
    **不解析** `task`、**不调** `[02]`、**不做**任何"受影响节点计算"。
    ⚠️ **只有一个 return**：所有非法输入都在守卫里显式失败 ⇒ 本边**没有**合法的 `None` 返回路径。
    ⚠️ **函数体内零局部绑定**：`task` / `context` 的**对象身份**因此不可能被替换。
    """
    return ReentryRequest(
        request=_require_modify_request(request),
        task=task,
        context=_require_task_and_context(task, context),
        attempt=_require_attempt(attempt),
    )
