"""Approval Disclosure —— 四项审批清单里**非推导项**的承载与 provenance。

架构依据：`docs/architecture.md` 2 节 ``Approval Judgment``
**Amendment A8**（Approval Disclosure provenance seam，**FROZEN**）。

**A8 的核心纪律：**

> **Composition 只有 transport / projection 权，没有事实发明权。**
> **"调用方"不是事实 owner。**

因此本模块**只做三件被冻结的事**，每件都可逐条审计：

```text
proposed_action  ← [05] Plan 既有陈述的**确定性投影**
                   main_thread_duty + 按 Plan.branches 原顺序的 BranchSpec.goal
                   ⚠️ 不得重新总结成"另一个计划"

impact_scope     ← [04] Evaluation.impact_scope 的**单字段投影**
                   ⚠️ 不得从 branch 数量 / 可写分支数二次推导 ——
                      那是**执行规模**，不是**影响范围**

recovery_method  ← 1. 有显式 recovery fact        → 原样投影
                   2. 无 fact + HARD_TO_REVERT    → 显式未知（A8.3 冻结文本）
                   3. 无 fact + 未命中            → **显式失败**
```

⚠️ **不得**出现：默认 ``"git revert"``、按代码库类型**猜** recovery、
按 branch 数量**猜** impact scope、把 ``Plan`` **重新概括**成新的
proposed action —— 这些都不是"提供事实"，而是**事实发明**。

第四项 ``main_risk`` **不在这里**：它由 Approval Judgment 自行推导
（A6.5 / A7.5 对它有两套内容规则）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import Evaluation, Plan, RiskAssessment, RiskCategory

__all__ = [
    "HARD_TO_REVERT_UNKNOWN",
    "ApprovalDisclosure",
    "DisclosureProvenanceError",
    "build_disclosure",
    "project_impact_scope",
    "project_proposed_action",
    "resolve_recovery_method",
]


class DisclosureProvenanceError(ValueError):
    """没有合法事实来源可产出某个 disclosure 字段。

    A8.3：**不得**为了让清单非空而编造内容 ——
    编造出来的"恢复方式"会让人类基于**假事实**授权。
    """


#: A8.3 冻结的**显式未知**文本（`HARD_TO_REVERT_OPERATION` 分支）。
#:
#: ⚠️ 措辞是冻结的：`HARD_TO_REVERT_OPERATION` 只证明"**难以**恢复"，
#: **不能**证明"**无可靠恢复方式**" —— 不得改写成更强的断言。
HARD_TO_REVERT_UNKNOWN = "未提供具体恢复方式；该操作被标记为难以恢复。"


@dataclass(frozen=True)
class ApprovalDisclosure:
    """四项清单里**由 composition 经 A8 通路搬运**的三项。

    ⚠️ 本 DTO 只**承载**事实；它不判断"是否需要审批"
    （那是 Approval Judgment 的职责）。

    第四项 ``main_risk`` 由 Judgment **自行推导**。
    """

    proposed_action: str
    impact_scope: str
    recovery_method: str

    def __post_init__(self) -> None:
        for name in ("proposed_action", "impact_scope", "recovery_method"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} 必须是 str，收到 {value!r}")
            if not value.strip():
                raise ValueError(f"{name} 必须是非空 str（L43 四项清单不得为空）")


# --------------------------------------------------------------------------
# A8.3 —— 三条 provenance 规则（各自公开，便于逐条审计与测试）
# --------------------------------------------------------------------------


def project_proposed_action(plan: Plan) -> str:
    """A8.3 ①：``[05] Plan`` 既有陈述的**确定性投影**。

    ```text
    主线程  → 恒来自 plan.main_thread_duty
    有 child → 按 plan.branches **原顺序**附带各 BranchSpec.goal
    ```

    ⚠️ **只搬运** Plan 自己写下的文本，**不得**重新总结成"另一个计划"。
    """
    if not isinstance(plan, Plan):
        raise TypeError(f"plan 必须是 Plan，收到 {plan!r}")
    parts = [f"主线程：{plan.main_thread_duty}"]
    for spec in plan.branches:
        parts.append(f"分支 {spec.branch_id}：{spec.goal}")
    return "；".join(parts)


def project_impact_scope(evaluation: Evaluation) -> str:
    """A8.3 ②：``[04] Evaluation.impact_scope`` 的**单字段投影**。

    ⚠️ 这是**事实搬运**，不是第二个 Evaluator：
    不得把 ``Evaluation`` 整体塞进下游，也不得从
    branch 数量 / 可写分支数**重新推导**"影响范围"。
    """
    if not isinstance(evaluation, Evaluation):
        raise TypeError(f"evaluation 必须是 Evaluation，收到 {evaluation!r}")
    return evaluation.impact_scope.value


def resolve_recovery_method(
    risk_assessment: RiskAssessment,
    *,
    recovery_method: str | None = None,
) -> str:
    """A8.3 ③：``recovery_method`` 的三分支语义。

    ```text
    1. 有显式 recovery fact                  → 原样投影
    2. 无 fact + HARD_TO_REVERT_OPERATION 命中 → 显式未知（冻结文本）
    3. 无 fact + 未命中                       → 显式失败
    ```

    ⚠️ 第 2 分支**只**说"难以恢复"，**不得**升级为"无可靠恢复方式"；
    第 3 分支**不得**默认填充一个具体方法（例如 ``"git revert"``）。
    """
    if not isinstance(risk_assessment, RiskAssessment):
        raise TypeError(
            f"risk_assessment 必须是 RiskAssessment，收到 {risk_assessment!r}"
        )
    if recovery_method is not None:
        if not isinstance(recovery_method, str):
            raise TypeError(f"recovery_method 必须是 str | None，收到 {recovery_method!r}")
        if not recovery_method.strip():
            raise DisclosureProvenanceError(
                "显式 recovery fact 不得为空 —— 空串不是「未知」："
                "要么给出真实方法，要么传 None 走显式未知 / 显式失败分支"
            )
        return recovery_method

    if RiskCategory.HARD_TO_REVERT_OPERATION in risk_assessment.categories:
        return HARD_TO_REVERT_UNKNOWN

    raise DisclosureProvenanceError(
        "没有合法来源可产出 recovery_method：既未提供显式 recovery fact，"
        "风险也未标记 HARD_TO_REVERT_OPERATION。"
        "**不得**编造一个恢复方式（A8.3）—— 请提供真实的上游 recovery fact。"
    )


def build_disclosure(
    *,
    plan: Plan,
    evaluation: Evaluation,
    risk_assessment: RiskAssessment,
    recovery_method: str | None = None,
) -> ApprovalDisclosure:
    """组合层经 A8 通路构造 ``ApprovalDisclosure`` 的**唯一**入口。

    它只做 A8.3 的三条投影；其中 ``recovery_method`` 无合法来源时
    **显式失败**，而不是让清单看起来"填满了"。
    """
    return ApprovalDisclosure(
        proposed_action=project_proposed_action(plan),
        impact_scope=project_impact_scope(evaluation),
        recovery_method=resolve_recovery_method(
            risk_assessment, recovery_method=recovery_method
        ),
    )
