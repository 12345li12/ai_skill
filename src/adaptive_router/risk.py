"""risk —— Risk Assessment：只回答"风险有多大"。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 Risk Assessment / Approval Judgment 的职责切分
  - 4.2 Risk → Model → Cost → Approval 线性解环流

⚠️ 本模块的职责**非常窄**：

     ✅ 产生操作 / 业务风险，用于**约束**模型选择
     ❌ 不决定"是否暂停"（那是 Approval Judgment）
     ❌ 不含任何审批流程控制字段
     ❌ 不含成本事实（"显著额外费用"是 [15a] CostEstimate 的事实）

   理由（架构 2 节）：即使 Risk 是 LOW，后面的 Cost Estimate 仍可能发现
   "显著额外费用"，所以 Approval Judgment **本来就必须运行**。
   一个"是否需要考虑审批"的 bool 极易被写成
   ``if not risk.requires_approval_consideration: skip_approval_judgment()``
   —— 那会重新制造审批旁路。

⚠️ 本模块**不选择模型**。它只输出 level + categories 供 [06] 约束档位。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import (
    Confidence,
    ImpactScope,
    RiskAssessment,
    RiskCategory,
    RiskLevel,
)

__all__ = [
    "SIGNAL_INPUT_FIELD",
    "RiskSignal",
    "RiskInputs",
    "RiskEvaluator",
    "requires_human_input",
]


class RiskSignal(Enum):
    """L41 / L43 的高风险场景类别。

    值与冻结契约 ``RiskCategory`` **一一对应**（10 项）。

    ⚠️ **不含成本信号**：Risk 在 Cost Estimator 之前运行，
       此时还不知道真正的成本估算。"显著额外费用"由
       ``[15a] CostEstimate → ApprovalJudgment`` 判断。
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


#: 命中即判 HIGH 的信号 —— 不可逆、影响真实用户、涉及凭据或发布付款。
_IRREVERSIBLE_SIGNALS: frozenset[RiskSignal] = frozenset(
    {
        RiskSignal.HARD_TO_REVERT_OPERATION,
        RiskSignal.PRODUCTION_OR_REAL_USERS,
        RiskSignal.RELEASE_DEPLOY_MERGE_PAYMENT,
        RiskSignal.CREDENTIALS_PRIVACY_PERMISSIONS,
    }
)

#: 依赖深度达到该值即抬升风险。
_DEEP_DEPENDENCY = 3

#: 信号 → ``RiskInputs`` 中承载它的布尔字段。
#: 显式映射而非"用枚举值拼字段名"，避免两者拼写分叉时静默失效。
SIGNAL_INPUT_FIELD: dict[RiskSignal, str] = {
    RiskSignal.BROAD_HARD_TO_REVIEW_CHANGE: "broad_or_hard_to_review_change",
    RiskSignal.HARD_TO_REVERT_OPERATION: "hard_to_revert_operation",
    RiskSignal.PRODUCTION_OR_REAL_USERS: "production_or_real_users",
    RiskSignal.SHARED_INFRASTRUCTURE: "shared_infrastructure",
    RiskSignal.RELEASE_DEPLOY_MERGE_PAYMENT: "release_deploy_merge_payment",
    RiskSignal.OUTBOUND_MESSAGE: "outbound_message",
    RiskSignal.EXTERNAL_WRITE: "external_write",
    RiskSignal.CREDENTIALS_PRIVACY_PERMISSIONS: "credentials_privacy_permissions",
    RiskSignal.MAJOR_CONCLUSION_CONFLICT: "major_conclusion_conflict",
    RiskSignal.SCOPE_EXPANSION: "scope_expansion",
}


@dataclass(frozen=True)
class RiskInputs:
    """风险评估的**事实输入**。

    ⚠️ 字段集合是**封闭**的：既不含审批控制，也不含成本事实。
       任何新增字段都必须先过架构变更流程。

    ``affected_scope`` / ``dependency_depth`` / ``evidence_sufficiency``
    分别是 [04] Evaluation 的「影响范围」维度、任务内部的依赖链深度，
    以及已有证据的充分度。
    """

    broad_or_hard_to_review_change: bool
    hard_to_revert_operation: bool
    production_or_real_users: bool
    shared_infrastructure: bool
    release_deploy_merge_payment: bool
    outbound_message: bool
    external_write: bool
    credentials_privacy_permissions: bool
    major_conclusion_conflict: bool
    scope_expansion: bool
    affected_scope: ImpactScope
    dependency_depth: int
    evidence_sufficiency: Confidence


