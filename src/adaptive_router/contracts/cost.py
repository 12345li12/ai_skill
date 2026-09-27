"""contracts.cost —— 成本类契约。

架构依据：`docs/architecture.md` 第 11.1 节（cost.py 归属）+ 4.5 Cost Y 型流
依赖方向：仅依赖 ``contracts.common``。

本模块内容（架构冻结清单）：
  CostBasis / CostMagnitude      估算依据与量级
  CostEstimate                   [15a] 执行前预测
  CostRequest                    [11] 平台请求参数
  CostActual                     [11] 平台回报的实际值
  LedgerStage / LedgerEntry      [15b] 执行中记录
  CostUnitSummary                [15c] 一个 coverage unit = (branch_id, attempt)
  CostSummary                    [15c] 执行后汇总（coverage 与金额全部派生）

⚠️ 本模块承载 INV-07（unknown ≠ 0）、INV-14（必须披露 coverage）、
   INV-18（未知覆盖时精确值字段名必须含"已确认"语义）。

   INV-18 的契约化落实：字段名就是 ``confirmed_cost_exact``，
   而"完整总成本"只能走 ``complete_total_magnitude`` / ``complete_total_exact``，
   且后者的合法性由构造期按 coverage 强制。

aggregation scope（架构 4.5）：
  RoutingRun = 一次顶层用户任务进入 Router 到产生一个终态的完整执行周期。
  [15c] 是**每 RoutingRun 一次**，不是每 Session 一次（Session 无可靠结束事件）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .common import (
    BranchId,
    Confidence,
    ConfirmationSource,
    ContractError,
    RunId,
    validate_branch_id,
    validate_run_id,
)

__all__ = [
    "COVERAGE_QUALITATIVE_THRESHOLD",
    "CostBasis",
    "CostMagnitude",
    "CostEstimate",
    "CostRequest",
    "CostActual",
    "LedgerStage",
    "LedgerEntry",
    "CostUnitSummary",
    "CostSummary",
]


#: 架构 9.2 冻结值：``coverage < 0.5`` 时整体成本只能给定性结论。
COVERAGE_QUALITATIVE_THRESHOLD = 0.5


def _require_enum(value: Any, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise ContractError(f"{field_name} 必须是 {expected.__name__}，收到 {value!r}")


def _require_non_empty_str(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field_name} 必须是非空字符串，收到 {value!r}")


def _require_str_tuple(value: Any, field_name: str) -> None:
    if not isinstance(value, tuple):
        raise ContractError(f"{field_name} 必须是 tuple（不可变契约）")
    for item in value:
        _require_non_empty_str(item, f"{field_name} 元素")


def _require_number(value: Any, field_name: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ContractError(f"{field_name} 必须是数值，收到 {value!r}")


# --------------------------------------------------------------------------
# [15a] Cost Estimator
# --------------------------------------------------------------------------


class CostBasis(Enum):
    """估算依据。``UNKNOWN`` 是合法状态，**不是异常**（架构 2 节 [15a]）。"""

    HEURISTIC = "heuristic"
    HISTORICAL = "historical"
    PROVIDER_QUOTE = "provider_quote"
    UNKNOWN = "unknown"


class CostMagnitude(Enum):
    """三级量级。L55 允许"仅用低/中/高或相对描述"。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class CostEstimate:
    """[15a] 执行前预测。

    ⚠️ 它**读** [06] 的档位，不能反过来改档位（INV-05）。
    ⚠️ 它**不能自己触发审批门**，只提供输入给 Approval Judgment。

    基数（架构 4.5 [15a] 冻结）：**每分支的每个执行 attempt 最多一次**。
      Attempt 1 必须产生；后续 attempt 仅当成本驱动（模型/范围/并发/副作用）
      变化时重估，否则复用上一 attempt 的估算并保留来源。

    ``basis="historical"`` 会撞上 L7（状态默认仅限当前会话）：有依据的历史
    估算需要跨会话数据，**必须显式 opt-in**（INV-15）。默认只能启发式。
    """

    basis: CostBasis
    magnitude: CostMagnitude
    confidence: Confidence
    drivers: tuple[str, ...]
    range_min: float | None = None
    range_max: float | None = None

    def __post_init__(self) -> None:
        _require_enum(self.basis, CostBasis, "basis")
        _require_enum(self.magnitude, CostMagnitude, "magnitude")
        _require_enum(self.confidence, Confidence, "confidence")
        _require_str_tuple(self.drivers, "drivers")

        if self.range_min is None and self.range_max is None:
            # 只有量级没有区间 —— 完全合法（L55 定性描述）
            return
        if self.range_min is None or self.range_max is None:
            raise ContractError("区间必须同时给出 range_min 与 range_max")
        _require_number(self.range_min, "range_min")
        _require_number(self.range_max, "range_max")
        if self.range_min > self.range_max:
            raise ContractError("range_min 不得大于 range_max")
        if self.range_min < 0:
            raise ContractError("成本区间不得为负")
        if self.basis is CostBasis.UNKNOWN:
            raise ContractError(
                "basis=UNKNOWN 时不得给出精确区间（无依据就不许有数字，L55）"
            )

    @property
    def is_quantitative(self) -> bool:
        """是否给出了可用的数值区间。"""
        return self.range_min is not None and self.range_max is not None


