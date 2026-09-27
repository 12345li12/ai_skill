"""contracts.decision —— 决策类契约（不可变）。

架构依据：`docs/architecture.md` 第 11.1 节（decision.py 归属）
依赖方向：仅依赖 ``contracts.common``。

本模块内容（架构冻结清单）：
  Evaluation          11 维评估（L23），**只有维度 + reason**
  ExecutionMode       三种执行方式（L25），只出现在 Plan
  BranchSpec          分支定义（goal / read_only / completion_criteria）
  Plan                执行方式 + 分支 + 主线职责（[05] Planner 的输出）
  ModelSelection      抽象档位 + 推理强度（[06] Model Router 的输出）
  RoutingPlan         执行计划（**不可变**，Adapter 不得改：INV-03）
  RiskLevel / RiskCategory
  RiskAssessment      操作/业务风险，**约束**模型选择
  ApprovalJudgment    系统决策：是否需要审批
  FailureClass        9 类失败（L49）
  RecoveryTarget      回流目标
  RecoveryDecision    Recovery 的唯一输出（它不自己动手）

生命周期（架构 4.2 冻结，本模块必须原样表达）：

    Evaluation          [04]  只有 11 维事实
       ↓
    Plan / BranchSpec   [05]  决定执行方式与分支，**不选模型**
       ↓
    RiskAssessment      [07]  只回答"风险有多大"
       ↓
    ModelSelection      [06]  抽象档位，**不含平台 model ID**
       ↓
    RoutingPlan         [06]  交给 Adapter，不可变
       ↓
    BranchReturn        [12]  此时才同时拥有 planned/requested/actual

⚠️ 本模块的边界（三处职责越界已按第 2 步审查删除）：
   - ``Evaluation`` **不含 mode** —— 执行方式是 [05] Planner 的职责。
   - ``RiskAssessment`` **不含审批流程控制** —— 是否暂停是 Approval Judgment 的职责，
     且 Judgment 在 Cost Estimator 之后才能综合判断。
   - ``ModelSelection`` **不含 provider model ID / requested / actual** ——
     平台模型由 [11] Adapter 翻译，Core 禁止出现平台细节。
     三值确认（planned/requested/actual）在 [12] ``BranchReturn`` 中表达。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .common import (
    BranchId,
    Confidence,
    ContractError,
    EffortLevel,
    ModelTier,
    RunId,
    validate_branch_id,
    validate_run_id,
)

__all__ = [
    "Ordinal",
    "ImpactScope",
    "ExecutionMode",
    "Evaluation",
    "BranchSpec",
    "Plan",
    "RoutingPlan",
    "ModelTier",
    "EffortLevel",
    "ModelSelection",
    "RiskLevel",
    "RiskCategory",
    "RiskAssessment",
    "ApprovalJudgment",
    "FailureClass",
    "RecoveryTarget",
    "RecoveryDecision",
]

# ModelTier / EffortLevel 定义在 contracts.common（被 runtime 共享，避免横向依赖），
# 这里 re-export 是为了保持 decision 的公开 API 稳定。


def _require_enum(value: Any, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise ContractError(f"{field_name} 必须是 {expected.__name__}，收到 {value!r}")


def _require_non_empty_str(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field_name} 必须是非空字符串，收到 {value!r}")


# --------------------------------------------------------------------------
# 评估维度
# --------------------------------------------------------------------------


class Ordinal(Enum):
    """三级序数。用于**量级型**评估维度。

    刻意不用浮点数：L55 禁止虚构精确度，评估阶段给 0.73 这种分数
    是伪精确。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ImpactScope(Enum):
    """影响范围（L23）。不是量级，是作用域，故单列。"""

    LOCAL = "local"
    CROSS_MODULE = "cross_module"
    SYSTEM_WIDE = "system_wide"


class ExecutionMode(Enum):
    """三种执行方式（L25）。"""

    DIRECT = "direct"
    STAGED_SINGLE_AGENT = "staged_single_agent"
    MULTI_AGENT = "multi_agent"


