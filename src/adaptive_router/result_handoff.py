"""A15 的 seam DTO —— ``[11] → [12]`` 的**结果交接边界**（Core 侧）。

架构依据：`docs/architecture.md` 2 节 ``[12] Execution Results``（**Amendment A15**，FROZEN）；
范围、锁清单（LR-1 – LR-12）与 probe 计划见 `tests/behavior/README_step5b.md` §62。

**这些 DTO 落在 Core 顶层**（与 ``agent_start_requests.py`` / ``runtime_control_facts.py`` 同形），
**不进 ``contracts/``**（A15.7 已冻结），也**不进 ``adapters/``** —— 因为 ``[12]`` 在 Core 内，
而 **Core 一律不得 import adapters**（L11-1）。

**owner 逐项标注**（谁产生，谁就不是谁的副本）：

    RuntimeResultPayload     owner = [11] Runtime Adapter
        · A15.2：它是 **Adapter 已规范化的结果事实**，**不是** ``raw_provider_response``
          （Core 层禁止平台细节 ⇒ 平台格式解析属 [11] 的回程翻译职责）
    ResultHandoff            owner = P1 execution.py
        · A15.3：七字段 = **身份三字段（P1 附加）** + ``planned_fact``（P1 附加的 [06] 投影）
          + 三个来自 payload 的字段

**provenance 是结构性的，不是文档约定**（裁定 ②）：

    PlannedFacts     = what was intended    ← [06] 的档位投影
    RequestedFacts   = what was requested   ← [11] 的翻译结果
    ActualFacts      = what was observed    ← 平台实测

⚠️ **禁止**整体对象偷渡（A15.5 / LR-4）：本模块**不出现** ``RoutingPlan`` / ``Plan`` /
``ModelSelection`` 任何一个符号。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import (
    AgentId,
    BranchId,
    Confidence,
    ConfirmationSource,
    EffortLevel,
    ModelTier,
)
from .contracts.common import validate_agent_id, validate_branch_id

__all__ = [
    "ActualFacts",
    "PlannedFacts",
    "ProviderResultPayload",
    "RequestedFacts",
    "ResultHandoff",
    "RuntimeResultPayload",
]


def _require_non_empty_str(value: object, label: str) -> None:
    """本模块自用的最小形状校验（与契约包同一纪律：阻止非法状态被构造出来）。"""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} 必须是非空 str，收到 {value!r}")


def _require_str_tuple(value: object, label: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} 必须是 tuple，收到 {value!r}")
    for item in value:
        if type(item) is not str:
            raise TypeError(f"{label} 的元素必须是 str，收到 {item!r}")


def _require_enum(value: object, enum_type: type[Enum], label: str) -> None:
    if not isinstance(value, enum_type):
        raise TypeError(f"{label} 必须是 {enum_type.__name__}，收到 {value!r}")


@dataclass(frozen=True)
class PlannedFacts:
    """**计划**侧事实 —— ``[06]`` 的档位投影（owner = P1；**不是** ``ModelSelection`` 整体）。"""

    tier: ModelTier
    effort: EffortLevel
    source: ConfirmationSource

    def __post_init__(self) -> None:
        _require_enum(self.tier, ModelTier, "tier")
        _require_enum(self.effort, EffortLevel, "effort")
        _require_enum(self.source, ConfirmationSource, "source")
        # ExecutionValue 不变量 3：planned 必有来源，UNAVAILABLE 不是偷懒默认值
        if self.source is ConfirmationSource.UNAVAILABLE:
            raise ValueError("planned 的 source 不得为 UNAVAILABLE（ExecutionValue 不变量 3）")


@dataclass(frozen=True)
class RequestedFacts:
    """**请求**侧事实 —— ``[11]`` 的翻译结果（owner = `[11]`）。

    ⚠️ ``model_id`` **不得是抽象档位** —— 该规则**只有一处实现**（``BranchReturn``
    的 ``_require_model_dimension``）；本 DTO 只保证它是**非空 str**，不复制那条规则。
    """

    model_id: str
    effort: EffortLevel
    source: ConfirmationSource

    def __post_init__(self) -> None:
        _require_non_empty_str(self.model_id, "model_id")
        _require_enum(self.effort, EffortLevel, "effort")
        _require_enum(self.source, ConfirmationSource, "source")
        # ExecutionValue 不变量 4
        if self.source is ConfirmationSource.UNAVAILABLE:
            raise ValueError(
                "requested 的 source 不得为 UNAVAILABLE（ExecutionValue 不变量 4）"
            )


@dataclass(frozen=True)
class ActualFacts:
    """**实际**侧事实 —— 平台实测（owner = `[11]` / 平台）。

    ⚠️ ``observed`` 与 ``source`` **必须互相一致**：本 DTO 在**构造期**就拒绝不一致的组合
    （把 ``ExecutionValue`` 的规则 1/2 提前到 seam 上失败）——
    于是 `[12]` 的转换期**不可能**遇到"未观测却有 actual"或"已观测却 UNAVAILABLE"的输入。
    """

    model_id: str | None
    effort: EffortLevel | None
    source: ConfirmationSource
    confidence: Confidence
    observed: bool

    def __post_init__(self) -> None:
        _require_enum(self.source, ConfirmationSource, "source")
        _require_enum(self.confidence, Confidence, "confidence")
        if not isinstance(self.observed, bool):
            raise TypeError(f"observed 必须是 bool，收到 {self.observed!r}")
        if self.model_id is not None:
            _require_non_empty_str(self.model_id, "model_id")
        if self.effort is not None:
            _require_enum(self.effort, EffortLevel, "effort")

        if self.observed:
            if self.model_id is None or self.effort is None:
                raise ValueError("observed=True 时 model_id / effort 都必须给值")
            if self.source is ConfirmationSource.UNAVAILABLE:
                raise ValueError("observed=True 时 source 不得为 UNAVAILABLE")
        else:
            if self.model_id is not None or self.effort is not None:
                raise ValueError(
                    "observed=False 时 model_id / effort 必须为 None"
                    "（未观测不得携带数值，否则会冒充 actual）"
                )
            if self.source is not ConfirmationSource.UNAVAILABLE:
                raise ValueError("observed=False 时 source 必须是 UNAVAILABLE")


@dataclass(frozen=True)
class ProviderResultPayload:
    """L37 的**内容项**（owner = `[11]`，已规范化）。

    ⚠️ 字段名是 **``provider_status``**，**不是** ``validation_state``：
    ``validation`` 在架构里属 **`[13]`**；`[12]` 只**记录** provider / runtime 返回的事实，
    `[13]` 才**验证**这些事实。（`BranchReturn` 自己的冻结字段名仍叫 ``validation_state``
    —— 那是 L37 的十项之一，**不改**；本 DTO 只是它的**来源**，并显式表明它是"自报状态"。）
    """

    provider_status: str
    conclusion_summary: str
    key_evidence: tuple[str, ...]
    residual_risks: tuple[str, ...]
    main_agent_todo: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty_str(self.provider_status, "provider_status")
        _require_non_empty_str(self.conclusion_summary, "conclusion_summary")
        _require_str_tuple(self.key_evidence, "key_evidence")
        _require_str_tuple(self.residual_risks, "residual_risks")
        _require_str_tuple(self.main_agent_todo, "main_agent_todo")


@dataclass(frozen=True)
class RuntimeResultPayload:
    """``[11]`` 的**结果输出**（owner = `[11]`）。

    A15.2：**已规范化事实**，**不是** ``raw_provider_response``。
    """

    requested: RequestedFacts
    actual: ActualFacts
    payload: ProviderResultPayload

    def __post_init__(self) -> None:
        if not isinstance(self.requested, RequestedFacts):
            raise TypeError(f"requested 必须是 RequestedFacts，收到 {self.requested!r}")
        if not isinstance(self.actual, ActualFacts):
            raise TypeError(f"actual 必须是 ActualFacts，收到 {self.actual!r}")
        if not isinstance(self.payload, ProviderResultPayload):
            raise TypeError(f"payload 必须是 ProviderResultPayload，收到 {self.payload!r}")


@dataclass(frozen=True)
class ResultHandoff:
    """A15.3 冻结的**七字段** carrier（owner = P1）。

    身份三字段由 **P1 附加**（`[12]` 是结果入口，**不反查运行上下文**）；
    ``agent_id`` 由 P1 从 `[10]` 的既有记录解析（A14.2：**绝不由 Adapter 提供**）。
    """

    branch_id: BranchId
    agent_id: AgentId
    attempt: int
    planned_fact: PlannedFacts
    requested_fact: RequestedFacts
    actual_fact: ActualFacts
    provider_payload: ProviderResultPayload

    def __post_init__(self) -> None:
        validate_branch_id(self.branch_id)
        validate_agent_id(self.agent_id)
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise TypeError(f"attempt 必须是 int，收到 {self.attempt!r}")
        if self.attempt < 1:
            raise ValueError(f"attempt 从 1 开始计数（架构 9.1），收到 {self.attempt!r}")
        if not isinstance(self.planned_fact, PlannedFacts):
            raise TypeError(f"planned_fact 必须是 PlannedFacts，收到 {self.planned_fact!r}")
        if not isinstance(self.requested_fact, RequestedFacts):
            raise TypeError(
                f"requested_fact 必须是 RequestedFacts，收到 {self.requested_fact!r}"
            )
        if not isinstance(self.actual_fact, ActualFacts):
            raise TypeError(f"actual_fact 必须是 ActualFacts，收到 {self.actual_fact!r}")
        if not isinstance(self.provider_payload, ProviderResultPayload):
            raise TypeError(
                f"provider_payload 必须是 ProviderResultPayload，收到 {self.provider_payload!r}"
            )
