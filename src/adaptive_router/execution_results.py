"""``[12] Execution Results`` —— A15 的**结果入口**（Core 内的**纯转换器**）。

架构依据：`docs/architecture.md` 2 节 ``[12] Execution Results``（**Amendment A15**，FROZEN）；
范围、锁清单（LR-1 – LR-12）与 probe 计划见 `tests/behavior/README_step5b.md` §62。

**唯一输入** ``ResultHandoff``；**唯一输出** ``BranchReturn``（含两个 ``ExecutionValue`` 三值）。

**三条硬边界（A15 冻结）：**

- **不反查运行上下文**：不 import `[09]`/`[10]`/`[11]`/``execution``，不读 ``RoutingPlan``、
  不读 ``pending_admission`` / ``routing_run``；
- **不做判断**：不 Validation（`[13]`）、不 Recovery / Retry（`[14]`）、不 Cost（`[15c]`）、
  不 Approval、不 Routing；
- **V1 无状态**：本模块**没有类**、**没有 ``self``**、**没有模块级可变容器** ——
  两个**模块级纯函数**就是全部公开面（"无状态"因此是可机械断言的事实，LR-5）。

**⚠️ 最大风险：事实污染。** ``planned`` / ``requested`` / ``actual`` 三值必须**各来自各自的 fact**；
**禁止**在 ``actual`` 未观测时用 ``planned`` 顶替（那正是 INV-04 禁止的"把计划配置伪装成实际配置"）。
"""

from __future__ import annotations

from typing import TypeVar

from .contracts import BranchReturn, Confidence, ConfirmationSource, ExecutionValue
from .result_handoff import ResultHandoff

__all__ = ["build_branch_return", "build_execution_value"]

TPlan = TypeVar("TPlan")
TRequest = TypeVar("TRequest")
TActual = TypeVar("TActual")


def build_execution_value(
    *,
    planned: TPlan,
    planned_source: ConfirmationSource,
    requested: TRequest,
    requested_source: ConfirmationSource,
    actual: TActual | None,
    actual_source: ConfirmationSource,
    observed: bool,
    confidence: Confidence,
) -> ExecutionValue[TPlan, TRequest, TActual]:
    """**唯一**的 ``ExecutionValue`` 构造点（全 ``src/`` 只有这一处）。

    它是**透传**而非转换：三个值与三个来源**原样**来自各自的 fact，
    本函数**不推断、不顶替、不改写**。四条构造期不变量由 ``ExecutionValue`` 自己强制
    （``observed`` ↔ ``actual`` / ``actual_source``；``planned_source`` / ``requested_source``
    不得为 ``UNAVAILABLE``）。
    """
    return ExecutionValue(
        planned=planned,
        planned_source=planned_source,
        requested=requested,
        requested_source=requested_source,
        actual=actual,
        actual_source=actual_source,
        observed=observed,
        confidence=confidence,
    )


def build_branch_return(*, handoff: ResultHandoff) -> BranchReturn:
    """把 ``ResultHandoff`` 装配成 L37 的**十项** ``BranchReturn``。

    十个实参**逐一来自** handoff 的字段 —— 本函数**只装配，不判断**：

        branch_id / agent_id / attempt   ← 身份（P1 附加）
        model                            ← planned.tier + requested.model_id + actual.model_id
        effort                           ← planned.effort + requested.effort + actual.effort
        conclusion_summary / key_evidence / residual_risks / main_agent_todo
                                         ← provider_payload（原样）
        validation_state                 ← provider_payload.provider_status（**自报状态**，
                                           不是 [13] 的判定）
    """
    model = build_execution_value(
        planned=handoff.planned_fact.tier,
        planned_source=handoff.planned_fact.source,
        requested=handoff.requested_fact.model_id,
        requested_source=handoff.requested_fact.source,
        actual=handoff.actual_fact.model_id,
        actual_source=handoff.actual_fact.source,
        observed=handoff.actual_fact.observed,
        confidence=handoff.actual_fact.confidence,
    )
    effort = build_execution_value(
        planned=handoff.planned_fact.effort,
        planned_source=handoff.planned_fact.source,
        requested=handoff.requested_fact.effort,
        requested_source=handoff.requested_fact.source,
        actual=handoff.actual_fact.effort,
        actual_source=handoff.actual_fact.source,
        observed=handoff.actual_fact.observed,
        confidence=handoff.actual_fact.confidence,
    )
    payload = handoff.provider_payload
    return BranchReturn(
        branch_id=handoff.branch_id,
        agent_id=handoff.agent_id,
        attempt=handoff.attempt,
        model=model,
        effort=effort,
        conclusion_summary=payload.conclusion_summary,
        key_evidence=payload.key_evidence,
        validation_state=payload.provider_status,
        residual_risks=payload.residual_risks,
        main_agent_todo=payload.main_agent_todo,
    )
