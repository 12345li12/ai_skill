"""router —— **Router Core composition**：把已冻结的决策节点接成一条真实主链。

架构依据：`docs/architecture.md` v1.0 FROZEN（含 Amendment A1–A8）。

```text
Router 负责 orchestration，**不成为 decision-maker**。
```

**入口形状是冻结的**（`entrypoint.py` 的 ``RouterCore`` 协议）：

```python
def __call__(self, task, context) -> RouterOutcome
```

该协议**有意不含** ``SessionGateResult`` —— Core 不需要知道
UNASKED / DISABLED / bypass（BYPASS 在 Router 之外）。

**本模块只做四件事：**

1. 从**唯一事实入口** ``facts_source`` 取已规范化的事实；
2. 按冻结顺序调用各决策节点，**传递**它们已经形成的产物；
3. 做 **A5 规定的两处组合层派生**（``max_concurrency`` / ``retry_allowance``）；
4. 按 A8 通过 ``build_disclosure()`` 取 disclosure，再调用 Approval Judgment。

**⚠️ Router 不做的事（结构锁随实现一起加）：**

    ❌ 不决定 DIRECT / STAGED / MULTI_AGENT（那是 [05] Planner）
    ❌ 不选模型（那是 [06] Model Router）
    ❌ 不改 CostEstimate（那是 [15a]）；不判 needs_approval（那是 Approval Judgment）
    ❌ 不直接构造 ApprovalDisclosure —— **必须**经 build_disclosure()（A8）
    ❌ 不自己拼 proposed_action / 推 impact_scope / 生成 recovery_method（A8.3）
    ❌ 不解析自然语言 task、不重新推导事实、**缺事实即显式失败**（5B.5.0 ①）
    ❌ 不出现平台细节（Codex / OpenAI 的字段形状、API 形状）

⚠️ **``RouterFacts`` / ``RouterOutcome`` 都是实现级的**：
``RouterFacts`` 只承载已经规范化的事实 —— 事实来源与 owner 仍以
A1/A2/A3/A5/A6/A8 与既有节点定义为准，本模块**不取得任何事实所有权**；
``RouterOutcome`` 只聚合已经形成的 Core 产物，**不产生新决策、不修改任何产物**。
两者都**不进 ``contracts/``**，也**不代表第 19 个架构节点**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .approval_constraints import EffectiveApprovalConstraints
from .approval_disclosure import build_disclosure
from .approval_judgment import ApprovalJudge
from .capabilities import CapabilityRegistry
from .contracts import (
    ApprovalJudgment,
    CapabilitySnapshot,
    CostEstimate,
    Plan,
    ProjectContext,
    RoutingPlan,
)
from .cost import CostEstimatorWiring, UnitCostEstimate
from .model_router import ModelRouter
from .planner import Evaluator, Planner, TaskFacts
from .policy import Policy, PolicyFacts, PolicyResult
from .risk import RiskEvaluator, RiskInputs
from .routing_plan import RoutingPlanAssembler

__all__ = [
    "FIRST_ATTEMPT",
    "TOTAL_ATTEMPT_LIMIT",
    "AdaptiveRouter",
    "FactSource",
    "RouterError",
    "RouterFacts",
    "RouterOutcome",
]


class RouterError(RuntimeError):
    """composition 无法安全继续。

    ⚠️ **缺事实即显式失败** —— Router **不补默认值**：
    补出来的默认事实会让"某节点没拿到输入"变成一条看起来正常的调度结果。
    """


#: V1 冻结的 attempt 上限（架构 9.1："首次 + 最多一次恢复执行"）。
#: A5.5 的 ``retry_allowance`` 由它派生，**不得**随意配置。
TOTAL_ATTEMPT_LIMIT = 2

#: 首次执行是**结构性**的（A5.6）：核心在真正执行之前运行，此时还没有生命周期。
FIRST_ATTEMPT = 1


@dataclass(frozen=True)
class RouterFacts:
    """**唯一事实入口**的产物：只承载**已经规范化**的事实。

    ⚠️ 本 DTO **不拥有**任何事实：每个字段的 owner 由既有冻结定义决定：

        policy          [01] Session / [17] Project Context / 用户与平台事实的规范化
        task            [04] / [05] 共享的当前任务规划事实（A2）
        risk            Risk Assessment 的事实输入
        recovery_method A8.3 的**显式 recovery fact**（Core 外部 / 上游提供）；
                        ``None`` 表示"未提供"，交由 A8.3 的三分支规则处理

    ⚠️ **不含 ``capability``**（5B.5.1 订正）：能力快照**不是**"开跑前已形成的静态事实" ——
    它由冻结主链上的 ``[03]`` 节点在 ``[02]`` **之后**产出，见 :class:`AdaptiveRouter`。
    """

    policy: PolicyFacts
    task: TaskFacts
    risk: RiskInputs
    recovery_method: str | None = None


class FactSource(Protocol):
    """把宿主的 ``task`` 规范化成 :class:`RouterFacts` 的**唯一**入口。

    ⚠️ Router **不解析自然语言 task**、**不重新推导**任何事实字段；
    它只接收本协议的产物并转交。缺事实时由实现方**显式失败**。
    """

    def __call__(self, task: Any, context: ProjectContext) -> RouterFacts:
        ...  # pragma: no cover - 协议声明


@dataclass(frozen=True)
class RouterOutcome:
    """实现级 orchestration 结果：只聚合**已经形成**的 Core 产物。

    每个字段都有**具名**的冻结下游消费者，因此不携带上游中间产物：

        plan      → [08] Context Builder（架构 2 节 [08] 输入行）
        routing   → [11] Runtime Adapter（不可变，INV-03）
        costs     → [15b] Cost Ledger / [15c] Cost Summary
        approval  → Approval Gate（``needs_approval`` + 四项清单）

    ⚠️ **不含** Codex / OpenAI 等平台字段；**不含**新决策。
    """

    plan: Plan
    routing: RoutingPlan
    costs: tuple[UnitCostEstimate, ...]
    approval: ApprovalJudgment


@dataclass
class AdaptiveRouter:
    """按冻结顺序接线的 Router Core。

    所有节点都可注入（默认各自用真实实现），因此本类只做 orchestration。
    """

    facts_source: FactSource
    capabilities: CapabilityRegistry
    policy: Policy = field(default_factory=Policy)
    evaluator: Evaluator = field(default_factory=Evaluator)
    risk_evaluator: RiskEvaluator = field(default_factory=RiskEvaluator)
    planner: Planner = field(default_factory=Planner)
    model_router: ModelRouter = field(default_factory=ModelRouter)
    assembler: RoutingPlanAssembler = field(default_factory=RoutingPlanAssembler)
    cost_wiring: CostEstimatorWiring = field(default_factory=CostEstimatorWiring)
    judge: ApprovalJudge = field(default_factory=ApprovalJudge)

    def __call__(self, task: Any, context: ProjectContext) -> RouterOutcome:
        """跑完决策主链并返回已经形成的产物。

        顺序与架构 4.1 的冻结主流程**逐节点一致**（`:1657-1668`）：

            [02] → [03] → [04] → [05] → Risk → [06] → [15a] → Approval Judgment

        ⚠️ **[05] Planner 在 Risk Assessment 之前** —— 不要照 4.2 的
        "Risk → Model → Cost → Approval" 反环图重排：4.2 只画了解环所需的那条子链
        （Planner 不在该环里，故未出现），它不是完整顺序。

        ⚠️ **`[03]` 在主链上**（5B.5.1）：`[02]` 之后调用一次
        ``capabilities.snapshot()``，**同一次 invocation 内只有一个能力视图** ——
        Evaluator、Planner 与 A5 的 ``max_concurrency`` 派生共用它。
        Router **不探测、不打时间戳、不调 ``recheck()``**：
        首次探测与缓存由 ``CapabilityRegistry`` 自己负责，
        重查是**事件驱动**的（6 类运行期事件），不属本决策链。
        """
        if not isinstance(context, ProjectContext):
            raise RouterError(f"context 必须是 ProjectContext，收到 {context!r}")

        # ---- ① 唯一事实入口 -------------------------------------------
        facts = self.facts_source(task, context)
        if not isinstance(facts, RouterFacts):
            raise RouterError(
                "facts_source 必须返回 RouterFacts；"
                f"收到 {facts!r}。缺事实时**显式失败**，Router 不补默认值"
            )

        # ---- ② [02] Policy：解算约束 + 聚合授权事实 --------------------
        policy_result = self.policy.resolve(facts.policy)

        # ---- ③ [03] Capability：取一次快照（缓存/首次探测由 registry 负责）
        #      ⚠️ 只调用一次；下面三处消费者共用这**同一个** snapshot。
        capability = self.capabilities.snapshot()

        # ---- ④ [04] Evaluator ----------------------------------------
        evaluation = self.evaluator.evaluate(
            facts.task, capability=capability, project=context
        )

        # ---- ⑤ [05] Planner（A3 seam 是**必需**输入） -----------------
        #      ⚠️ 在 Risk 之前（架构 4.1 主流程）；Planner 不需要 risk。
        plan = self.planner.plan(
            evaluation,
            facts.task,
            capability=capability,
            project=context,
            constraints=policy_result.planning,
        )

        # ---- ⑥ Risk Assessment ---------------------------------------
        risk_assessment = self.risk_evaluator.assess(facts.risk)

        # ---- ⑦ [06] Model Router：任务级决策 + A4 per-child 纯投影 -----
        selection = self.model_router.select(evaluation, risk_assessment, project=context)
        branch_models = self.model_router.assign_branch_models(plan, selection=selection)

        # ---- ⑧ RoutingPlan 装配（纯装配，不做决策） --------------------
        routing = self.assembler.assemble(
            plan, main_thread_model=selection, branch_models=branch_models
        )

        # ---- ⑨ [15a] 成本接线（A5：两处派生 + attempt 转交） -----------
        #      ⚠️ `max_concurrency` 用的是**同一个** `capability`（③）。
        costs = self.cost_wiring.estimate_all_units(
            routing,
            max_concurrency=_max_concurrency(routing, capability),
            attempt=FIRST_ATTEMPT,
            retry_allowance=_retry_allowance(FIRST_ATTEMPT),
        )

        # ---- ⑩ A8：disclosure **只能**经 build_disclosure() ------------
        disclosure = build_disclosure(
            plan=plan,
            evaluation=evaluation,
            risk_assessment=risk_assessment,
            recovery_method=facts.recovery_method,
        )

        # ---- ⑪ Approval Judgment -------------------------------------
        approval = self.judge.judge(
            risk_assessment,
            _main_thread_estimate(routing, costs),
            policy_result.authorization,
            routing,
            constraints=_approval_constraints(policy_result),
            disclosure=disclosure,
        )

        return RouterOutcome(
            plan=plan, routing=routing, costs=costs, approval=approval
        )


# --------------------------------------------------------------------------
# A5 的组合层派生 —— **只有这两处**由 Router 计算
# --------------------------------------------------------------------------


def _max_concurrency(routing: RoutingPlan, capability: CapabilitySnapshot) -> int:
    """A5.4 的三分支派生。

    ```text
    ceiling is None → unit_count                        （保守成本上界）
    ceiling > 0     → max(1, min(unit_count, ceiling))
    ceiling == 0    → 1                                 （成本结构下界，**非**能力口径）
    ```

    ⚠️ ``ceiling == 0`` 时的 ``1`` **不是**把 capability 从 0 悄悄改成 1 ——
    它是"至少有一个执行单位要算"这一**结构性事实**的下界；
    合并写成 ``max(1, min(...))`` 是同一语义的等价式。
    """
    unit_count = len(routing.execution_unit_ids)
    ceiling = capability.concurrency_limit
    if ceiling is None:
        return unit_count
    return max(1, min(unit_count, ceiling))


def _retry_allowance(attempt: int) -> int:
    """A5.5：``max(total_attempt_limit - attempt, 0)``。

    V1（``total_attempt_limit = 2``）⇒ attempt 1 → 1、attempt 2 → 0。
    """
    return max(TOTAL_ATTEMPT_LIMIT - attempt, 0)


def _main_thread_estimate(
    routing: RoutingPlan, costs: tuple[UnitCostEstimate, ...]
) -> CostEstimate:
    """5B.5.0 ②（C1）：Judgment 固定读取**主线程执行单位**的估算。

    ⚠️ **不做跨执行单位聚合** —— V1 里各单位 ``magnitude`` 实际相同
    （A4 同档位，且 ``_magnitude`` 已计入 MULTI_AGENT / branch_count /
    并发 / 重试余量），但"取主线程"是一条**显式规则**，不依赖那个巧合。
    将来 per-branch 真正分化时，应**另行定义**任务级成本聚合 seam，
    **不得**在这里用 ``max()`` 提前发明语义。
    """
    for unit in costs:
        if unit.branch_id == routing.main_thread_branch_id:
            return unit.estimate
    raise RouterError(
        f"成本接线结果里没有主线程执行单位 {routing.main_thread_branch_id!r} 的估算；"
        "Judgment 需要它作为本次任务的成本输入（5B.5.0 ② / C1）"
    )


def _approval_constraints(policy_result: PolicyResult) -> EffectiveApprovalConstraints:
    """取 [02] 的 approval-policy 投影（A6 seam；**只读**，不重算）。"""
    constraints = policy_result.approval
    if not isinstance(constraints, EffectiveApprovalConstraints):
        # Policy 保证产出该类型；走到这里说明装配被换成了不合规实现。
        raise RouterError(
            "PolicyResult.approval 必须是 EffectiveApprovalConstraints（A6 seam）"
        )
    return constraints