# --------------------------------------------------------------------------
# [11] 请求值 / 实际值
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CostRequest:
    """[11] 向平台发出的成本相关请求参数（L19 的"请求"值）。"""

    model_id: str
    effort: str
    platform: str
    max_concurrency: int | None = None

    def __post_init__(self) -> None:
        for name in ("model_id", "effort", "platform"):
            _require_non_empty_str(getattr(self, name), name)
        if self.max_concurrency is not None:
            if not isinstance(self.max_concurrency, int) or isinstance(
                self.max_concurrency, bool
            ):
                raise ContractError("max_concurrency 必须是 int | None")
            if self.max_concurrency < 1:
                raise ContractError("max_concurrency 至少为 1")


@dataclass(frozen=True)
class CostActual:
    """[11] 平台回报的实际用量（L19 的"实际"值）。

    ⚠️ INV-07：``None`` 表示**未观测到**，``0`` 表示**确实为零**。
       两者语义相反，**永远不可等价**。

    ⚠️ INV-08：必须携带 ``source``。无法确认时取
       ``ConfirmationSource.UNAVAILABLE``（中文文案由 [16] Reporting 渲染）。

    ⚠️ **``cost_accounting_key`` —— 成本口径可比性**（架构 9.2 边界 2）：

       "只有**可加**的观测才能求精确和；**跨平台口径不同不可加**。"

       因此凡是有 ``cost_exact`` 的观测，**必须**同时给出一个
       **平台无关的 opaque 口径标识**，例如：

           money:USD
           money:CNY
           provider-credit:openai
           provider-credit:some-vendor

       Core **不需要理解**美元 / 人民币 / 某供应商 credit 的业务含义，
       只需要比较同一个 opaque key 是否一致。

       ``cost_exact`` 与 ``cost_accounting_key`` 必须**同生同灭**：
       有金额没口径 → 拒绝（无法判断可比性）；
       有口径没金额 → 拒绝（口径只用来解释金额）。
    """

    source: ConfirmationSource
    confidence: Confidence
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_exact: float | None = None
    cost_magnitude: CostMagnitude | None = None
    duration_ms: int | None = None
    retries: int | None = None
    cost_accounting_key: str | None = None

    def __post_init__(self) -> None:
        _require_enum(self.source, ConfirmationSource, "source")
        _require_enum(self.confidence, Confidence, "confidence")

        observed = self.observed
        if not observed:
            # 未观测：先给最具体的错误，再谈口径
            if self.cost_exact is not None:
                raise ContractError("未观测到任何用量时 cost_exact 必须为 None")
            if self.cost_accounting_key is not None:
                raise ContractError(
                    "未观测到任何用量时不得给出 cost_accounting_key"
                )
            if self.source is not ConfirmationSource.UNAVAILABLE:
                raise ContractError(
                    "未观测到任何用量时 source 必须是 UNAVAILABLE，"
                    f"收到 {self.source!r}"
                )
            return

        # 已观测：来源不得是 UNAVAILABLE（否则"测到了但不知道从哪来"）
        if self.source is ConfirmationSource.UNAVAILABLE:
            raise ContractError("有实际用量时 source 不得为 UNAVAILABLE")

        # 口径标识与精确金额同生同灭
        if self.cost_exact is not None:
            _require_non_empty_str(self.cost_accounting_key, "cost_accounting_key")
        elif self.cost_accounting_key is not None:
            raise ContractError(
                "没有 cost_exact 时不得给出 cost_accounting_key"
                "（口径只用来解释金额）"
            )

        for name in ("tokens_in", "tokens_out", "duration_ms", "retries"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise ContractError(f"{name} 必须是 int | None")
                if value < 0:
                    raise ContractError(f"{name} 不得为负")
        if self.cost_exact is not None:
            _require_number(self.cost_exact, "cost_exact")
            if self.cost_exact < 0:
                raise ContractError("cost_exact 不得为负")
        if self.cost_magnitude is not None:
            _require_enum(self.cost_magnitude, CostMagnitude, "cost_magnitude")

    @property
    def observed(self) -> bool:
        """是否观测到了任何实际数据。

        ``cost_exact=0`` 或 ``tokens_in=0`` **算观测到**；全为 ``None`` 才算没测到。
        """
        return any(
            value is not None
            for value in (
                self.tokens_in,
                self.tokens_out,
                self.cost_exact,
                self.cost_magnitude,
                self.duration_ms,
                self.retries,
            )
        )

    @classmethod
    def unobserved(cls, note_confidence: Confidence = Confidence.LOW) -> "CostActual":
        """构造"无法可靠确认"的合法形态（架构 6.2.2）。"""
        return cls(
            source=ConfirmationSource.UNAVAILABLE,
            confidence=note_confidence,
        )


# --------------------------------------------------------------------------
# [15b] Cost Ledger
# --------------------------------------------------------------------------


class LedgerStage(Enum):
    """埋点位置（架构 2 节 [15b]）：[06] [10] [11] [14]。

    ⚠️ stage 只是**遥测来源**，不携带完整成本事实。
       ``[06]`` / ``[10]`` / ``[14]`` 大多天生没有真实费用。
       因此 **stage 不能充当 coverage 的分母**（第 2 步审查裁决 C）：
       coverage 的单位是 ``(branch_id, attempt)``，见 ``CostSummary``。
    """

    MODEL_ROUTER = "model_router"
    AGENT_MANAGER = "agent_manager"
    ADAPTER = "adapter"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class LedgerEntry:
    """[15b] 一条遥测记录。

    ⚠️ **每条记录必须归属 run_id / branch_id / attempt**，否则成本账会跨任务串号
       （架构 4.5）。attempt 维度必须存在，因此 [15a] 的基数也必须是
       attempt 级而不是 branch 级。

    ⚠️ INV-06：Ledger 只观测，**不拥有 stop / retry / rebuild 控制权**。
       契约里因此没有任何控制字段。
    """

    run_id: RunId
    branch_id: BranchId
    stage: LedgerStage
    attempt: int
    planned: CostEstimate
    requested: CostRequest | None
    actual: CostActual | None

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        validate_branch_id(self.branch_id)
        _require_enum(self.stage, LedgerStage, "stage")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise ContractError("attempt 必须是 int")
        if self.attempt < 1:
            raise ContractError("attempt 从 1 开始计数（架构 9.1）")
        if not isinstance(self.planned, CostEstimate):
            raise ContractError("planned 必须是 CostEstimate")
        if self.requested is not None and not isinstance(self.requested, CostRequest):
            raise ContractError("requested 必须是 CostRequest | None")
        if self.actual is not None and not isinstance(self.actual, CostActual):
            raise ContractError("actual 必须是 CostActual | None")

    @property
    def observed(self) -> bool:
        return self.actual is not None and self.actual.observed

    @property
    def is_unknown(self) -> bool:
        """没测到 —— 与"测到 0"完全不同（INV-07）。"""
        return not self.observed

    @property
    def is_additive(self) -> bool:
        """是否可加。

        架构 9.2 边界 2：只有**可加**的观测才能求精确和
        （token 费用可加；时长不可加；跨平台口径不同不可加）。
        """
        return self.observed and self.actual is not None and self.actual.cost_exact is not None


# --------------------------------------------------------------------------
# [15c] Cost Summary
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CostUnitSummary:
    """一个 **coverage unit** 的成本汇总 —— 严格对应一个 ``(branch_id, attempt)``。

    第 2 步审查裁决（最后三项之 ②）：cost coverage unit 是
    ``(branch_id, attempt)``，因此汇总单位**必须携带 attempt**。

    否则下面的两次尝试在 Summary 之后就没有独立身份：

        branch A / attempt 1 / $0.20
        branch A / attempt 2 / $0.35

    ⚠️ ``observed`` 与 ``confirmed_cost_exact`` **是两件事**：

        ``CostActual`` 允许"有 token / 有 duration / 有 magnitude，
        但没有 cost_exact" —— 那仍然是**观测到了实际数据**，不是 unknown。
        因此 ``observed=True`` 允许 ``confirmed_cost_exact=None``，
        此时必须给出 ``magnitudes``。

        只有 ``variance_exact``（估算 vs 实际差额）才依赖 exact cost。
    """

    branch_id: BranchId
    attempt: int
    planned: CostEstimate
    observed: bool
    magnitudes: tuple[CostMagnitude, ...]
    confirmed_cost_exact: float | None = None
    variance_exact: float | None = None
    cost_accounting_key: str | None = None

    def __post_init__(self) -> None:
        validate_branch_id(self.branch_id)
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise ContractError("attempt 必须是 int")
        if self.attempt < 1:
            raise ContractError("attempt 从 1 开始计数（架构 9.1）")
        if not isinstance(self.planned, CostEstimate):
            raise ContractError("planned 必须是 CostEstimate")
        if not isinstance(self.observed, bool):
            raise ContractError("observed 必须是 bool")
        if not isinstance(self.magnitudes, tuple):
            raise ContractError("magnitudes 必须是 tuple")
        for magnitude in self.magnitudes:
            _require_enum(magnitude, CostMagnitude, "magnitudes 元素")

        # 口径标识与精确金额同生同灭（与 CostActual 同一规则）
        if not self.observed:
            # 未观测：先给最具体的错误，再谈口径
            if self.confirmed_cost_exact is not None:
                raise ContractError("未观测的 unit 不得给出精确值")
            if self.variance_exact is not None:
                raise ContractError("未观测的 unit 不得给出差额")
            if self.magnitudes:
                raise ContractError("未观测的 unit 不得给定量级")
            if self.cost_accounting_key is not None:
                raise ContractError("未观测的 unit 不得给出 cost_accounting_key")
            return

        if self.confirmed_cost_exact is not None:
            _require_non_empty_str(self.cost_accounting_key, "cost_accounting_key")
        elif self.cost_accounting_key is not None:
            raise ContractError(
                "没有 confirmed_cost_exact 时不得给出 cost_accounting_key"
                "（口径只用来解释金额）"
            )

        # 已观测：必须有**某种**可展示的结论（精确值 或 量级）
        if self.confirmed_cost_exact is None and not self.magnitudes:
            raise ContractError(
                "observed=True 时必须给出 confirmed_cost_exact 或 magnitudes"
                "（观测到了数据却什么都不报，等于把已确认信息丢掉）"
            )
        if self.confirmed_cost_exact is not None:
            _require_number(self.confirmed_cost_exact, "confirmed_cost_exact")
            if self.confirmed_cost_exact < 0:
                raise ContractError("confirmed_cost_exact 不得为负")
        if self.variance_exact is not None:
            if self.confirmed_cost_exact is None:
                raise ContractError(
                    "没有 confirmed_cost_exact 就算不出 variance_exact（差额依赖精确成本）"
                )
            _require_number(self.variance_exact, "variance_exact")

    @property
    def unit_key(self) -> tuple[str, int]:
        """coverage unit 身份：``(branch_id, attempt)``。"""
        return (self.branch_id, self.attempt)

    @property
    def has_exact_cost(self) -> bool:
        """是否观测到了精确成本（不只是量级）。"""
        return self.observed and self.confirmed_cost_exact is not None

    @property
    def is_observed_but_inexact(self) -> bool:
        """观测到了数据，但只有量级、没有精确金额。

        这正是第 2 步审查要求表达的合法状态。
        """
        return self.observed and self.confirmed_cost_exact is None

    @property
    def is_additive(self) -> bool:
        """本 unit 是否提供了**可求和**的精确成本。

        ⚠️ 仅仅"有 ``cost_exact``"**不足以**判定可加 ——
           还必须有**可比的成本口径标识** ``cost_accounting_key``
           （架构 9.2 边界 2：跨平台口径不同不可加）。

           跨口径是否真的可比由 ``CostSummary`` 在汇总层判断
           （要求所有 unit 的 key 一致）；本属性只保证
           "本 unit 自身带上了可比的标识"。
        """
        return self.has_exact_cost and self.cost_accounting_key is not None


@dataclass(frozen=True)
class CostSummary:
    """[15c] 每个 RoutingRun 一次的汇总。

    ⚠️ **纯聚合，不产生新事实。**

    第 2 步审查裁决（最后三项之 ③）：**能派生的就派生。**

    ``coverage`` / ``cost_units_total`` / ``cost_units_observed`` /
    ``confident_units_additive`` / ``confirmed_cost_exact`` /
    ``complete_total_exact`` **全部由 ``units`` 机械核算**，
    调用方**没有机会填写**。

    这样就机械地排除了"unit 合计 $0.55 却报 $999"这类不聚合的总额 ——
    它们不再是一段需要靠校验拦住的代码，而是**根本不存在这样的入参**。

    INV-18 的契约化：
      未知覆盖时，精确值字段名必须含"已确认"语义 →
      ``confirmed_cost_exact``（已确认部分，可以精确）
      ``complete_total_exact``（完整总成本，**coverage < 1 时恒为 None**）

    architecture 9.2 覆盖度分级（全部为派生结果）：
      ``coverage = 0``            → 不得给任何金额（``confirmed_cost_exact`` 为 None）
      ``0 < c < 0.5``             → 可展示"已确认部分"；整体只给 low/medium/high
      ``0.5 <= c < 1``            → 可展示已确认成本精确值；**必须同时显示 coverage**；
                                    完整总成本仍标 unknown
      ``c = 1`` 且所有 unit 可加   → 才允许报告完整精确总成本

    ⚠️ **coverage 的单位**：一个实际执行的 ``(branch_id, attempt)``，
       **不是 Ledger 行数**。Ledger 的 4 个 stage
       （``[06]``/``[10]``/``[11]``/``[14]``）大多是遥测来源，天生不携带真实费用。
       若以 Ledger 行为分母：一次 attempt 产生 4 条 telemetry、只有 Adapter
       那条有真实费用 → 得 25%，而该 attempt 其实 **100% 已被观测**。

       ``branch_id`` 取值域 = ``RoutingPlan.execution_unit_ids``
       （主线程 + 各 child branch）。

    ⚠️ **coverage=1 但口径不可加，是合法状态**：
       含义是"**全部知道，但不能相加**"，不是 unknown。例：
       分支 A 美元费用完整 / 分支 B 本地 GPU 秒完整 / 分支 C 只给 token ——
       coverage 100%，但三个口径不能加。
       这与 ``observed ≠ exact`` 是同一个哲学：**观测完整 ≠ 可以求和**。

    ⚠️ **跨口径精确金额一律不可加**（架构 9.2 边界 2）：
       ``USD 0.20 + EUR 0.30 + credit 0.50`` **不得**产生 ``0.50`` 这种假精确值。
       判定依据是各 unit 的 ``cost_accounting_key`` 是否**完全一致**：

           unit A = 0.20 USD  / unit B = 0.30 USD   → 可加，总 $0.50
           unit A = 0.20 USD  / unit B = 0.30 EUR   → **不可加**
           unit A = 0.20 USD  / unit B = 0.30 credit→ **不可加**

       Core 不解释 key 的业务含义，只比较一致性（见 ``is_cross_denomination``）。
    """

    run_id: RunId
    units: tuple[CostUnitSummary, ...]
    complete_total_magnitude: CostMagnitude
    confidence: Confidence
    notes: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if not isinstance(self.units, tuple):
            raise ContractError("units 必须是 tuple（不可变契约）")
        for unit in self.units:
            if not isinstance(unit, CostUnitSummary):
                raise ContractError(f"units 元素必须是 CostUnitSummary，收到 {unit!r}")
        if not isinstance(self.notes, tuple):
            raise ContractError("notes 必须是 tuple")
        _require_enum(
            self.complete_total_magnitude, CostMagnitude, "complete_total_magnitude"
        )
        _require_enum(self.confidence, Confidence, "confidence")

        # coverage unit 身份必须唯一：一个 (branch_id, attempt) 只能出现一次
        keys = [unit.unit_key for unit in self.units]
        if len(set(keys)) != len(keys):
            duplicates = sorted({k for k in keys if keys.count(k) > 1})
            raise ContractError(
                f"coverage unit 身份必须唯一，出现重复 {duplicates}"
                "（一个 (branch_id, attempt) 只能有一条汇总）"
            )

        # ---- 覆盖度分级强制（INV-14 / 架构 9.2）----
        # 全部为派生值，所以只需检查"合法状态组合"，无需再核对数字一致性。
        if self.coverage < COVERAGE_QUALITATIVE_THRESHOLD:
            if self.complete_total_exact is not None:
                raise ContractError(
                    f"coverage < {COVERAGE_QUALITATIVE_THRESHOLD} 时整体成本只能给定性结论，"
                    "不得给出完整总成本精确值"
                )

        if self.coverage == 0.0 and self.confirmed_cost_exact is not None:
            raise ContractError("coverage=0 时不得给出任何精确金额")

        if self.coverage < 1.0:
            if self.complete_total_exact is not None:
                raise ContractError(
                    "coverage<1 时不得给出完整总成本精确值"
                    "（不得把已观测部分冒充成完整总成本，L55）"
                )
            if not self.notes:
                raise ContractError(
                    "覆盖不完整时必须给出 notes（如『N 个 unit 无法可靠获取用量』）"
                )

        if self.coverage == 1.0 and self.confident_units_additive:
            if self.complete_total_exact is None:
                raise ContractError("coverage=1 且口径可加时必须给出完整精确总成本")
        if self.coverage == 1.0 and not self.confident_units_additive:
            if self.complete_total_exact is not None:
                raise ContractError(
                    "coverage=1 但 coverage unit 口径不可加时，"
                    "不得给出完整精确总成本（架构 9.2 边界 2）"
                )

    # ------------------------------------------------------------------
    # 全部为派生值 —— 调用方无法填写
    # ------------------------------------------------------------------

    @property
    def cost_units_total(self) -> int:
        """coverage 分母：``(branch_id, attempt)`` 总数。"""
        return len(self.units)

    @property
    def cost_units_observed(self) -> int:
        """coverage 分子：已观测的 unit 数。"""
        return sum(1 for unit in self.units if unit.observed)

    @property
    def coverage(self) -> float:
        """coverage = 已观测 unit / 总 unit。无 unit 时为 0.0。"""
        total = self.cost_units_total
        if total == 0:
            return 0.0
        return self.cost_units_observed / total

    @property
    def confident_units_additive(self) -> bool:
        """所有**已观测**的 unit 是否提供了**可求和**的精确成本。

        两个条件**同时**成立才算可加（架构 9.2 边界 2）：

          ① 每个已观测 unit 都有精确成本 **且** 带可比口径标识
          ② **所有**口径标识相同（USD + EUR、USD + credit 一律不可加）

        只要有一个已观测 unit 只有量级、缺口径，或口径不一致，
        就不能声称"可以求和"。
        """
        observed_units = self._observed_units
        if not observed_units:
            return False
        if not all(unit.is_additive for unit in observed_units):
            return False
        return len(self.accounting_keys) == 1

    @property
    def accounting_keys(self) -> frozenset[str]:
        """全部已观测 unit 携带的口径标识集合。

        长度为 1 表示口径一致（可加）；大于 1 表示跨口径，不可加。
        """
        return frozenset(
            unit.cost_accounting_key
            for unit in self._observed_units
            if unit.cost_accounting_key is not None
        )

    @property
    def cost_accounting_key(self) -> str | None:
        """统一的成本口径标识。**仅当口径一致时**才有值。

        跨口径（如 USD + EUR，或 USD + provider credit）时为 ``None`` ——
        Core 从不解释这个 key 的业务含义，只比较它是否一致。
        """
        keys = self.accounting_keys
        if len(keys) != 1:
            return None
        return next(iter(keys))

    @property
    def is_cross_denomination(self) -> bool:
        """是否存在**多个不可比口径**的精确成本。

        架构 9.2 边界 2 明确禁止把不同口径拼成一个假精确值。
        """
        return len(self.accounting_keys) > 1

    @property
    def confirmed_cost_exact(self) -> float | None:
        """**已确认部分**的精确合计（INV-18：名字含"已确认"语义）。

        只有所有已观测 unit 都可加时才有意义 —— 否则"把美元和 GPU 秒加起来"
        会造出一个假的精确数。coverage=0 或口径不可加时为 ``None``。
        """
        if not self.confident_units_additive:
            return None
        return float(sum(unit.confirmed_cost_exact or 0.0 for unit in self._observed_units))

    @property
    def complete_total_exact(self) -> float | None:
        """完整总成本的精确值。

        **仅当 coverage == 1 且所有 unit 口径可加时**存在（架构 9.2）。
        其余情况恒为 ``None`` —— 不可能被调用方填错。
        """
        if self.coverage != 1.0 or not self.confident_units_additive:
            return None
        return float(sum(unit.confirmed_cost_exact or 0.0 for unit in self.units))

    @property
    def planned_total_exact(self) -> float | None:
        """计划侧的可加合计（各 unit 估算区间上界）。无可用区间时为 ``None``。"""
        bounds = [
            unit.planned.range_max
            for unit in self.units
            if unit.planned.is_quantitative
        ]
        if len(bounds) != len(self.units) or not bounds:
            return None
        return float(sum(bounds))

    @property
    def variance_total_exact(self) -> float | None:
        """估算 vs 实际的差额。依赖完整可加，否则为 ``None``。"""
        complete = self.complete_total_exact
        planned = self.planned_total_exact
        if complete is None or planned is None:
            return None
        return complete - planned

    @property
    def is_quantitatively_complete(self) -> bool:
        """是否可报告"完整精确总成本"。"""
        return self.coverage == 1.0 and self.confident_units_additive

    @property
    def is_qualitative_only(self) -> bool:
        """是否只能给定性结论（架构 9.2 的 c < 0.5 区间）。"""
        return self.coverage < COVERAGE_QUALITATIVE_THRESHOLD

    @property
    def is_fully_observed_but_not_additive(self) -> bool:
        """全部 unit 都已观测，但口径不可加，因此不能求和。

        这是**合法**状态：``coverage == 1`` 且 ``confident_units_additive is False``。
        分 unit 的精确值都可用，但不存在一个有意义的"总成本精确值"。
        """
        return self.coverage == 1.0 and not self.confident_units_additive

    @property
    def unobserved_units(self) -> tuple[CostUnitSummary, ...]:
        """无法可靠获取用量的 unit —— 供 notes 与 [16] Reporting 使用。"""
        return tuple(unit for unit in self.units if not unit.observed)

    @property
    def unobserved_unit_keys(self) -> tuple[tuple[str, int], ...]:
        return tuple(unit.unit_key for unit in self.unobserved_units)

    @property
    def _observed_units(self) -> tuple[CostUnitSummary, ...]:
        return tuple(unit for unit in self.units if unit.observed)
