"""``[15a] Cost Estimator`` 的**接线**：``RoutingPlan`` → per-unit / per-attempt 输入。

架构依据：`docs/architecture.md`

- 2 节 ``[15a] Cost Estimator``：输入 = ``ModelSelection``（已定档位）、分支规模、
  并发放大系数、恢复重试余量；输出 = ``CostEstimate``。
- 4.5 Cost Y 型流：``[15a]`` 是**真阶段**，严格前置于本次 attempt 的新增副作用。
- **INV-05**：``[15a]`` **只读** ``[06]`` 的档位，**不得反向改档位**。
- 基数（V1 冻结）：**每分支的每个执行 attempt 最多一次**。

**本模块只做映射与透传，不做任何决策。**

它把**已经形成**的 ``RoutingPlan`` 逐执行单位（主线程 + 各 child）
转成 ``CostEstimator`` 需要的 ``CostDriverSet``，并如实透出 ``EstimationOutcome``。

✅ 允许：读 ``routing.model_for_branch(unit_id)`` 的 ``tier`` / ``effort``，
        映射 ``Plan.mode → PlannedScope``，调用 ``CostEstimator.estimate()``。
❌ 禁止：构造/修改 ``ModelSelection``；import ``model_router``；
        自行比较成本驱动；自行决定 ``ReusePolicy``。

**为什么三个运行期事实必须显式传入：**

``CostDriverSet`` 有 7 个字段，其中只有 4 个能从 ``RoutingPlan`` 得到：

    tier / effort             ← routing.model_for_branch(unit_id)
    planned_branch_count      ← len(routing.branch_ids)
    scope                     ← routing.plan.mode（需显式映射表，见下）
    ─────────────────────────────────────────────────────────────
    max_concurrency           ← **环境 / 运行期事实**，本模块不生产
    attempt                   ← **运行期事实**（每分支每 attempt 最多一次）
    retry_allowance           ← **运行期事实**（恢复重试余量）

架构 ``[15a]`` 只列出"并发放大系数 / 恢复重试余量"是输入，
**全篇没有指定生产者**。因此这里把它们定为**显式必需入参**：
本模块**不猜**、**不从** ``CapabilitySnapshot.concurrency_limit`` 反推
（estimator 自己不读 CapabilitySnapshot，接线层同样不替它编一个）。

**``reuse`` 的归属：** ``previous`` 是**可选**的 per-unit 映射；
给了就按单位显式传给 ``estimate()``，没给就传 ``None`` ——
``FRESH`` / ``REUSED`` 由 ``CostEstimator`` 自己判定。
本模块**不比较驱动、不决定复用**。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ..contracts import (
    BranchId,
    CostEstimate,
    ExecutionMode,
    RoutingPlan,
)
from .estimator import CostDriverSet, CostEstimator, EstimationOutcome, PlannedScope

__all__ = ["CostWiringError", "UnitCostEstimate", "CostEstimatorWiring"]


class CostWiringError(ValueError):
    """接线输入不足以产出合法的成本驱动集合。

    宁可显式失败，也不要替调用方猜一个并发/重试余量 ——
    猜出来的成本驱动会让"这次估算为什么是这个量级"变成不可审计的假事实。
    """


#: ``Plan.mode`` → ``PlannedScope`` 的**显式**映射。
#:
#: ⚠️ 两者**并非同名同义**（`cost/estimator.py` 的 ``PlannedScope`` docstring
#: 曾称"取值与 ``Plan.mode`` 同名同义"，那是错的）：``Plan`` 侧 0 分支的取值是
#: ``DIRECT``，而 ``PlannedScope`` 侧对应的是 ``MAIN_THREAD``。
#: 本表是这条 seam 的**唯一**映射点。
SCOPE_BY_MODE: dict[ExecutionMode, PlannedScope] = {
    ExecutionMode.DIRECT: PlannedScope.MAIN_THREAD,
    ExecutionMode.STAGED_SINGLE_AGENT: PlannedScope.STAGED_SINGLE_AGENT,
    ExecutionMode.MULTI_AGENT: PlannedScope.MULTI_AGENT,
}


@dataclass(frozen=True)
class UnitCostEstimate:
    """**一个执行单位**在某次 attempt 的估算结果（含驱动，便于审计）。"""

    branch_id: BranchId
    is_main_thread: bool
    drivers: CostDriverSet
    outcome: EstimationOutcome

    @property
    def estimate(self) -> CostEstimate:
        return self.outcome.estimate


@dataclass
class CostEstimatorWiring:
    """把 ``RoutingPlan`` 接到 ``CostEstimator`` 上。**不做决策、不记账。**"""

    estimator: CostEstimator = field(default_factory=CostEstimator)

    def estimate_for_unit(
        self,
        routing: RoutingPlan,
        branch_id: BranchId,
        *,
        max_concurrency: int,
        attempt: int,
        retry_allowance: int,
        previous: CostEstimate | None = None,
    ) -> UnitCostEstimate:
        """估算**单个**执行单位的本次 attempt。

        需要给不同单位不同的 ``attempt``（例如只有某条分支在重试）时，
        逐个调用本方法 —— 那正是"每分支的每个 attempt 最多一次"的字面含义。
        """
        if not isinstance(routing, RoutingPlan):
            raise CostWiringError(f"routing 必须是 RoutingPlan，收到 {routing!r}")
        if branch_id not in routing.execution_unit_ids:
            raise CostWiringError(
                f"未知执行单位 {branch_id!r}；"
                f"合法取值域是 {list(routing.execution_unit_ids)}"
            )

        # ⚠️ **只读**档位（INV-05）：从已装配的 RoutingPlan 取，绝不构造/修改它。
        selection = routing.model_for_branch(branch_id)
        drivers = CostDriverSet.for_selection(
            selection=selection,
            scope=self._scope_for(routing),
            planned_branch_count=len(routing.branch_ids),
            max_concurrency=max_concurrency,
            attempt=attempt,
            retry_allowance=retry_allowance,
        )
        return UnitCostEstimate(
            branch_id=branch_id,
            is_main_thread=branch_id == routing.main_thread_branch_id,
            drivers=drivers,
            # 复用与否**由 estimator 判定**，本模块只透传 previous。
            outcome=self.estimator.estimate(drivers, previous=previous),
        )

    def estimate_all_units(
        self,
        routing: RoutingPlan,
        *,
        max_concurrency: int,
        attempt: int,
        retry_allowance: int,
        previous: Mapping[BranchId, CostEstimate] | None = None,
    ) -> tuple[UnitCostEstimate, ...]:
        """估算**全部**执行单位（主线程 + 各 child），顺序同 ``execution_unit_ids``。

        ``attempt`` 对全部单位相同 —— 需要逐单位不同 attempt 时用
        :meth:`estimate_for_unit`。``previous`` 是可选的 per-unit 上一 attempt 估算。
        """
        if not isinstance(routing, RoutingPlan):
            raise CostWiringError(f"routing 必须是 RoutingPlan，收到 {routing!r}")
        if previous is not None and not isinstance(previous, Mapping):
            raise CostWiringError(
                f"previous 必须是 {{branch_id: CostEstimate}} 映射或 None，"
                f"收到 {previous!r}"
            )
        return tuple(
            self.estimate_for_unit(
                routing,
                branch_id,
                max_concurrency=max_concurrency,
                attempt=attempt,
                retry_allowance=retry_allowance,
                previous=None if previous is None else previous.get(branch_id),
            )
            for branch_id in routing.execution_unit_ids
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _scope_for(routing: RoutingPlan) -> PlannedScope:
        """``Plan.mode`` → ``PlannedScope``；映射表必须**全覆盖**（有测试锁）。"""
        mode = routing.plan.mode
        try:
            return SCOPE_BY_MODE[mode]
        except KeyError:  # pragma: no cover - ExecutionMode 新增取值时才会命中
            raise CostWiringError(
                f"ExecutionMode.{mode.name} 没有对应的 PlannedScope 映射；"
                "新增执行方式时必须显式扩展 SCOPE_BY_MODE"
            ) from None
