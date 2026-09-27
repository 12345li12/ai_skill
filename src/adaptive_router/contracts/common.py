"""contracts.common —— 通用枚举 / 不可变基础类型。

架构依据：`docs/architecture.md` 第 11.1 节（common.py 归属）
依赖方向：common 是唯一被依赖基座，**不 import 本包其他模块**。

本模块内容（架构冻结清单）：
  ID 相关        RunId / BranchId / AgentId + 生成器
  Confidence     置信度
  ConfirmationSource   确认来源（全系统唯一，INV-S5）
  ExecutionValue       计划 / 请求 / 实际 三值（不同型，各自带来源）
  TerminalState        终止原因枚举（6 值，共享）
  BranchTerminal       分支作用域终止
  RunTerminal          RoutingRun 作用域终止
  ModelTier / EffortLevel  档位与推理强度（**decision 与 runtime 共享，故下沉至此**）

依赖纪律：``common`` 是最底层，**不得 import 本包任何其他模块**（架构 11.2）。
ModelTier / EffortLevel 下沉的原因：``runtime.BranchReturn`` 需要它们表达
"计划/请求/实际推理强度"，若留在 ``decision`` 会造成 ``runtime → decision``
横向依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar
from uuid import uuid4

__all__ = [
    "ContractError",
    "RunId",
    "BranchId",
    "AgentId",
    "new_run_id",
    "new_branch_id",
    "new_agent_id",
    "Confidence",
    "ConfirmationSource",
    "ExecutionValue",
    "TerminalState",
    "BranchTerminal",
    "RunTerminal",
    "ModelTier",
    "EffortLevel",
]


class ContractError(ValueError):
    """契约不变量被违反。

    架构宪法：与之冲突的实现视为缺陷。
    所有契约层的非法构造都抛这个类型，便于契约测试精确断言。
    """


# --------------------------------------------------------------------------
# ID
# --------------------------------------------------------------------------

RunId = str
BranchId = str
AgentId = str

_RUN_PREFIX = "run_"
_BRANCH_PREFIX = "br_"
_AGENT_PREFIX = "ag_"


def _check_prefixed(value: str, prefix: str, kind: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{kind} 必须是非空字符串，收到 {value!r}")
    if not value.startswith(prefix):
        raise ContractError(f"{kind} 必须以 {prefix!r} 开头，收到 {value!r}")
    return value


def new_run_id() -> RunId:
    """生成一个新的 RoutingRun 标识。

    架构依据：RoutingRun = 一次顶层用户任务进入 Router 到产生终态的完整执行周期。
    """
    return f"{_RUN_PREFIX}{uuid4().hex}"


def new_branch_id() -> BranchId:
    """生成一个新的分支标识。"""
    return f"{_BRANCH_PREFIX}{uuid4().hex}"


def new_agent_id() -> AgentId:
    """生成一个新的 Agent 实例标识。"""
    return f"{_AGENT_PREFIX}{uuid4().hex}"


def validate_run_id(value: str) -> RunId:
    return _check_prefixed(value, _RUN_PREFIX, "RunId")


def validate_branch_id(value: str) -> BranchId:
    return _check_prefixed(value, _BRANCH_PREFIX, "BranchId")


def validate_agent_id(value: str) -> AgentId:
    return _check_prefixed(value, _AGENT_PREFIX, "AgentId")


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------


class Confidence(Enum):
    """置信度。三级定性，禁止用浮点数冒充精确概率（L55 精神）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ModelTier(Enum):
    """抽象档位（L33 的 4 档）。

    ``planned`` 侧用它；``actual`` 侧必须是提供方模型 ID（见 ModelSelection）。
    定义在 ``common`` 是因为 ``decision``（选档位）与 ``runtime``（回传
    计划/请求/实际推理强度）都要用 —— 放任何一侧都会造成横向依赖。
    """

    LOW_COST = "low_cost"
    BALANCED = "balanced"
    STRONG = "strong"
    MAXIMUM = "maximum"


