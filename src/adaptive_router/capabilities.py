"""capabilities —— [03] CapabilityProbe + Snapshot。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 [03] Capability Probe + Snapshot
  - 1.2 PERCEPTION 层

⚠️ 本模块内部**明确拆成两种性质**（架构 2 节 [03]）：

     Probe（探测）   —— 有副作用、昂贵、可失效、**可重入**
     Snapshot（快照）—— 只读、可缓存

   因此：``CapabilityProbe`` 只负责把一次**环境读数**规范化成冻结契约
   ``CapabilitySnapshot``；缓存与重查策略由 ``CapabilityRegistry`` 承担。
   两者分开，是为了让"重查"这件事天然只落在 Probe 一侧。

⚠️ 本模块承载 INV-12 的前半段（真实性）：
   只使用环境**真实暴露**的能力；未知就是未知，**不得把 None 当成 False**。
   L17：无真实子代理时如实说明并自动改为单代理阶段化 —— **不暂停、不模拟分支**。

⚠️ 重查策略（架构 9 节 V1 冻结）：**事件驱动，只重查可能变化的维度，不定时轮询。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

from .contracts import CapabilitySnapshot, EffortLevel, UnavailableReason

__all__ = [
    "CapabilityDimension",
    "RecheckTrigger",
    "FIXED_DIMENSIONS",
    "TRIGGER_DIMENSIONS",
    "EnvironmentReading",
    "ProbeOutcome",
    "CapabilityProbe",
    "CapabilityRegistry",
]


class CapabilityDimension(Enum):
    """L13 的 8 项能力维度。**不得增删。**"""

    SUBAGENTS = "subagents"
    ASYNC_BACKGROUND = "async_background"
    CONCURRENCY_LIMIT = "concurrency_limit"
    AVAILABLE_MODELS = "available_models"
    AVAILABLE_EFFORT_LEVELS = "available_effort_levels"
    STATE_WAIT_APPEND_STOP = "state_wait_append_stop"
    HOT_SWITCH = "hot_switch"
    RELIABLE_COST_DATA = "reliable_cost_data"


#: 与"运行实例生命周期"绑定的维度：环境切换时需要整体重查。
FIXED_DIMENSIONS: frozenset[CapabilityDimension] = frozenset(
    {
        CapabilityDimension.SUBAGENTS,
        CapabilityDimension.ASYNC_BACKGROUND,
        CapabilityDimension.CONCURRENCY_LIMIT,
        CapabilityDimension.STATE_WAIT_APPEND_STOP,
        CapabilityDimension.HOT_SWITCH,
    }
)


class RecheckTrigger(Enum):
    """L15 的 6 个重查触发条件。

    ⚠️ 全部是**事件**，没有时间型触发 —— 架构冻结"不定时轮询"。
    """

    TOOL_FAILURE = "tool_failure"
    MANIFEST_CHANGED = "manifest_changed"
    REQUEST_REJECTED = "request_rejected"
    CONCURRENCY_ANOMALY = "concurrency_anomaly"
    ENVIRONMENT_SWITCHED = "environment_switched"
    SESSION_RESUMED = "session_resumed"


#: 每个触发条件**可能影响**的维度。只重查这些，不重查全部（L15）。
TRIGGER_DIMENSIONS: dict[RecheckTrigger, frozenset[CapabilityDimension]] = {
    # 工具失败：子代理能力与状态操作能力可能已失效
    RecheckTrigger.TOOL_FAILURE: frozenset(
        {
            CapabilityDimension.SUBAGENTS,
            CapabilityDimension.STATE_WAIT_APPEND_STOP,
            CapabilityDimension.HOT_SWITCH,
        }
    ),
    # 清单变化：可请求模型与推理强度清单变了
    RecheckTrigger.MANIFEST_CHANGED: frozenset(
        {
            CapabilityDimension.AVAILABLE_MODELS,
            CapabilityDimension.AVAILABLE_EFFORT_LEVELS,
        }
    ),
    # 请求被拒：模型 / 强度清单与并发上限都不可信了
    RecheckTrigger.REQUEST_REJECTED: frozenset(
        {
            CapabilityDimension.AVAILABLE_MODELS,
            CapabilityDimension.AVAILABLE_EFFORT_LEVELS,
            CapabilityDimension.CONCURRENCY_LIMIT,
        }
    ),
    # 并发异常：并发上限与异步后台能力不可信
    RecheckTrigger.CONCURRENCY_ANOMALY: frozenset(
        {
            CapabilityDimension.CONCURRENCY_LIMIT,
            CapabilityDimension.ASYNC_BACKGROUND,
        }
    ),
    # 环境切换：与实例生命周期绑定的维度整体重查
    RecheckTrigger.ENVIRONMENT_SWITCHED: FIXED_DIMENSIONS,
    # 会话恢复后能力不明：全部重查
    RecheckTrigger.SESSION_RESUMED: frozenset(CapabilityDimension),
}


@dataclass(frozen=True)
class EnvironmentReading:
    """一次环境读数的**原始事实**。

    刻意不设默认值：读数必须显式给出全部 8 项，
    不允许靠默认值蒙混（那样会制造"看似已确认"的假事实）。
    """

    subagents: bool | None
    async_background: bool | None
    concurrency_limit: int | None
    available_models: tuple[str, ...]
    available_effort_levels: tuple[EffortLevel, ...]
    state_wait_append_stop: bool | None
    hot_switch: bool | None
    reliable_cost_data: bool | None
    unavailable_reason: UnavailableReason | None = None


@dataclass(frozen=True)
class ProbeOutcome:
    """探测结论：把能力事实翻译成**对下游的约束**。

    ⚠️ 它不建分支、不选模型、不排计划 —— 只说明"环境允许什么"。
    """

    class ModeHint(Enum):
        """执行方式的能力侧约束。**不是**执行方式决策（那是 [05] Planner）。"""

        NO_CONSTRAINT = "no_constraint"
        STAGED_SINGLE_AGENT = "staged_single_agent"

    class HotSwitchStrategy(Enum):
        APPEND = "append"
        STOP_AND_REBUILD = "stop_and_rebuild"

    mode_hint: "ProbeOutcome.ModeHint"
    hot_switch_strategy: "ProbeOutcome.HotSwitchStrategy"
    must_report_truthfully: bool
    may_simulate_branches: bool
    must_reuse_compressed_results: bool
    may_restart_unstarted_plans: bool


class CapabilityProbe:
    """把一次环境读数规范化成冻结契约 ``CapabilitySnapshot``。

    ⚠️ 有副作用、昂贵、**可重入** —— 这正是它必须与快照缓存分开的原因。
    """

    def __init__(self, *, probed_at: str) -> None:
        self._probed_at = probed_at

    @property
    def probed_at(self) -> str:
        return self._probed_at

    def snapshot(
        self,
        reading: EnvironmentReading,
        *,
        dimensions: Iterable[CapabilityDimension] | None = None,
    ) -> CapabilitySnapshot:
        """构造快照。

        ``dimensions`` 为 ``None`` 时记录全部 8 项为"已探测"；
        否则只记录指定维度 —— 这是"只重查变化部分"的落点。
        """
        probed = (
            tuple(sorted(d.value for d in CapabilityDimension))
            if dimensions is None
            else tuple(sorted(d.value for d in dimensions))
        )
        return CapabilitySnapshot(
            subagents=reading.subagents,
            async_background=reading.async_background,
            concurrency_limit=reading.concurrency_limit,
            available_models=tuple(reading.available_models),
            available_effort_levels=tuple(reading.available_effort_levels),
            state_wait_append_stop=reading.state_wait_append_stop,
            hot_switch=reading.hot_switch,
            reliable_cost_data=reading.reliable_cost_data,
            probed_dimensions=probed,
            unavailable_reason=reading.unavailable_reason,
            probed_at=self._probed_at,
        )

    def evaluate(self, reading: EnvironmentReading) -> ProbeOutcome:
        """把能力事实翻译成对下游的约束（L17）。"""
        # 无真实子代理 → 自动改为单代理阶段化；不暂停、不模拟分支。
        # ⚠️ None（未知）**不得**当作 False 来降级。
        if reading.subagents is False:
            mode_hint = ProbeOutcome.ModeHint.STAGED_SINGLE_AGENT
        else:
            mode_hint = ProbeOutcome.ModeHint.NO_CONSTRAINT

        if reading.hot_switch is True:
            strategy = ProbeOutcome.HotSwitchStrategy.APPEND
            must_reuse = False
        else:
            # False（确认不支持）与 None（未知）都走保守路径：
            # 停旧分支、复用有效压缩结果后重建。
            strategy = ProbeOutcome.HotSwitchStrategy.STOP_AND_REBUILD
            must_reuse = True

        return ProbeOutcome(
            mode_hint=mode_hint,
            hot_switch_strategy=strategy,
            must_report_truthfully=True,
            may_simulate_branches=False,
            must_reuse_compressed_results=must_reuse,
            may_restart_unstarted_plans=False,
        )


@dataclass
class CapabilityRegistry:
    """快照缓存 + 事件驱动的局部重查（L15）。

    ⚠️ **不定时轮询**：只有 ``recheck(trigger)`` 被显式调用才会重新探测。
    ⚠️ 只重查**可能变化的维度**（``TRIGGER_DIMENSIONS``），其余保持原值。
    """

    probe: CapabilityProbe
    loader: Callable[..., EnvironmentReading]
    last_trigger: RecheckTrigger | None = None
    _snapshot: CapabilitySnapshot | None = field(default=None, repr=False)

    def snapshot(self) -> CapabilitySnapshot:
        """读取缓存快照；无缓存时做一次全量探测。"""
        if self._snapshot is None:
            self._snapshot = self._fresh(self._load(set(CapabilityDimension)))
        return self._snapshot

    def recheck(self, trigger: RecheckTrigger) -> CapabilitySnapshot:
        """按触发条件重查**受影响的维度**，其余保持原值。"""
        current = self.snapshot()
        dimensions = TRIGGER_DIMENSIONS[trigger]
        fresh = self._fresh(self._load(set(dimensions)), dimensions=dimensions)
        self._snapshot = self._merge(current, fresh, dimensions)
        self.last_trigger = trigger
        return self._snapshot

    # ------------------------------------------------------------------

    def _load(self, dimensions: set[CapabilityDimension]) -> EnvironmentReading:
        """调用环境读数。

        先尝试带维度参数的精确读数；loader 不接受参数时退化为全量读数
        （调用方需保证其实现本就只反映受影响的维度）。
        """
        try:
            return self.loader(dimensions)
        except TypeError:
            return self.loader()

    def _fresh(
        self,
        reading: EnvironmentReading,
        *,
        dimensions: Iterable[CapabilityDimension] | None = None,
    ) -> CapabilitySnapshot:
        return self.probe.snapshot(reading, dimensions=dimensions)

    @staticmethod
    def _merge(
        current: CapabilitySnapshot,
        fresh: CapabilitySnapshot,
        dimensions: frozenset[CapabilityDimension],
    ) -> CapabilitySnapshot:
        """把重查结果并入旧快照，只覆盖被重查的维度。"""
        merged: dict[str, object] = {
            "subagents": current.subagents,
            "async_background": current.async_background,
            "concurrency_limit": current.concurrency_limit,
            "available_models": current.available_models,
            "available_effort_levels": current.available_effort_levels,
            "state_wait_append_stop": current.state_wait_append_stop,
            "hot_switch": current.hot_switch,
            "reliable_cost_data": current.reliable_cost_data,
        }
        for dimension in dimensions:
            merged[dimension.value] = getattr(fresh, dimension.value)

        return CapabilitySnapshot(
            subagents=merged["subagents"],  # type: ignore[arg-type]
            async_background=merged["async_background"],  # type: ignore[arg-type]
            concurrency_limit=merged["concurrency_limit"],  # type: ignore[arg-type]
            available_models=merged["available_models"],  # type: ignore[arg-type]
            available_effort_levels=merged["available_effort_levels"],  # type: ignore[arg-type]
            state_wait_append_stop=merged["state_wait_append_stop"],  # type: ignore[arg-type]
            hot_switch=merged["hot_switch"],  # type: ignore[arg-type]
            reliable_cost_data=merged["reliable_cost_data"],  # type: ignore[arg-type]
            probed_dimensions=fresh.probed_dimensions,
            unavailable_reason=fresh.unavailable_reason,
            probed_at=fresh.probed_at,
        )
