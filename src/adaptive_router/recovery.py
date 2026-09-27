"""``[14] Recovery Router`` —— **Amendment A17 的实现**（CONTROL 层：只产决策）。

架构依据：`docs/architecture.md` 2 节 ``[14] Recovery Router``
（**Amendment A17**，FROZEN；含 A17.1–A17.9 七件事与九项裁决登记）；
范围、锁清单（L14-1 – L14-15）与 probe 计划见 `tests/behavior/README_step5b.md` §83。

**它是唯一合法的失败回边出口，不在主线上。它只产生决策，由对应节点重新做正式决策。**

```text
RecoveryContext（上游事实）                    recovery_context.py
        ↓
decide_recovery(*, context)                    ← 本模块
   ① 上限判定   attempt >= total_attempt_limit ⇒ **返回 None**（不查表、不分类）
   ② 分类       validation ⇒ COMPLETION_CRITERIA（A16.5）
                failure_kind ⇒ FAILURE_KIND_TO_CLASS（A17.1）
   ③ 可达性     ∉ V1_REACHABLE ⇒ 显式失败（A17.3：不得产出不可达类别）
   ④ 路由       FAILURE_CLASS_TO_TARGET（A17.2）
   ⑤ 携带事实   carry_forward（A17.4）+ minimal_correction（逐类固定措辞表）
   ⑥ 装配       RecoveryDecision                       ← **唯一构造点**
```

**「表即唯一事实来源」（A17 的实现纪律，范围稿 §83.6）：**

- **五张模块级字面量表 + 一个常量**是全部规则所在：`FAILURE_KIND_TO_CLASS` ·
  `FAILURE_CLASS_TO_TARGET` · `V1_REACHABLE` · `FAILURE_CLASS_TO_CORRECTION` · `CARRY_FORWARD_TEXT`；
  另加常量 `DECISION_CONFIDENCE`（V1 固定，见 README §84.7 上报项 2）；
- 决策函数里**不存在**对 `failure_kind` / `failure_class` 的等值比较（锁 L14-5）——
  规则**只能**在表里；
- 查表一律 `TABLE[key]`（**不得** `.get(key, default)`）；未命中 ⇒ **显式失败**（锁 L14-6）。

**明确不做（A17.5 / A17.6 / §83.8）：**

- ❌ 不写任何运行态（`BranchState` / `RoutingRun` 的单写者是 `[10]`）· ❌ 不推进 Run 级 attempt
  （`[14]` 产决策、`[10]` 写状态：**决策者 ≠ 状态写者**）；
- ❌ 不 import `adapters`（L11-1 / L11-2 / L12-2）⇒ 结构上收不到 `CapabilityMismatch`
  （能力不匹配回 Core 重新决策，不进 `[14]`）；
- ❌ 不碰收敛事实（`control_flow_converged` / `no_pending_*` 是 `[10]` 的合成输入 ——
  **INV-20 在 `[14]` 侧的机械形式就是"名称出现 = 0"**）；
- ❌ 不生成建议文本（`minimal_correction` 只能来自固定措辞表，**不得**按 `message` 自由生成）；
- ❌ 不判定事实质量（`confidence` 在 V1 是**固定的决策置信度常量**，不按文本猜测 —— 见 §84 上报项）。
"""

from __future__ import annotations

from .contracts import Confidence, FailureClass, RecoveryDecision, RecoveryTarget
from .recovery_context import CarryForwardKind, RecoveryContext

__all__ = [
    "CARRY_FORWARD_TEXT",
    "DECISION_CONFIDENCE",
    "FAILURE_CLASS_TO_CORRECTION",
    "FAILURE_CLASS_TO_TARGET",
    "FAILURE_KIND_TO_CLASS",
    "V1_REACHABLE",
    "RecoveryRouterError",
    "decide_recovery",
]


class RecoveryRouterError(RuntimeError):
    """`[14]` 的显式失败：表未命中 / 不可达类别 / 输入不可用。

    ⚠️ 它**不是**业务失败：业务失败由 `ValidationRecord(FAIL)` 表达，
    路由由 `RecoveryDecision` 表达；本异常只表示**决策无法建立**。
    """


