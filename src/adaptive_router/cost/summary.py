"""cost.summary —— [15c] Cost Summary：执行后汇总。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 [15c] Cost Summary
  - 4.5 Cost Y 型流 + RoutingRun 聚合边界
  - 9.2 Coverage 规则

⚠️ 本模块的边界：**纯聚合，不产生新事实。**

   ``coverage`` / 计数 / 精确金额全部由 **_units 机械派生**，
   调用方**没有机会**填写总额（INV-18）。

⚠️ 聚合边界是 **RoutingRun**，不是 Session（架构 4.5）：
   一个会话内可能连续发生多个独立任务，Session 没有可靠的"结束事件"。

⚠️ 进入条件：**只有真正结束执行后才允许汇总。**
   即 Run 的两个收敛条件都满足（分支全终止 + Recovery / Approval 已收敛）。
   失败一次不得开始最终汇总。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts import (
    BranchId,
    Confidence,
    CostEstimate,
    CostMagnitude,
    CostSummary,
    CostUnitSummary,
    RunId,
)

__all__ = ["CostSummaryBuilder", "SummaryNotReady"]


class SummaryNotReady(RuntimeError):
    """Run 尚未收敛时试图汇总。

    架构 4.5：RoutingRun 必须**同时**满足
    「① 所属分支均已终止」与「② Recovery / Approval 等控制流均已收敛」，
    才允许进入 [15c] Cost Summary。
    **失败一次不得开始最终汇总。**

    ⚠️ 真实的 convergence source（由 ``RoutingRun.is_converged`` 自动驱动
       ``mark_converged()``）可以后接；但 **build-before-convergence
       现在就必须不可能**。
    """


@dataclass
class CostSummaryBuilder:
    """按 coverage unit 收集成本事实，最后机械汇总。

    ⚠️ 它**不接受** coverage / 总额参数 —— 那些由 ``CostSummary`` 派生。
       本类只负责收集 unit 与生成覆盖不全时的 notes。
    """

    run_id: RunId
    _units: dict[tuple[BranchId, int], CostUnitSummary] = field(
        default_factory=dict, repr=False
    )
    _converged: bool = field(default=False, repr=False)

    # ------------------------------------------------------------------
    # 收集
    # ------------------------------------------------------------------

    def note_unit(
        self,
        *,
        branch_id: BranchId,
        attempt: int,
        planned: CostEstimate,
        observed: bool,
        confirmed_cost_exact: float | None,
        cost_accounting_key: str | None = None,
        magnitudes: tuple[CostMagnitude, ...] = (),
    ) -> CostUnitSummary:
        """记录一个 coverage unit ``(branch_id, attempt)`` 的成本事实。

        同一 ``(branch_id, attempt)`` 只允许记录一次 —— 身份必须唯一。
        """
        key = (branch_id, attempt)
        if key in self._units:
            raise ValueError(
                f"coverage unit 身份 {key!r} 已存在；"
                "一个 (branch_id, attempt) 只能有一条汇总"
            )
        unit = CostUnitSummary(
            branch_id=branch_id,
            attempt=attempt,
            planned=planned,
            observed=observed,
            magnitudes=magnitudes,
            confirmed_cost_exact=confirmed_cost_exact,
            cost_accounting_key=cost_accounting_key,
        )
        self._units[key] = unit
        return unit

    def note_from_ledger(self, ledger, run_id: RunId | None = None) -> None:
        """从 Ledger 按 coverage unit 归集（便捷入口）。

        ⚠️ 归集单位是 ``(branch_id, attempt)``，**不是 Ledger 行数** ——
           Ledger 的 4 个 stage 大多只是遥测来源，天生不携带真实费用。
        """
        target_run = run_id or self.run_id
        entries = ledger.entries_for(target_run)

        planned_by_unit: dict[tuple[BranchId, int], CostEstimate] = {}
        exact_by_unit: dict[tuple[BranchId, int], tuple[float, str]] = {}
        magnitude_by_unit: dict[tuple[BranchId, int], list[CostMagnitude]] = {}

        for entry in entries:
            key = (entry.branch_id, entry.attempt)
            planned_by_unit.setdefault(key, entry.planned)
            if entry.actual is None:
                continue
            if entry.actual.cost_exact is not None and entry.actual.cost_accounting_key:
                exact_by_unit[key] = (
                    entry.actual.cost_exact,
                    entry.actual.cost_accounting_key,
                )
            if entry.actual.cost_magnitude is not None:
                magnitude_by_unit.setdefault(key, []).append(entry.actual.cost_magnitude)

        for key in planned_by_unit:
            if key in self._units:
                continue
            exact = exact_by_unit.get(key)
            magnitudes = tuple(magnitude_by_unit.get(key, ()))
            self.note_unit(
                branch_id=key[0],
                attempt=key[1],
                planned=planned_by_unit[key],
                observed=exact is not None or bool(magnitudes),
                confirmed_cost_exact=exact[0] if exact else None,
                cost_accounting_key=exact[1] if exact else None,
                magnitudes=magnitudes,
            )

    def mark_converged(self) -> None:
        """标记 RoutingRun 已收敛（架构 4.5 两个条件都满足）。

        ⚠️ 真实的自动来源（``RoutingRun.is_converged`` 由 Approval / Recovery
           状态驱动）**可以后接**；接上之前，这个显式接口是唯一合法途径，
           而 ``build()`` 在未调用它之前一律拒绝。
        """
        self._converged = True

    # ------------------------------------------------------------------
    # 汇总
    # ------------------------------------------------------------------

    def build(
        self,
        *,
        complete_total_magnitude: CostMagnitude,
        confidence: Confidence = Confidence.LOW,
        extra_notes: tuple[str, ...] = (),
    ) -> CostSummary:
        """产出 ``CostSummary``。

        ⚠️ **convergence gate（架构 4.5）**：RouteingRun 未收敛时必须拒绝。

           ① 所属分支均已终止
           ② Recovery / Approval 等控制流均已收敛

           两者同时满足才允许进入 [15c]。**失败一次不得开始最终汇总。**

        ⚠️ 这里**没有** coverage / 金额参数 —— 全部由 units 派生。
        """
        if not self._converged:
            raise SummaryNotReady(
                "RoutingRun 尚未收敛，不得进入 [15c] Cost Summary"
                "（架构 4.5 条件 ① 分支全终止 + 条件 ② Recovery/Approval 收敛）；"
                "请先调用 mark_converged()"
            )

        units = tuple(self._units.values())
        notes = tuple(self._coverage_notes(units)) + tuple(extra_notes)

        return CostSummary(
            run_id=self.run_id,
            units=units,
            complete_total_magnitude=complete_total_magnitude,
            confidence=confidence,
            notes=notes,
        )

    @staticmethod
    def _coverage_notes(units: tuple[CostUnitSummary, ...]) -> list[str]:
        """覆盖不完整时自动生成说明，不用调用方手写。"""
        if not units:
            return ["no coverage units recorded"]
        missing = [unit.unit_key for unit in units if not unit.observed]
        if not missing:
            return []
        return [
            f"{len(missing)} of {len(units)} coverage units could not be "
            "reliably measured; affected units: "
            + ", ".join(f"{branch}@attempt{attempt}" for branch, attempt in missing)
        ]

    # ------------------------------------------------------------------

    @property
    def is_converged(self) -> bool:
        return self._converged

    @property
    def unit_count(self) -> int:
        return len(self._units)

    @property
    def unit_keys(self) -> tuple[tuple[BranchId, int], ...]:
        return tuple(self._units)