class RiskEvaluator:
    """把风险事实折算成 ``RiskAssessment``。

    确定性、无副作用：同样的输入永远得到同样的评估。
    """

    def assess(self, inputs: RiskInputs) -> RiskAssessment:
        self._validate(inputs)

        raised = self._raised_signals(inputs)
        level = self._level(inputs, raised)
        detail = self._detail(inputs, raised, level)

        return RiskAssessment(
            level=level,
            categories=tuple(RiskCategory(signal.value) for signal in raised),
            detail=detail,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _validate(inputs: RiskInputs) -> None:
        if not isinstance(inputs.affected_scope, ImpactScope):
            raise TypeError(
                f"affected_scope 必须是 ImpactScope，收到 {inputs.affected_scope!r}"
            )
        if not isinstance(inputs.evidence_sufficiency, Confidence):
            raise TypeError(
                "evidence_sufficiency 必须是 Confidence，"
                f"收到 {inputs.evidence_sufficiency!r}"
            )
        if not isinstance(inputs.dependency_depth, int) or isinstance(
            inputs.dependency_depth, bool
        ):
            raise TypeError(
                f"dependency_depth 必须是 int，收到 {inputs.dependency_depth!r}"
            )
        if inputs.dependency_depth < 0:
            raise ValueError("dependency_depth 不得为负")

    @staticmethod
    def _raised_signals(inputs: RiskInputs) -> tuple[RiskSignal, ...]:
        """按 ``RiskSignal`` 声明顺序返回命中的信号，保证确定性。"""
        return tuple(
            signal
            for signal in RiskSignal
            if bool(getattr(inputs, SIGNAL_INPUT_FIELD[signal]))
        )

    @staticmethod
    def _level(inputs: RiskInputs, raised: tuple[RiskSignal, ...]) -> RiskLevel:
        if _IRREVERSIBLE_SIGNALS.intersection(raised):
            return RiskLevel.HIGH
        if raised:
            return RiskLevel.MEDIUM
        # 无场景命中，但影响面 / 依赖深度 / 证据充分度仍可能抬升风险
        if inputs.affected_scope is ImpactScope.SYSTEM_WIDE:
            return RiskLevel.MEDIUM
        if inputs.dependency_depth >= _DEEP_DEPENDENCY:
            return RiskLevel.MEDIUM
        if inputs.evidence_sufficiency is Confidence.LOW:
            return RiskLevel.MEDIUM

        # 跨模块改动本身不足以离开 LOW（类别为空才允许 LOW）
        if inputs.affected_scope is ImpactScope.CROSS_MODULE:
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    @staticmethod
    def _detail(
        inputs: RiskInputs, raised: tuple[RiskSignal, ...], level: RiskLevel
    ) -> str:
        parts: list[str] = [f"level={level.value}"]
        if raised:
            parts.append("signals=" + ",".join(signal.value for signal in raised))
        else:
            parts.append("signals=none")
        parts.append(f"scope={inputs.affected_scope.value}")
        parts.append(f"dependency_depth={inputs.dependency_depth}")
        parts.append(f"evidence={inputs.evidence_sufficiency.value}")
        return "; ".join(parts)


def requires_human_input(assessment: RiskAssessment) -> bool:
    """风险侧是否需要人的输入。

    ⚠️ 本函数**不回答"是否需要审批"** —— 架构 4.2 把那个判断留给
       **Approval Judgment**，它在 Risk + Cost + Authorization + 副作用
       齐备（即 Cost Estimator **之后**）才统一决定。

    因此它是一个**独立函数**而不是 ``RiskAssessment`` 的字段：
    这样它就无法被误用成
    ``if not risk.requires_approval_consideration: skip_approval_judgment()``
    那样的审批旁路 —— 它根本不在 risk 对象上。

    ⚠️ 反过来不成立：本函数返回 ``False`` **不意味着**不需要审批。
       成本仍可能显著，Approval Judgment 仍必须运行。
    """
    return assessment.level is RiskLevel.HIGH