@dataclass(frozen=True)
class Evaluation:
    """低成本评估的 11 个维度（L21 / L23）。

    维度清单（**不得增删**，与 protocol L23 一一对应）：
      1. scale                规模
      2. complexity           难度
      3. risk                 风险
      4. impact_scope         影响范围
      5. independence         独立性
      6. parallel_benefit     并行收益
      7. context_volume       上下文量
      8. scheduling_cost      调度成本
      9. rework_probability   返工概率
     10. review_value         复核价值
     11. concurrency_capacity 并发容量   ← 来自 [03] 快照，[04] 不得自行探测

    第 2 步审查裁决：**不含 ``mode``**。
    执行方式（DIRECT / STAGED_SINGLE_AGENT / MULTI_AGENT）是
    ``[05] Planner`` 的职责，表达在 ``Plan.mode``。
    若 ``Evaluation`` 也存一份，Planner 就只剩"重复确认"的份，决策职责被架空。
    """

    scale: Ordinal
    complexity: Ordinal
    risk: Ordinal
    impact_scope: ImpactScope
    independence: Ordinal
    parallel_benefit: Ordinal
    context_volume: Ordinal
    scheduling_cost: Ordinal
    rework_probability: Ordinal
    review_value: Ordinal
    concurrency_capacity: Ordinal
    reason: str

    def __post_init__(self) -> None:
        for name in (
            "scale",
            "complexity",
            "risk",
            "independence",
            "parallel_benefit",
            "context_volume",
            "scheduling_cost",
            "rework_probability",
            "review_value",
            "concurrency_capacity",
        ):
            _require_enum(getattr(self, name), Ordinal, name)
        _require_enum(self.impact_scope, ImpactScope, "impact_scope")
        _require_non_empty_str(self.reason, "reason")

    @property
    def dimensions(self) -> tuple[Any, ...]:
        """11 维，顺序固定，便于契约测试与追溯。"""
        return (
            self.scale,
            self.complexity,
            self.risk,
            self.impact_scope,
            self.independence,
            self.parallel_benefit,
            self.context_volume,
            self.scheduling_cost,
            self.rework_probability,
            self.review_value,
            self.concurrency_capacity,
        )


# --------------------------------------------------------------------------
# 计划
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchSpec:
    """一个分支的定义（[05] Planner 产出）。

    ⚠️ 第 2 步审查裁决：**不含模型档位与推理强度**。
       ``[05] Planner`` 在 ``[06] Model Router`` **之前**运行，
       由 Planner 提前选模型会使 Model Router 失去决策职责。
       模型选择在 [06] 之后进入 ``RoutingPlan.branch_models``。

    保留下来的三项是 Planner 在自己的时点上真正能决定的：
    ``goal`` / ``read_only`` / ``completion_criteria``。
    """

    branch_id: BranchId
    goal: str
    read_only: bool
    completion_criteria: str

    def __post_init__(self) -> None:
        validate_branch_id(self.branch_id)
        _require_non_empty_str(self.goal, "goal")
        if not isinstance(self.read_only, bool):
            raise ContractError(f"read_only 必须是 bool，收到 {self.read_only!r}")
        _require_non_empty_str(self.completion_criteria, "completion_criteria")


@dataclass(frozen=True)
class Plan:
    """执行计划（[05] Planner 的输出）。

    ``mode`` 决定 ``branches`` 的合法性：
      DIRECT               → 0 分支，且不得展示形式化计划（L25）
      STAGED_SINGLE_AGENT  → 0 分支（阶段化，非真实分流）
      MULTI_AGENT          → ≥2 分支，每条必须有完成标准
    """

    mode: ExecutionMode
    main_thread_duty: str
    reason: str
    branches: tuple[BranchSpec, ...] = ()

    def __post_init__(self) -> None:
        _require_enum(self.mode, ExecutionMode, "mode")
        _require_non_empty_str(self.main_thread_duty, "main_thread_duty")
        _require_non_empty_str(self.reason, "reason")
        if not isinstance(self.branches, tuple):
            raise ContractError("branches 必须是 tuple（不可变契约）")
        for spec in self.branches:
            if not isinstance(spec, BranchSpec):
                raise ContractError(f"branches 元素必须是 BranchSpec，收到 {spec!r}")

        if self.mode is ExecutionMode.MULTI_AGENT:
            if len(self.branches) < 2:
                raise ContractError(
                    "MULTI_AGENT 至少需要 2 个独立且完成标准明确的子任务（L25）"
                )
            ids = [spec.branch_id for spec in self.branches]
            if len(set(ids)) != len(ids):
                raise ContractError("分支 branch_id 不得重复")
        elif self.branches:
            raise ContractError(
                f"{self.mode.value} 不得携带分支（只有真实多代理分流才建分支）"
            )

    @property
    def is_multi_agent(self) -> bool:
        return self.mode is ExecutionMode.MULTI_AGENT


