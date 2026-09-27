"""``RedecisionRequest → RegateRequest`` 的 **C2b-1 载体**（接线层）。

架构依据：`docs/architecture.md`

- **§4.4 Recovery 回流流**：`[14]` 产出决策 → 重决策 → **必要时重新 Cost Estimate** →
  **Approval Judgment** → `needs_approval?` → 继续 / **Approval Gate**；
- **§4.6 六条回流路径授权闭合表**：**R1 / R2 / R3** 的**授权路径**是
  "经 `[06]` → `[15a]` → **Judgment**"（**路径不跳过**），
  而"**是否真实进入 Gate**"是**条件性**的（产生新风险 / 成本 / 范围 / 副作用时进入）；
- **`INV-16`**：失败回流后**任何**扩大范围 / 提高档位 / 增加成本 / 增加副作用的尝试，
  **必须重新过 Approval Gate** ⇒ 本模块把这件事实**显式化**（`escalates`）；
- **L43 / `INV-10`**：审批四项清单 · 批准后只执行获批范围 ⇒ 属 **Gate 语义（C2b-2）**，本模块**不实现**；
- **A6**：`EffectiveApprovalConstraints` 缺失 ⇒ **显式失败**（fail-open 与 fail-closed 双向排除）；
- **A7 / A8**：ordinary 三判据与 disclosure 的唯一入口 —— 三者都属**判定链**，本模块**不调用、不 import**；
- **A17**：`[14]` **只路由不执行**（它不做授权判定）；
- **A18.2**：两个收敛事实的**严格 `type is bool`** 先例（本模块对 `escalates` 沿用同一纪律）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§114**（preflight）· **§115**（scope review）·
**§116**（冻结记录）· **§117**（实现授权）· **§118**（实现记录）。

**一条边（C2b-1 只做这一件事）**：

```text
RedecisionRequest（C2a 冻结载体：身份 + route_to + L49 事实）
        │  ① 五个字段**逐字透传**
        │  ② 调用方**显式给出**的加码事实 `escalates`（INV-16 的触发条件）
        ▼
   RegateRequest  ──►  「授权链重新进入」的**机械入口**（交给编排点 → Judgment；必要时 → Gate）
```

**它是**：授权链重新进入的**机械入口**。
**它不是**：授权决定器（Judgment 属 Approval 链）· **Gate**（属 C2b-2）· 执行恢复器（attempt 写入属 C2c）。

**七条口径（§116 冻结，逐条对应 ``LRG`` 锁）**：

1. **唯一载体**（`LRG-1`）：`RegateRequest(` 的构造点只有一处；
2. **只透传 / 只组装**（`LRG-2`）：六个实参**逐字**是 `request.*` / `escalates`，
   且**函数体内不得有任何局部绑定** ⇒ ``escalates = False`` 式的**内部覆盖**在结构上必然变红；
3. **不产判定**（`LRG-3`）：不 import `approval_judgment` / `risk`，不构造 `ApprovalJudgment`；
4. **不实现 Gate / 不假装批准**（`LRG-4`）：本 DTO **没有**任何"批准结果"字段；
   `needs_approval` / gate 语义**一个都不出现**；
5. **不重估成本 / 不重跑决策链**（`LRG-5`）：不 import `cost*` / `planner` / `model_router` / `router`；
6. **不写运行态**（`LRG-6`）：不 import `agent_manager` / `scheduler` / `execution`，不调用任何控制方法；
7. **无时钟 / 无状态**（`LRG-8` / `LRG-9`）：依赖白名单 = `{contracts, recovery_route_wiring}`。

**明确不做（越界即缺陷）**：❌ 不判定 `needs_approval` · ❌ 不创建 Gate · ❌ 不假装已批准 ·
❌ 不用 `needs_approval` 替代 Gate · ❌ 不自动通过 · ❌ 不携带新决策事实
（`Plan` / `ModelSelection` / `CostEstimate` —— 携带**部分**判定输入 = **半完整 Approval 输入**，
会诱导下游误认为可以直接 Judgment）· ❌ 不碰 attempt。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import BranchId, RecoveryTarget
from .recovery_route_wiring import RedecisionRequest

__all__ = [
    "RecoveryRegateWiringError",
    "RegateRequest",
    "build_regate_request",
]


class RecoveryRegateWiringError(RuntimeError):
    """C2b-1 接线级显式失败：**不是** `[14]` 决策或 Approval 判定的替代品。

    ⚠️ 它只在**接线自身**的前置条件不成立时抛出：对象不是 C2a 的请求载体、
    或 ``escalates`` 不是**严格 bool**（INV-16 的触发事实**不得**被 `0` / `1` / `"true"` / 缺省替代）。
    ⚠️ 它**不得**被降级成"默认没有加码" —— 那会把**伪造事实**写成合法的重过门请求。
    """


@dataclass(frozen=True)
class RegateRequest:
    """「授权链重新进入」的**机械入口**（C2b-1 的唯一产出）。

    ⚠️ **pure carrier**：本类**不做值校验**，也**没有**任何"批准结果"字段 ——
    它不表达"是否获批"（那是 Approval 链 / Gate 的事），只表达
    「**有这次的决策要被重新授权**」+「**是否加码**」这两件**事实**。
    ⚠️ 六个字段全部来自 C2a 的 `RedecisionRequest`（身份 + `route_to` + L49 事实）
    与**调用方显式给出**的 `escalates`；本边**一个都不重算、不补造**。
    ⚠️ **不含新决策事实**（`Plan` / `ModelSelection` / `CostEstimate`）：携带**部分**判定输入
    会制造"**半完整 Approval 输入**"，诱导下游误认为可以直接 Judgment。
    """

    branch_id: BranchId
    attempt: int
    target: RecoveryTarget
    minimal_correction: str
    carry_forward: tuple[str, ...]
    escalates: bool


def _require_request(request: object) -> RedecisionRequest:
    """类型守卫：本边只接受 **C2a 的恒定请求载体**（显式失败优先于 AttributeError）。"""
    if not isinstance(request, RedecisionRequest):
        raise RecoveryRegateWiringError(
            "build_regate_request 只接受 RedecisionRequest（C2a 的重决策请求载体），"
            f"收到 {request!r}"
        )
    return request


def _require_escalates(value: object) -> bool:
    """**严格 `type is bool`**：INV-16 的触发事实不得被 `0` / `1` / `"true"` / 缺省替代。

    ⚠️ "没收到就当作没有加码"是 **fail-open**（回流后加码却不过门）；"没收到就当作有加码"
    会把错误事实写进请求 ⇒ 两者都被排除，唯一正确语义是**显式失败**（A6 同一纪律）。
    """
    if type(value) is not bool:
        raise RecoveryRegateWiringError(
            f"escalates 必须是 bool（严格 type is bool），收到 {value!r}"
            "（INV-16 的触发事实不得由 0/1/「true」/缺省替代 —— A18.2 同一纪律）"
        )
    return value


def build_regate_request(
    *, request: RedecisionRequest, escalates: bool
) -> RegateRequest:
    """把 C2a 的请求 + **显式加码事实**组装成"授权链重新进入"的请求。

    ⚠️ **非加码也照样产出请求**（§4.6：授权**路径不跳过**）——`escalates=False` 是**正常输入**，
    只有"**是否真实进入 Gate**"才是条件性的。因此本函数**只有一个 return**（没有 `None` 分支）。
    ⚠️ 本函数**不调用** Approval 链、**不实现** Gate、**不重估**、**不重跑**、**不写运行态**。
    """
    _require_request(request)
    _require_escalates(escalates)
    return RegateRequest(
        branch_id=request.branch_id,
        attempt=request.attempt,
        target=request.target,
        minimal_correction=request.minimal_correction,
        carry_forward=request.carry_forward,
        escalates=escalates,
    )
