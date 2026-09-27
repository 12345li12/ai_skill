"""cost.ledger —— [15b] Cost Ledger / Telemetry：执行中记录。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 [15b] Cost Ledger / Telemetry
  - 4.5 Cost Y 型流（Ledger **不在主线上**，是订阅者）

⚠️ 本模块的边界：

    ✅ 只观测，记录事实
    ❌ **不拥有 stop / retry / rebuild 控制权**（INV-06）

   横切观察者一旦获得控制权，就退化成隐藏的第二个 Router。

⚠️ 作用域（架构 2 节 [15b] 冻结）：
   **Session 生命周期内存在**；跨会话复用需显式 opt-in（INV-15）。
   每条记录必须归属 ``run_id / branch_id / attempt``，否则成本账会跨任务串号。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..contracts import (
    BranchId,
    CostActual,
    CostEstimate,
    CostRequest,
    LedgerEntry,
    LedgerStage,
    RunId,
)

__all__ = ["TelemetryObservation", "CostLedger", "LedgerPersistenceProvider"]


class LedgerPersistenceProvider(Protocol):
    """跨会话 Ledger 持久化后端。INV-15：**默认不存在**。"""

    def append(self, entry: LedgerEntry) -> None:  # pragma: no cover - 协议声明
        ...


@dataclass(frozen=True)
class TelemetryObservation:
    """一次**遥测观测**的原始输入。

    刻意与 ``LedgerEntry`` 分开：观测是"看到了什么"，
    条目是"规范化后记进账本的事实"。
    """

    run_id: RunId
    branch_id: BranchId
    stage: LedgerStage
    attempt: int
    planned: CostEstimate
    requested: CostRequest | None
    actual: CostActual | None


@dataclass
class CostLedger:
    """按 ``run_id`` 分账的遥测账本。

    ⚠️ **只写不控**：本类没有任何停止 / 重试 / 重建方法。
    ⚠️ 默认不持久化：``persistence is None`` 是结构事实（INV-15）。
    """

    persistence: LedgerPersistenceProvider | None = None
    _entries: list[LedgerEntry] = field(default_factory=list, repr=False)

    def record(self, observation: TelemetryObservation) -> LedgerEntry:
        """把一次观测规范化成 ``LedgerEntry`` 并记账。"""
        if not isinstance(observation, TelemetryObservation):
            raise TypeError(
                f"observation 必须是 TelemetryObservation，收到 {observation!r}"
            )
        entry = LedgerEntry(
            run_id=observation.run_id,
            branch_id=observation.branch_id,
            stage=observation.stage,
            attempt=observation.attempt,
            planned=observation.planned,
            requested=observation.requested,
            actual=observation.actual,
        )
        self._entries.append(entry)
        return entry

    def entries_for(self, run_id: RunId) -> tuple[LedgerEntry, ...]:
        """某个 RoutingRun 的全部条目（**含失败 / 重试 / 被拒单位**）。"""
        return tuple(entry for entry in self._entries if entry.run_id == run_id)

    def entries_for_unit(self, run_id: RunId, branch_id: BranchId, attempt: int):
        """某个 coverage unit ``(branch_id, attempt)`` 的条目。"""
        return tuple(
            entry
            for entry in self._entries
            if (entry.run_id, entry.branch_id, entry.attempt)
            == (run_id, branch_id, attempt)
        )

    def observed_units(self, run_id: RunId) -> tuple[tuple[BranchId, int], ...]:
        """该 run 中**有实际观测**的 coverage unit 身份集合。

        ⚠️ 一个 unit 只要**任一条** telemetry 观测到实际数据，就算已观测 ——
           这正是"coverage 单位是 (branch_id, attempt)，不是 Ledger 行数"的落点。
        """
        seen: list[tuple[BranchId, int]] = []
        for entry in self.entries_for(run_id):
            if entry.observed:
                key = (entry.branch_id, entry.attempt)
                if key not in seen:
                    seen.append(key)
        return tuple(seen)

    def all_units(self, run_id: RunId) -> tuple[tuple[BranchId, int], ...]:
        """该 run 中**出现过的全部** coverage unit 身份。"""
        seen: list[tuple[BranchId, int]] = []
        for entry in self.entries_for(run_id):
            key = (entry.branch_id, entry.attempt)
            if key not in seen:
                seen.append(key)
        return tuple(seen)

    def bind_persistence(self, provider: LedgerPersistenceProvider) -> None:
        """显式 opt-in 后才绑定跨会话持久化（INV-15）。

        ⚠️ 绑定本身**不写入任何东西**。
        """
        self.persistence = provider

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def clear(self, run_id: RunId | None = None) -> None:
        """丢弃账本内容。仅用于会话边界与测试。"""
        if run_id is None:
            self._entries.clear()
        else:
            self._entries = [e for e in self._entries if e.run_id != run_id]