@dataclass(frozen=True)
class ModelSelection:
    """模型与推理强度选择 —— **[06] Model Router 的输出**。

    第 2 步审查裁决（阻塞问题 A）：**只有抽象计划，没有平台细节**。

      ✅ tier   抽象档位（ModelTier）
      ✅ effort 推理强度（EffortLevel）
      ✅ reason 选择理由
      ❌ provider model ID      —— 由 [11] Adapter 翻译，Core 禁止出现平台细节
      ❌ requested / actual     —— 生命周期上此时尚不存在

    生命周期（架构 4.2）：
      planned   → 本契约（[06]）
      requested → [11] Adapter 发请求时
      actual    → 执行后由平台回报

    三值确认（``ExecutionValue[ModelTier, str, str]``）在 ``[12] BranchReturn``
    中表达 —— 那时才同时拥有 planned / requested / actual。
    """

    tier: ModelTier
    effort: EffortLevel
    reason: str

    def __post_init__(self) -> None:
        _require_enum(self.tier, ModelTier, "tier")
        _require_enum(self.effort, EffortLevel, "effort")
        _require_non_empty_str(self.reason, "reason")


# --------------------------------------------------------------------------
# 风险
# --------------------------------------------------------------------------


class RiskLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskCategory(Enum):
    """L41 / L43 列举的高风险场景类别。

    ⚠️ 第 2 步审查裁决：**不含 SIGNIFICANT_ADDITIONAL_COST**。
       Risk Assessment 在 Cost Estimator **之前**运行，此时还不知道真正的
       成本估算。"显著额外费用"不能伪装成前置 risk fact ——
       它应由 ``[15a] CostEstimate → ApprovalJudgment`` 判断。

    枚举值与 protocol L41/L43 的场景一一对应，**category 属于哪个节点**
    见 ``tests/contracts/README.md`` 的 TEST-PLAN/CAP 映射注记。
    """

    BROAD_HARD_TO_REVIEW_CHANGE = "broad_hard_to_review_change"
    HARD_TO_REVERT_OPERATION = "hard_to_revert_operation"
    PRODUCTION_OR_REAL_USERS = "production_or_real_users"
    SHARED_INFRASTRUCTURE = "shared_infrastructure"
    RELEASE_DEPLOY_MERGE_PAYMENT = "release_deploy_merge_payment"
    OUTBOUND_MESSAGE = "outbound_message"
    EXTERNAL_WRITE = "external_write"
    CREDENTIALS_PRIVACY_PERMISSIONS = "credentials_privacy_permissions"
    MAJOR_CONCLUSION_CONFLICT = "major_conclusion_conflict"
    SCOPE_EXPANSION = "scope_expansion"


@dataclass(frozen=True)
class RiskAssessment:
    """操作/业务风险（[07] Risk Assessment 输出）。

    第 2 步审查裁决（原裁决 ④）：**职责非常窄 —— 只回答"风险有多大"，
    不回答"是否暂停"。**

    ⚠️ 因此本契约**不含任何审批流程控制字段**（如 requires_approval_consideration）。
       理由有二：
         1. 即使 Risk 是 LOW，后面的 Cost Estimate 仍可能发现"显著额外费用"，
            所以 Approval Judgment **本来就必须运行**；
         2. 一个"是否需要考虑审批"的 bool 极易被写成
            ``if not risk.requires_approval_consideration: skip_approval_judgment()``
            —— 那会重新制造审批旁路。

       是否暂停由 Approval Judgment 在
       Risk + Cost + Authorization + 副作用 齐备后统一判断（架构 4.2）。
    """

    level: RiskLevel
    categories: tuple[RiskCategory, ...]
    detail: str

    def __post_init__(self) -> None:
        _require_enum(self.level, RiskLevel, "level")
        if not isinstance(self.categories, tuple):
            raise ContractError("categories 必须是 tuple")
        for category in self.categories:
            if not isinstance(category, RiskCategory):
                raise ContractError(
                    f"categories 元素必须是 RiskCategory，收到 {category!r}"
                )
        _require_non_empty_str(self.detail, "detail")

        if self.categories and self.level is RiskLevel.LOW:
            raise ContractError(
                "命中高风险类别时 level 不得为 LOW（类别本身就是风险证据）"
            )


