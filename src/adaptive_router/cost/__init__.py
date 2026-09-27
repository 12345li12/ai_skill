"""cost —— [15a] / [15b] / [15c] 行为实现包。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 [15a] Cost Estimator / [15b] Cost Ledger / [15c] Cost Summary
  - 4.5 Cost Y 型流
  - 9.2 Coverage 规则

⚠️ 三种时间性质不同，因此拆成三个模块（架构 4.5）：

     estimator.py —— 严格**前置于**副作用      真阶段
     ledger.py    —— **贯穿**执行期            横切观察者，不是阶段
     summary.py   —— 严格**后置**              汇聚点（sink）

⚠️ ``wiring.py``（Step 5B.3 新增）不是第四种时间性质 —— 它只是
   ``[15a]`` 的**输入接线**：把已装配的 ``RoutingPlan`` 逐执行单位映射成
   ``CostDriverSet``。它不做决策、不记账、不改档位（INV-05）。

⚠️ 本包不得把行为代码写回 ``contracts/cost.py`` —— 那是 V1 冻结接口。
"""

from __future__ import annotations

from .estimator import (
    DRIVER_FIELD_NAMES,
    NON_DRIVER_FIELDS,
    CostDriverSet,
    CostEstimator,
    EstimationBasis,
    EstimationOutcome,
    PlannedScope,
    ReusePolicy,
)
from .ledger import CostLedger, TelemetryObservation
from .summary import CostSummaryBuilder, SummaryNotReady
from .wiring import CostEstimatorWiring, CostWiringError, UnitCostEstimate

__all__ = [
    # estimator
    "CostDriverSet",
    "CostEstimator",
    "EstimationBasis",
    "EstimationOutcome",
    "PlannedScope",
    "ReusePolicy",
    "DRIVER_FIELD_NAMES",
    "NON_DRIVER_FIELDS",
    # wiring（[15a] 接线：RoutingPlan → per-unit 驱动）
    "CostEstimatorWiring",
    "CostWiringError",
    "UnitCostEstimate",
    # ledger
    "CostLedger",
    "TelemetryObservation",
    # summary
    "CostSummaryBuilder",
    "SummaryNotReady",
]