#: **A17.1** —— `failure_kind → FailureClass`（四个冻结值的**唯一翻译点**）。
#:
#: ⚠️ 表内容 == `docs/architecture.md` 的 A17.1（测试逐行转录为期望值：**表 == 契约**）。
FAILURE_KIND_TO_CLASS: dict[str, FailureClass] = {
    "crash": FailureClass.EXTERNAL_SERVICE,
    "timeout": FailureClass.MODEL,
    "refused": FailureClass.PERMISSION,
    "network": FailureClass.EXTERNAL_SERVICE,
}

#: **A17.2** —— `FailureClass → RecoveryTarget`（九行**满射**表；五个目标全部被用到）。
FAILURE_CLASS_TO_TARGET: dict[FailureClass, RecoveryTarget] = {
    FailureClass.INPUT: RecoveryTarget.CONTEXT,
    FailureClass.PERMISSION: RecoveryTarget.PLANNER,
    FailureClass.TOOL: RecoveryTarget.CAPABILITY,
    FailureClass.MODEL: RecoveryTarget.MODEL_ROUTER,
    FailureClass.EFFORT: RecoveryTarget.MODEL_ROUTER,
    FailureClass.DECOMPOSITION: RecoveryTarget.PLANNER,
    FailureClass.DEPENDENCY: RecoveryTarget.SCHEDULER,
    FailureClass.EXTERNAL_SERVICE: RecoveryTarget.CAPABILITY,
    FailureClass.COMPLETION_CRITERIA: RecoveryTarget.PLANNER,
}

#: **A17.3** —— V1 的四个**可达**类别；其余五类**没有具名 producer** ⇒ `[14]` **不得**产出。
V1_REACHABLE: frozenset[FailureClass] = frozenset(
    {
        FailureClass.COMPLETION_CRITERIA,
        FailureClass.MODEL,
        FailureClass.PERMISSION,
        FailureClass.EXTERNAL_SERVICE,
    }
)

#: L49「采取**最小修正**」的**逐类固定措辞**（A17 的实现纪律：**不得**按 `message` 自由生成）。
#:
#: ⚠️ 措辞是"方向"，**不是**具体修法 —— `[14]` 不替对应节点做正式决定
#: （`[14]` 节点行："它只能产生决策，由对应节点重新做正式决策"）。
FAILURE_CLASS_TO_CORRECTION: dict[FailureClass, str] = {
    FailureClass.INPUT: "补齐或修正输入事实后重跑",
    FailureClass.PERMISSION: "改走可授权的最小动作后重跑",
    FailureClass.TOOL: "重查能力后按当前能力重新派发",
    FailureClass.MODEL: "调整模型档位后重试",
    FailureClass.EFFORT: "调整推理强度后重试",
    FailureClass.DECOMPOSITION: "重新拆分任务后重跑",
    FailureClass.DEPENDENCY: "重排调度以解开依赖后重跑",
    FailureClass.EXTERNAL_SERVICE: "待外部服务恢复后重查环境再跑",
    FailureClass.COMPLETION_CRITERIA: "对照未满足的完成标准重新规划",
}

#: **A17.4** —— 六类可复用资产的**文本前缀**（`carry_forward` 的机读分类位 → 审计可读文本）。
CARRY_FORWARD_TEXT: dict[CarryForwardKind, str] = {
    CarryForwardKind.VALIDATION_STATE: "验证状态",
    CarryForwardKind.EXCLUDED_DIRECTIONS: "已排除方向",
    CarryForwardKind.FACTS: "复用事实",
    CarryForwardKind.SUMMARY: "摘要",
    CarryForwardKind.LOGS: "日志",
    CarryForwardKind.MODIFICATIONS: "修改",
}

#: V1 的**决策置信度常量**（见模块 docstring 的 ❌ 最后一条与 README §84 的**上报项**）。
DECISION_CONFIDENCE: Confidence = Confidence.HIGH


