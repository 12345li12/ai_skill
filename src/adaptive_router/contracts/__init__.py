"""契约包 —— 系统内部允许用什么数据表达"事实"与"决策"。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 第 6 节   数据契约总览
  - 第 7 节   Architecture Invariants
  - 第 11 节  契约包结构

依赖方向（架构 11.2，固定）：

        common
       ↑   ↑   ↑
       │   │   │
 decision  runtime  cost
       ↑   ↑   ↑
       └───┴───┴──  models (facade)

``common`` 是唯一被依赖基座；``decision`` / ``runtime`` / ``cost``
**互不依赖**，避免循环。

纪律（第 2 步）：
  本包**只定义数据结构与构造期不变量**，不实现任何行为逻辑。
  ``__post_init__`` 中只允许出现"阻止非法状态被构造出来"的检查，
  不得出现路由、估算、调度、重试等业务判断。
"""

from __future__ import annotations

# common —— 通用枚举 / 不可变基础类型
from .common import (
    AgentId,
    BranchId,
    BranchTerminal,
    Confidence,
    ConfirmationSource,
    ContractError,
    ExecutionValue,
    RunId,
    RunTerminal,
    TerminalState,
    new_agent_id,
    new_branch_id,
    new_run_id,
)

# cost —— [15a] / [15b] / [15c]
from .cost import (
    COVERAGE_QUALITATIVE_THRESHOLD,
    CostActual,
    CostBasis,
    CostEstimate,
    CostMagnitude,
    CostRequest,
    CostSummary,
    CostUnitSummary,
    LedgerEntry,
    LedgerStage,
)

# decision —— 决策类契约（不可变）
from .decision import (
    ApprovalJudgment,
    BranchSpec,
    EffortLevel,
    Evaluation,
    ExecutionMode,
    FailureClass,
    ImpactScope,
    ModelSelection,
    ModelTier,
    Ordinal,
    Plan,
    RecoveryDecision,
    RecoveryTarget,
    RiskAssessment,
    RiskCategory,
    RiskLevel,
    RoutingPlan,
)

# runtime —— 运行期事实契约
from .runtime import (
    BRANCH_RETURN_FIELDS,
    AgentRun,
    AuthorizationContext,
    BranchReturn,
    BranchState,
    CapabilityMismatch,
    CapabilitySnapshot,
    ContextBundle,
    ProjectContext,
    RoutingRun,
    SessionGateResult,
    UnavailableReason,
    ValidationRecord,
)

__all__ = [
    # common
    "AgentId",
    "BranchId",
    "BranchTerminal",
    "Confidence",
    "ConfirmationSource",
    "ContractError",
    "ExecutionValue",
    "RunId",
    "RunTerminal",
    "TerminalState",
    "new_agent_id",
    "new_branch_id",
    "new_run_id",
    # decision
    "ApprovalJudgment",
    "BranchSpec",
    "EffortLevel",
    "Evaluation",
    "ExecutionMode",
    "FailureClass",
    "ImpactScope",
    "ModelSelection",
    "ModelTier",
    "Ordinal",
    "Plan",
    "RecoveryDecision",
    "RecoveryTarget",
    "RiskAssessment",
    "RiskCategory",
    "RiskLevel",
    "RoutingPlan",
    # runtime
    "BRANCH_RETURN_FIELDS",
    "AgentRun",
    "AuthorizationContext",
    "BranchReturn",
    "BranchState",
    "CapabilityMismatch",
    "CapabilitySnapshot",
    "ContextBundle",
    "ProjectContext",
    "RoutingRun",
    "SessionGateResult",
    "UnavailableReason",
    "ValidationRecord",
    # cost
    "COVERAGE_QUALITATIVE_THRESHOLD",
    "CostActual",
    "CostBasis",
    "CostEstimate",
    "CostMagnitude",
    "CostRequest",
    "CostSummary",
    "CostUnitSummary",
    "LedgerEntry",
    "LedgerStage",
]