class EffortLevel(Enum):
    """推理强度档位。与 ModelTier 同理下沉至 ``common``。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# --------------------------------------------------------------------------
# ConfirmationSource —— 全系统唯一（INV-S5）
# --------------------------------------------------------------------------


class ConfirmationSource(Enum):
    """确认来源。**全系统唯一，禁止各写一份**（架构 6.2 / INV-S5）。

    领域层只存枚举，**不得存展示文案**（架构 6.2.2）：
    上游协议 L19 要求的中文"当前工具无法可靠确认"由 [16] Reporting 渲染，
    Core 只保存 ``UNAVAILABLE``。
    """

    PLANNER = "planner"
    USER = "user"
    REQUEST = "request"
    PROVIDER_API = "provider_api"
    TOOL_REPORT = "tool_report"
    INFERRED = "inferred"
    UNAVAILABLE = "unavailable"


# --------------------------------------------------------------------------
# ExecutionValue —— 三值不同型
# --------------------------------------------------------------------------

TPlan = TypeVar("TPlan")
TRequest = TypeVar("TRequest")
TActual = TypeVar("TActual")


@dataclass(frozen=True)
class ExecutionValue(Generic[TPlan, TRequest, TActual]):
    """计划 / 请求 / 实际 三值，**不强制同类型**（架构 6.1）。

    模型维：``ExecutionValue[Tier, ModelId, ModelId]``
    成本维：``ExecutionValue[CostEstimate, CostRequest, CostActual]``

    统一的是"来源与确认语义"，不是数据类型。
    三个值各带一个 source —— 因为"计划值的来源"与"实际值的来源"是两个来源。

    构造期不变量（架构 6.3 / INV-07 / INV-08 / 第 2 步裁决 2、3）：

      1. ``observed=False`` → ``actual is None`` 且 ``actual_source is UNAVAILABLE``
      2. ``observed=True``  → ``actual is not None`` 且 ``actual_source is not UNAVAILABLE``
      3. ``planned_source`` 不得为 ``UNAVAILABLE``（planned 必有值）
      4. ``requested_source`` 不得为 ``UNAVAILABLE``（requested 必有值）

    第 3、4 条的用意：``UNAVAILABLE`` 是"实际运行事实无法确认"的合法状态，
    **不是给计划值和请求值偷懒填的默认值**。

    由此 ``actual=0, observed=True`` 与 ``actual=None, observed=False``
    在类型与构造期都**永远不可等价**（INV-07）。
    """

    planned: TPlan
    planned_source: ConfirmationSource
    requested: TRequest
    requested_source: ConfirmationSource
    actual: TActual | None
    actual_source: ConfirmationSource
    observed: bool
    confidence: Confidence

    def __post_init__(self) -> None:
        _require_enum(self.planned_source, ConfirmationSource, "planned_source")
        _require_enum(self.requested_source, ConfirmationSource, "requested_source")
        _require_enum(self.actual_source, ConfirmationSource, "actual_source")
        _require_enum(self.confidence, Confidence, "confidence")

        if not isinstance(self.observed, bool):
            raise ContractError(f"observed 必须是 bool，收到 {self.observed!r}")

        # 规则 1 / 2 —— unknown ≠ 0
        if not self.observed:
            if self.actual is not None:
                raise ContractError(
                    "observed=False 时 actual 必须为 None（未观测不得携带数值，"
                    "否则 0 会冒充 unknown）"
                )
            if self.actual_source is not ConfirmationSource.UNAVAILABLE:
                raise ContractError(
                    "observed=False 时 actual_source 必须是 UNAVAILABLE，"
                    f"收到 {self.actual_source!r}"
                )
        else:
            if self.actual is None:
                raise ContractError("observed=True 时 actual 必须给值（测到 0 也是值）")
            if self.actual_source is ConfirmationSource.UNAVAILABLE:
                raise ContractError(
                    "observed=True 时 actual_source 不得为 UNAVAILABLE"
                )

        # 规则 3 / 4 —— UNAVAILABLE 不是偷懒默认值
        if self.planned_source is ConfirmationSource.UNAVAILABLE:
            raise ContractError(
                "planned 有值，planned_source 不得为 UNAVAILABLE（它不是默认值）"
            )
        if self.requested_source is ConfirmationSource.UNAVAILABLE:
            raise ContractError(
                "requested 有值，requested_source 不得为 UNAVAILABLE（它不是默认值）"
            )

    @classmethod
    def unobserved(
        cls,
        planned: TPlan,
        requested: TRequest,
        planned_source: ConfirmationSource,
        requested_source: ConfirmationSource,
        confidence: Confidence = Confidence.LOW,
    ) -> "ExecutionValue[TPlan, TRequest, TActual]":
        """构造一个"实际值无法可靠确认"的三值。

        架构 6.2.2 冻结形态：
        ``actual=None, observed=False, actual_source=UNAVAILABLE``
        """
        return cls(
            planned=planned,
            planned_source=planned_source,
            requested=requested,
            requested_source=requested_source,
            actual=None,
            actual_source=ConfirmationSource.UNAVAILABLE,
            observed=False,
            confidence=confidence,
        )

    @property
    def actual_confirmed(self) -> bool:
        """实际值是否已被可靠确认。"""
        return self.observed


def _require_enum(value: Any, expected: type[Enum], field: str) -> None:
    if not isinstance(value, expected):
        raise ContractError(f"{field} 必须是 {expected.__name__}，收到 {value!r}")


# --------------------------------------------------------------------------
# TerminalState —— 终止原因枚举（6 值，Run 与 Branch 共享）
# --------------------------------------------------------------------------


class TerminalState(Enum):
    """终止原因。**6 个值**（架构 6.5 / INV-17）。Run 与 Branch 共享本枚举。

    `SUPERSEDED` 表示分支被新的重新规划替代（不是失败）。
    若把它算作 FAILURE，**失败率与成本分析会被污染**。
    """

    SUCCESS = "success"
    USER_CANCELLED = "user_cancelled"
    APPROVAL_DENIED = "approval_denied"
    TERMINAL_FAILURE = "terminal_failure"
    ABORTED_NO_CAPABILITY = "aborted_no_capability"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class BranchTerminal:
    """**分支作用域**的终止（第 2 步裁决 1）。

    共享"终止原因枚举"，分开"终止对象类型" —— 类型层阻止
    ``Branch SUCCESS`` 被错误当成 ``RoutingRun SUCCESS``。
    """

    branch_id: BranchId
    state: TerminalState

    def __post_init__(self) -> None:
        validate_branch_id(self.branch_id)
        _require_enum(self.state, TerminalState, "state")


@dataclass(frozen=True)
class RunTerminal:
    """**RoutingRun 作用域**的终止（第 2 步裁决 1）。

    架构 4.5 硬说明：Run 只有在所属分支均已终止、Recovery / Approval 等
    控制流均已收敛后，才允许产生 RunTerminal 并进入 [15c] Cost Summary。
    """

    run_id: RunId
    state: TerminalState

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        _require_enum(self.state, TerminalState, "state")