# --------------------------------------------------------------------------
# 审批（系统决策部分；人类交互边界见 runtime）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalJudgment:
    """系统决策：**是否需要审批**（架构 4.2）。

    ⚠️ 它消费 ``AuthorizationContext``（事实），**自己不聚合权限**。
    ⚠️ 它不是 Gate。Gate 是"暂停 → 交还控制权 → 等待人类输入"的阻塞边界。

    审批清单**仅此四项**（L43），多一个都不许：
      拟执行动作 / 影响范围 / 主要风险 / 恢复方式
    """

    needs_approval: bool
    proposed_action: str
    impact_scope: str
    main_risk: str
    recovery_method: str

    def __post_init__(self) -> None:
        if not isinstance(self.needs_approval, bool):
            raise ContractError("needs_approval 必须是 bool")
        for name in (
            "proposed_action",
            "impact_scope",
            "main_risk",
            "recovery_method",
        ):
            _require_non_empty_str(getattr(self, name), name)

    @property
    def checklist(self) -> tuple[str, str, str, str]:
        """审批清单四项，顺序固定（L43）。"""
        return (
            self.proposed_action,
            self.impact_scope,
            self.main_risk,
            self.recovery_method,
        )


# --------------------------------------------------------------------------
# 恢复
# --------------------------------------------------------------------------


class FailureClass(Enum):
    """L49 的 9 类失败。"""

    INPUT = "input"
    PERMISSION = "permission"
    TOOL = "tool"
    MODEL = "model"
    EFFORT = "effort"
    DECOMPOSITION = "decomposition"
    DEPENDENCY = "dependency"
    EXTERNAL_SERVICE = "external_service"
    COMPLETION_CRITERIA = "completion_criteria"


class RecoveryTarget(Enum):
    """回流目标（架构 4.4）。"""

    CAPABILITY = "capability"
    PLANNER = "planner"
    MODEL_ROUTER = "model_router"
    CONTEXT = "context"
    SCHEDULER = "scheduler"


@dataclass(frozen=True)
class RecoveryDecision:
    """[14] Recovery Router 的唯一输出。

    ⚠️ 它自己**不能**偷偷换模型 / 扩大范围 / 重试 / 增加 Agent。
       它只能产生决策，由对应节点重新做正式决策（架构第 2 节 [14]）。

    ``attempt`` 语义（架构 9.1）：
      Attempt 1 = 初次执行；Attempt 2 = 唯一一次自动恢复机会。
      ``attempt >= total_attempt_limit`` → 强制 TERMINAL_FAILURE，禁止静默继续。
    """

    failure_class: FailureClass
    route_to: RecoveryTarget
    attempt: int
    total_attempt_limit: int
    carry_forward: tuple[str, ...]
    minimal_correction: str
    confidence: Confidence

    def __post_init__(self) -> None:
        _require_enum(self.failure_class, FailureClass, "failure_class")
        _require_enum(self.route_to, RecoveryTarget, "route_to")
        _require_enum(self.confidence, Confidence, "confidence")
        _require_non_empty_str(self.minimal_correction, "minimal_correction")

        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise ContractError(f"attempt 必须是 int，收到 {self.attempt!r}")
        if not isinstance(self.total_attempt_limit, int) or isinstance(
            self.total_attempt_limit, bool
        ):
            raise ContractError(
                f"total_attempt_limit 必须是 int，收到 {self.total_attempt_limit!r}"
            )
        if self.attempt < 1:
            raise ContractError("attempt 从 1 开始计数")
        if self.total_attempt_limit < 1:
            raise ContractError("total_attempt_limit 至少为 1")
        if self.attempt >= self.total_attempt_limit:
            raise ContractError(
                f"attempt={self.attempt} 已达上限 {self.total_attempt_limit}，"
                "必须走 TERMINAL_FAILURE，不得产生回流决策（架构 4.4）"
            )
        if not isinstance(self.carry_forward, tuple):
            raise ContractError("carry_forward 必须是 tuple")
        for item in self.carry_forward:
            _require_non_empty_str(item, "carry_forward 元素")