def _class_for(context: RecoveryContext) -> FailureClass:
    """**分类**（A17.1 / A16.5）：两条路径各有唯一来源，**不做**自由心证。"""
    if context.validation is not None:
        # A16.5：`[13]` 的 FAIL ⇒ L49 九类里的"完成标准问题"（唯一取值）
        return FailureClass.COMPLETION_CRITERIA
    kind = context.failure_kind
    if kind not in FAILURE_KIND_TO_CLASS:
        raise RecoveryRouterError(
            f"failure_kind {kind!r} 不在 A17.1 的映射表里"
            "（V1 闭集新增了取值却未扩展表 ⇒ 显式失败，禁止兜底）"
        )
    return FAILURE_KIND_TO_CLASS[kind]


def _target_for(failure_class: FailureClass) -> RecoveryTarget:
    """**路由**（A17.2）：未命中 ⇒ 显式失败（表必须对九类**完备**）。"""
    if failure_class not in FAILURE_CLASS_TO_TARGET:
        raise RecoveryRouterError(
            f"failure_class {failure_class!r} 不在 A17.2 的路由表里"
            "（表必须覆盖 FailureClass 全部九类）"
        )
    return FAILURE_CLASS_TO_TARGET[failure_class]


def _carry_forward(context: RecoveryContext) -> tuple[str, ...]:
    """**携带事实**（A17.4）：V1 只带**有具名来源**的那一项，**不编造**。

    ⚠️ A17.4 的"排除方向必带"在 **V1 没有 producer**（`RecoveryContext` 里没有任何
    排除方向的来源）⇒ 本实现**不带**该项，并在 README §84 登记为上报项 ——
    **不得**为凑满六类而编造一条"已排除方向"。
    """
    if context.validation is not None:
        criteria = "；".join(context.failed_criteria) or "（未逐条给出）"
        return (
            f"{CARRY_FORWARD_TEXT[CarryForwardKind.VALIDATION_STATE]}：{criteria}",
        )
    return (
        f"{CARRY_FORWARD_TEXT[CarryForwardKind.FACTS]}：运行期失败类别 {context.failure_kind}",
    )


def decide_recovery(*, context: RecoveryContext) -> RecoveryDecision | None:
    """对**一次**失败事件产出回流决策；**到上限 ⇒ 返回 `None`**（不得回流）。

    ``None`` 是**正常结果**（与 `Scheduler.schedule` 的"0 child ⇒ `()` 同形"）：
    语义是"**已达 `total_attempt_limit`，不得产生回流决策**"，调用方据此走
    `TERMINAL_FAILURE`（6.5 + A17.6）。

    ⚠️ **`None` 只有这一个出口**：其余任何路径（表未命中 / 不可达类别 / 输入不合规）
    一律**显式失败**，绝不静默返回 `None`。
    """
    if not isinstance(context, RecoveryContext):
        raise RecoveryRouterError(
            f"context 必须是 RecoveryContext，收到 {context!r}"
        )

    # ① 上限判定 —— **先于分类**：到上限就不得回流（连表都不查，避免无意义的中间判断）
    if context.attempt >= context.total_attempt_limit:
        return None

    # ② 分类（A17.1 / A16.5）
    failure_class = _class_for(context)

    # ③ 可达性（A17.3：不得产出 V1 不可达类别）
    if failure_class not in V1_REACHABLE:
        raise RecoveryRouterError(
            f"{failure_class!r} 在 V1 没有具名 producer（A17.3）⇒ 不得进入 RecoveryDecision"
            "（「没有事实来源」≠「不支持」）"
        )

    # ④ 路由（A17.2）· ⑤ 携带事实（A17.4）
    route_to = _target_for(failure_class)
    carry_forward = _carry_forward(context)

    # ⑥ 唯一构造点
    return RecoveryDecision(
        failure_class=failure_class,
        route_to=route_to,
        attempt=context.attempt,
        total_attempt_limit=context.total_attempt_limit,
        carry_forward=carry_forward,
        minimal_correction=FAILURE_CLASS_TO_CORRECTION[failure_class],
        confidence=DECISION_CONFIDENCE,
    )
