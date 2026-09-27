"""cost.estimator —— [15a] Cost Estimator：执行前预测。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 [15a] Cost Estimator
  - 4.5 Cost Y 型流（[15a] 基数规则 V1 冻结）

⚠️ 本模块的边界：

    ✅ 读 [06] 已定的档位算钱
    ❌ 不获得模型选择权（INV-05）
    ❌ 不能自己触发审批门，只提供输入

⚠️ 基数（架构 4.5 V1 冻结）：**每分支的每个执行 attempt 最多一次**。

     Attempt 1        → 必须产生 CostEstimate
     后续 Attempt     → 成本驱动变化 → 重新估算
                      → 成本驱动未变化 → 复用上一 attempt 的估算，并保留来源

   这与 Ledger 按 attempt 记账对齐（每条记录带 run_id / branch_id / attempt）。

⚠️ L55：**永不产生数字区间**。任何无真实 quote / history evidence 的情况
   一律产出 ``basis=UNKNOWN`` + 无区间 + 仅定性 ``magnitude``。

⚠️ 本模块**不读 [03] CapabilitySnapshot**：
   ``reliable Token/费用数据`` 是环境能力，只决定未来有没有机会取得 evidence，
   **不是成本高低的因素**。capability bool 不得代替 evidence。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..contracts import (
    Confidence,
    CostBasis,
    CostEstimate,
    CostMagnitude,
    EffortLevel,
    ModelSelection,
    ModelTier,
)

__all__ = [
    "PlannedScope",
    "EstimationBasis",
    "ReusePolicy",
    "CostDriverSet",
    "EstimationOutcome",
    "CostEstimator",
    "DRIVER_FIELD_NAMES",
    "NON_DRIVER_FIELDS",
]


class PlannedScope(Enum):
    """计划形态对成本的影响。

    ⚠️ **与 ``Plan.mode``（``ExecutionMode``）不是同名同义**（5B.3 订正）：
    两者只有后两项同名，**0 分支那项不同名**：

        ExecutionMode.DIRECT  ↔  PlannedScope.MAIN_THREAD

    这条映射由 ``cost/wiring.py::SCOPE_BY_MODE`` **显式**承担，
    不要假定两边可以互换取值。
    """

    MAIN_THREAD = "main_thread"
    STAGED_SINGLE_AGENT = "staged_single_agent"
    MULTI_AGENT = "multi_agent"


class EstimationBasis(Enum):
    """估算依据。

    ``UNKNOWN`` 是**合法状态**，不是异常（架构 2 节 [15a]）。
    值与冻结契约 ``CostBasis`` 一一对应。
    """

    HEURISTIC = "heuristic"
    HISTORICAL = "historical"
    PROVIDER_QUOTE = "provider_quote"
    UNKNOWN = "unknown"


class ReusePolicy(Enum):
    """本次估算是新算的还是复用上一 attempt 的。"""

    FRESH = "fresh"
    REUSED = "reused"


#: 成本驱动 → 对量级的贡献权重。
_TIER_WEIGHT: dict[ModelTier, int] = {
    ModelTier.LOW_COST: 0,
    ModelTier.BALANCED: 1,
    ModelTier.STRONG: 2,
    ModelTier.MAXIMUM: 3,
}

_EFFORT_WEIGHT: dict[EffortLevel, int] = {
    EffortLevel.LOW: 0,
    EffortLevel.MEDIUM: 1,
    EffortLevel.HIGH: 2,
}


def _magnitude_from(score: int) -> CostMagnitude:
    if score <= 1:
        return CostMagnitude.LOW
    if score <= 3:
        return CostMagnitude.MEDIUM
    return CostMagnitude.HIGH


#: ``CostDriverSet`` 中**不参与成本驱动比较**的字段，及其理由。
#: 新增任何字段时，若不属于成本驱动，必须在此显式登记 —— 否则测试会失败。
NON_DRIVER_FIELDS: dict[str, str] = {
    "attempt": "标记第几次尝试，不改变成本口径；其推进由基数规则处理，不是驱动",
}


#: 参与成本驱动比较的字段名，**顺序与 ``CostDriverSet.drivers()`` 一致**。
#:
#: ⚠️ 这张表不是文档，是**断言目标**：
#:    ``drivers()`` 用"字段名拼值"的写法生成描述，测试会核对
#:    ``DRIVER_FIELD_NAMES`` 与 ``CostDriverSet.__dataclass_fields__``
#:    减去 ``NON_DRIVER_FIELDS`` 完全相等。
#:    也就是说 —— **新增一个成本驱动字段却忘了加进 drivers()，测试必然失败**，
#:    不会静默地把"驱动已变化"误判成"可复用上一 attempt 的估算"。
DRIVER_FIELD_NAMES: tuple[str, ...] = (
    "scope",
    "tier",
    "effort",
    "planned_branch_count",
    "max_concurrency",
    "retry_allowance",
)


@dataclass(frozen=True)
class CostDriverSet:
    """本次 attempt 的**成本驱动集合**。

    这就是"成本驱动是否变化"的比较对象：任一字段变化都要重新估算
    （模型 / 范围 / 并发 / 恢复余量）。

    ⚠️ 字段集合是封闭的 —— 它只描述**已经由 [04]/[05]/[06] 定下的事实**，
       不含任何"要不要审批"或"能不能便宜点"的判断。

    ⚠️ **不含 ``reliable_cost_data``。** 架构归属很清楚：

       ``reliable Token/费用数据`` 是 **[03] Capability Probe** 探测的**环境能力**；
       而冻结的 [15a] 输入是 ``ModelSelection + 分支规模 + 并发放大 + 恢复重试余量``，
       **没有 CapabilitySnapshot 这一项**。

       更重要的是：**"平台能够提供可靠成本数据"不是"预计成本更高/更低"的因素**
       —— 它只决定未来**有没有机会取得 evidence**。因此：

           CapabilitySnapshot.reliable_cost_data
                   ↓ 未来决定"是否有能力取得成本 evidence"

           真实 Provider Quote / Historical Evidence
                   ↓ **显式 evidence 输入**（将来新增）
           Cost Estimator → PROVIDER_QUOTE / HISTORICAL + range

       **不得让 capability bool 代替 evidence。**
    """

    scope: PlannedScope
    tier: ModelTier
    effort: EffortLevel
    planned_branch_count: int
    max_concurrency: int
    attempt: int
    retry_allowance: int

    def __post_init__(self) -> None:
        if not isinstance(self.scope, PlannedScope):
            raise TypeError(f"scope 必须是 PlannedScope，收到 {self.scope!r}")
        if not isinstance(self.tier, ModelTier):
            raise TypeError(f"tier 必须是 ModelTier，收到 {self.tier!r}")
        if not isinstance(self.effort, EffortLevel):
            raise TypeError(f"effort 必须是 EffortLevel，收到 {self.effort!r}")
        for name in (
            "planned_branch_count",
            "max_concurrency",
            "attempt",
            "retry_allowance",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{name} 必须是 int，收到 {value!r}")
            if value < 0:
                raise ValueError(f"{name} 不得为负")
        if self.attempt < 1:
            raise ValueError(f"attempt 从 1 开始计数，收到 {self.attempt}")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency 至少为 1")

    @classmethod
    def for_selection(
        cls,
        *,
        selection: ModelSelection,
        scope: PlannedScope,
        planned_branch_count: int,
        max_concurrency: int,
        attempt: int,
        retry_allowance: int,
    ) -> "CostDriverSet":
        """从 [06] 已定的 ``ModelSelection`` 构造驱动集合。

        ⚠️ 只**读**档位，不修改它 —— INV-05。
        """
        return cls(
            scope=scope,
            tier=selection.tier,
            effort=selection.effort,
            planned_branch_count=planned_branch_count,
            max_concurrency=max_concurrency,
            attempt=attempt,
            retry_allowance=retry_allowance,
        )

    def drivers(self) -> tuple[str, ...]:
        """可审计的驱动描述（进入 ``CostEstimate.drivers``）。

        ⚠️ **这是复用判定的唯一依据**（见 ``CostEstimator._same_drivers``），
           因此它必须编码**全部**成本驱动字段，顺序与
           ``DRIVER_FIELD_NAMES`` 一致。漏掉一个字段会让"驱动已变化"
           被静默误判为"可复用上一 attempt 的估算"，
           所以有测试机械核对字段覆盖。
        """
        return (
            f"scope={self.scope.value}",
            f"tier={self.tier.value}",
            f"effort={self.effort.value}",
            f"planned_branch_count={self.planned_branch_count}",
            f"max_concurrency={self.max_concurrency}",
            f"retry_allowance={self.retry_allowance}",
        )


@dataclass(frozen=True)
class EstimationOutcome:
    """估算结果 + 它是新算的还是复用的。"""

    estimate: CostEstimate
    reuse: ReusePolicy


class CostEstimator:
    """确定性估算器。**不选模型、不触发审批、不访问外部。**

    ⚠️ **永不产生数字区间**（第 3 步收口 ①）。

    本估算器**不读** [03] CapabilitySnapshot。架构归属：

        ``reliable Token/费用数据`` 是 [03] 探测的**环境能力**，
        它只决定未来**有没有机会取得 evidence**；
        **"平台能提供可靠成本数据"不是"预计成本更高/更低"的因素。**

    ``PROVIDER_QUOTE`` / ``HISTORICAL`` 需要**显式的 evidence 输入**
    （真实 provider quote / 已授权历史数据），将来才接入。
    **capability bool 不得代替 evidence。**
    在那之前，只有定性 ``magnitude`` 是诚实的。
    """

    def estimate(
        self, drivers: CostDriverSet, *, previous: CostEstimate | None = None
    ) -> EstimationOutcome:
        """按架构 4.5 的基数规则产出估算。

        ``previous`` 是上一 attempt 的估算；驱动未变化时复用它。
        """
        if not isinstance(drivers, CostDriverSet):
            raise TypeError(f"drivers 必须是 CostDriverSet，收到 {drivers!r}")
        if previous is not None and self._same_drivers(previous, drivers):
            return EstimationOutcome(estimate=previous, reuse=ReusePolicy.REUSED)
        return EstimationOutcome(
            estimate=self._derive(drivers), reuse=ReusePolicy.FRESH
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _same_drivers(previous: CostEstimate, drivers: CostDriverSet) -> bool:
        """成本驱动是否与上一 attempt 完全一致。

        比对上一条估算记录的驱动描述 —— 描述由 ``drivers()`` 生成，
        字段集合变化会自动反映为不一致，不会静默放过。
        """
        return tuple(previous.drivers) == drivers.drivers()

    def _derive(self, drivers: CostDriverSet) -> CostEstimate:
        """产出**纯定性**估算。

        可达的 ``basis`` 只有 ``UNKNOWN``：

            ``PROVIDER_QUOTE``  需要**真实 provider quote** → 尚无该输入
            ``HISTORICAL``      需要**已授权的历史数据** → 尚无该输入

        两者都需要 Adapter / 跨会话数据接入，且跨会话数据须显式 opt-in
        （INV-15）。在那之前，只有 ``magnitude`` 是诚实的。
        """
        return CostEstimate(
            basis=CostBasis.UNKNOWN,
            magnitude=self._magnitude(drivers),
            confidence=Confidence.LOW,
            drivers=drivers.drivers(),
        )

    @staticmethod
    def _magnitude(drivers: CostDriverSet) -> CostMagnitude:
        score = _TIER_WEIGHT[drivers.tier] + _EFFORT_WEIGHT[drivers.effort]
        if drivers.scope is PlannedScope.MULTI_AGENT or drivers.planned_branch_count >= 2:
            score += 1
        if drivers.max_concurrency >= 4:
            score += 1
        if drivers.retry_allowance >= 1:
            score += 1
        return _magnitude_from(score)