# --------------------------------------------------------------------------
# 路由计划（不可变，交给 Adapter）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingPlan:
    """交给 [11] Runtime Adapter 的执行计划。

    这是第 2 步审查所指的那条生命周期的**汇合点**：

        Plan / BranchSpec   [05]  分支形状（目标 / 只读 / 完成标准）
        ModelSelection      [06]  抽象档位
        ─────────────────────────────── 在本契约汇合
        RoutingPlan         [06]  不可变，交 Adapter
        BranchReturn        [12]  此时才有 planned / requested / actual

    ⚠️ **不可变**。INV-03：Adapter 只能翻译和执行，不得自行改变 RoutingPlan。
       能力不足时 Adapter 必须返回 ``CapabilityMismatch``，由 Core 重新决策。

    ⚠️ **只有抽象档位，没有平台 model ID。** 平台模型由 [11] Adapter 翻译；
       Core 不得出现平台细节。

    ⚠️ **``main_thread_branch_id`` 是主线程的执行单位身份**
       （第 2 步审查阻塞 C）。

       主线程本身也是执行主体，会产生费用：

         DIRECT               → ``Plan.branches == ()``，主线程是**唯一**执行单位
         STAGED_SINGLE_AGENT  → 同上
         MULTI_AGENT          → 主线程仍有自己的单位，每个 child branch 另有一个

       若不显式给出，DIRECT 情况下 Cost Ledger 的
       ``(branch_id, attempt)`` 就没有合法取值，而 ``branch_id``
       **不应该**改成 Optional —— 那会破坏已冻结的
       ``run_id / branch_id / attempt`` 记账语义。

       采用**显式字段**而非全局魔法常量，是为了让"主线程单位"可被
       Ledger、ValidationRecord、BranchReturn 一致引用，而不是散落在各处的字面量。
    """

    run_id: RunId
    plan: Plan
    main_thread_branch_id: BranchId
    main_thread_model: "ModelSelection"
    branch_models: tuple[tuple[BranchId, "ModelSelection"], ...] = ()

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if not isinstance(self.plan, Plan):
            raise ContractError("plan 必须是 Plan")
        validate_branch_id(self.main_thread_branch_id)
        if not isinstance(self.main_thread_model, ModelSelection):
            raise ContractError("main_thread_model 必须是 ModelSelection")
        if not isinstance(self.branch_models, tuple):
            raise ContractError("branch_models 必须是 tuple（不可变契约）")

        seen: list[BranchId] = []
        for pair in self.branch_models:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ContractError(
                    f"branch_models 元素必须是 (BranchId, ModelSelection)，收到 {pair!r}"
                )
            branch_id, selection = pair
            validate_branch_id(branch_id)
            if not isinstance(selection, ModelSelection):
                raise ContractError("branch_models 第二项必须是 ModelSelection")
            seen.append(branch_id)

        if len(set(seen)) != len(seen):
            raise ContractError("branch_models 中 branch_id 不得重复")

        planned = {spec.branch_id for spec in self.plan.branches}

        # 主线程单位不得与任何 child branch 冲突
        if self.main_thread_branch_id in planned:
            raise ContractError(
                f"main_thread_branch_id {self.main_thread_branch_id!r} "
                "不得被 child branch 占用（执行单位身份必须唯一）"
            )

        extra = set(seen) - planned
        if extra:
            raise ContractError(
                f"branch_models 出现了 Plan 未定义的分支 {sorted(extra)}"
                "（档位不得由 [06] 凭空发明分支）"
            )
        missing = planned - set(seen)
        if missing:
            raise ContractError(
                f"分支 {sorted(missing)} 缺少档位选择"
                "（每个分支都必须在 [06] 之后获得档位）"
            )

    @property
    def branch_ids(self) -> tuple[BranchId, ...]:
        """child branch 标识（**不含主线程**）。"""
        return tuple(spec.branch_id for spec in self.plan.branches)

    @property
    def execution_unit_ids(self) -> tuple[BranchId, ...]:
        """全部 coverage unit 身份：主线程 + 各 child branch。

        Cost Ledger 的 ``branch_id`` 取值域就是这里。
        DIRECT / STAGED 模式下长度为 1（只有主线程）。
        """
        return (self.main_thread_branch_id, *self.branch_ids)

    def model_for_branch(self, branch_id: BranchId) -> "ModelSelection":
        if branch_id == self.main_thread_branch_id:
            return self.main_thread_model
        for candidate, selection in self.branch_models:
            if candidate == branch_id:
                return selection
        raise ContractError(f"未知执行单位 {branch_id!r}")
