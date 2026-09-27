"""`judgment_source` 与分支结果载荷 —— Host 必须提供的**判定注入**（自建）。

契约来源 = `§279.3`。

★ **锚定陷阱（本文件的核心知识点）**：
    `ValidationJudgment.verdicts[].criterion` **必须回填 Core 给出的权威完成标准**
    （即 `judgment_source(branch_return, criteria)` 的 `criteria` 参数）。
    ⛔ 若判定来源自己编一个 criterion，**锚定校验不过**。
    ⇒ 判定来源只能"承认 Core 的标准并给出证据"，⛔ 不能自造标准。
    这正是反证 `MP-B3` 要打的地方。
"""

from __future__ import annotations

from typing import Callable

from adaptive_router.contracts import Confidence, ConfirmationSource, EffortLevel
from adaptive_router.result_handoff import (
    ActualFacts,
    ProviderResultPayload,
    RequestedFacts,
    RuntimeResultPayload,
)
from adaptive_router.validation_judgment import CriterionVerdict, ValidationJudgment

MODEL_ID = "demo-model-x"


def payload(*, conclusion_summary: str = "demo 子任务已完成", evidence: tuple[str, ...] = ("demo-evidence-1",)) -> RuntimeResultPayload:
    """一次真实的"分支结果上报"载荷（请求面 / 实际面 / 供应方产出）。"""
    return RuntimeResultPayload(
        requested=RequestedFacts(
            model_id=MODEL_ID,
            effort=EffortLevel.MEDIUM,
            source=ConfirmationSource.REQUEST,
        ),
        actual=ActualFacts(
            model_id=MODEL_ID,
            effort=EffortLevel.MEDIUM,
            source=ConfirmationSource.PROVIDER_API,
            confidence=Confidence.HIGH,
            observed=True,
        ),
        payload=ProviderResultPayload(
            provider_status="ok",
            conclusion_summary=conclusion_summary,
            key_evidence=evidence,
            residual_risks=(),
            main_agent_todo=(),
        ),
    )


def judgment_source(*, satisfied: bool = True, backfill_criterion: bool = True) -> Callable[[object, str], ValidationJudgment]:
    """判定来源工厂。

    参数 `backfill_criterion=True` = **正确**形态（回填权威标准 ⇒ 锚定校验通过）。
    置 `False` 仅供反证 `MP-B3` 使用：自造 criterion ⇒ 必须被判红。
    """

    def source(branch_return: object, criteria: str) -> ValidationJudgment:
        del branch_return
        return ValidationJudgment(
            verdicts=(
                CriterionVerdict(
                    criterion=criteria if backfill_criterion else "（自造标准：demo 说了算）",
                    satisfied=satisfied,
                    evidence=("demo-evidence-1",),
                ),
            ),
            detail="demo judgment",
        )

    return source
