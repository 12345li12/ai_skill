"""``[14] → [15b]/[15c]`` 的 **C1 成本观测边**（接线层）。

架构依据：`docs/architecture.md`

- 2 节 ``[15b] Cost Ledger / Telemetry``（PERCEPTION）：**不是流水线阶段**，是 Execution 之后的
  观测入口；**只观测**，不拥有 stop / retry / rebuild 控制权（``INV-06``）；作用是
  **Session 生命周期内存在**；每条记录必须归属 ``run_id`` / ``branch_id`` / ``attempt``；
- 2 节 ``[15c] Cost Summary``（FEEDBACK）：**纯聚合，不产生新事实**；聚合边界 = **RoutingRun**；
  进入条件 = **4.5 双条件**（分支全终止 ∧ 控制流收敛）；
- 4.5 Cost Y 型流：Ledger **不在主线上**，是订阅者；
- 9.2 Coverage 规则：coverage 单位是 ``(branch_id, attempt)``，**不是 Ledger 行数**
  （``LedgerStage`` 已被裁决**不得充当分母**）；
- ``INV-07`` unknown ≠ 0 · ``INV-14`` Summary 必须披露 coverage · ``INV-18`` 精确值命名 ·
  ``INV-S3`` Ledger 不得导入控制接口。

范围、锁与变异见 `tests/behavior/README_step5b.md` **§103**（scope review）· **§104**（冻结）·
**§105**（实现授权）· **§106**（实现记录）。

**三条职责（本边只做这三件事）**：

```text
record_observation    观测七字段 ──► TelemetryObservation ──► CostLedger.record      （[15b] 记账）
note_expected_units   显式分母 + 账本事实 ──► builder 的 coverage unit 全覆盖          （[15c] 收集）
build_run_summary     [10] 的只读收敛事实 ──► 机械 gate ──► builder.build             （[15c] 汇总）
```

**六条口径（§103.2 冻结，逐条对应 ``LCS`` 锁）**：

1. **只搬运**：不生产事实、不推导 facts、不构造 ``CostEstimate`` / ``CostActual`` /
   ``CostUnitSummary`` / ``CostSummary``（``LCS-4``）；
2. **双注入**：账本（Session 级）与 builder（每 RoutingRun 一份）**都由调用方注入** ——
   本边**不构造**任何一个（``LCS-1``：一次调用 = 一次新账本会让会话内账本断裂）；
3. **分母显式**：expected units 由调用方给出；账本里没有的 unit 记为**未观测**并**返回**
   （``LCS-4`` 的返回形态）——「给多少覆盖多少，缺的绝不静默当成已完成」；
4. **收敛事实只读**：gate 只认 ``routing_run.is_converged``（由 ``[10]`` 的 ``close_run``
   唯一写入）；为假 ⇒ **显式失败**（``LCS-3``）；
5. **``unknown ≠ 0``**：``None`` 一律**不折算**（``LCS-5``）；
6. **观测者无控制权**：不 import ``agent_manager`` / ``scheduler`` / ``adapters`` /
   ``recovery`` / ``execution``（``LCS-6`` / ``LCS-9``）。

**明确不做（越界即缺陷）**：❌ 不实现四个 stage 的真实埋点（producer 侧，本轮不做）·
❌ 不做 C2 的 ``route_to`` 控制回流 · ❌ **不触发自己**（``build`` 由编排点调用）·
❌ 不写任何运行态 · ❌ 不复制 ``[15a]`` / ``[15b]`` / ``[15c]`` 的既有规则（一律复用）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import (
    BranchId,
    Confidence,
    CostActual,
    CostEstimate,
    CostMagnitude,
    CostRequest,
    CostSummary,
    LedgerEntry,
    LedgerStage,
    RoutingRun,
    RunId,
)
from .ledger import CostLedger, TelemetryObservation
from .summary import CostSummaryBuilder

__all__ = [
    "CostEdgeWiringError",
    "ExpectedUnit",
    "build_run_summary",
    "note_expected_units",
    "record_observation",
]


class CostEdgeWiringError(RuntimeError):
    """C1 接线级显式失败：**不是**既有组件守卫的替代品。

    ⚠️ ``CostLedger`` / ``CostSummaryBuilder`` / 各成本契约的构造期校验**原样生效**；
    本异常只覆盖**接线自身**的前置条件（对象不是本边的契约载体、Run 未收敛）。
    ⚠️ 它**不得**被降级成"给一个默认事实"（``0.0`` / ``observed=True`` / 直接放过 gate）——
    那会把接线缺陷写成成本事实。
    """


@dataclass(frozen=True)
class ExpectedUnit:
    """**coverage 分母**的一格：一个应当被记账的执行单位 ``(branch_id, attempt)``。

    ⚠️ 它是**纯载体**：本边**不合成** expected units，也**不从运行态反推**它们
    （§103.5：owner = 调用方；``[10]`` 只提供终态事实，不承担业务覆盖全集定义）。
    ⚠️ ``planned`` 来自 ``[15a]``，由调用方给出；本边**不重算**（``INV-05``）。
    ⚠️ 本类**不做二次值校验** —— 规则只有一份：身份 / ``attempt`` / ``planned`` 的类型
    由 ``CostUnitSummary``（``note_unit`` 的构造期）与 ``LedgerEntry`` 强制。
    """

    branch_id: BranchId
    attempt: int
    planned: CostEstimate


def _require_ledger(ledger: object, *, entry: str) -> CostLedger:
    """``ledger`` 身份守卫（**注入**的 Session 级账本）。"""
    if not isinstance(ledger, CostLedger):
        raise CostEdgeWiringError(
            f"{entry} 的 ledger 必须是 CostLedger（Session 级、由调用方注入），收到 {ledger!r}"
        )
    return ledger


def _require_builder(builder: object, *, entry: str) -> CostSummaryBuilder:
    """``builder`` 身份守卫（**注入**的每 RoutingRun 一份的汇总器）。"""
    if not isinstance(builder, CostSummaryBuilder):
        raise CostEdgeWiringError(
            f"{entry} 的 builder 必须是 CostSummaryBuilder（每个 RoutingRun 一份、"
            f"由调用方注入），收到 {builder!r}"
        )
    return builder


def _require_routing_run(routing_run: object, *, entry: str) -> RoutingRun:
    """``routing_run`` 身份守卫：本边只接受 ``[10]`` 的**只读投影值**（不是管理器）。"""
    if not isinstance(routing_run, RoutingRun):
        raise CostEdgeWiringError(
            f"{entry} 的 routing_run 必须是 RoutingRun（[10] 的只读投影），收到 {routing_run!r}"
        )
    return routing_run


def _require_expected_units(
    expected_units: object, *, entry: str
) -> tuple[ExpectedUnit, ...]:
    """分母守卫：必须是 ``ExpectedUnit`` 的 **tuple**（不可变，且逐元素验身份）。"""
    if not isinstance(expected_units, tuple):
        raise CostEdgeWiringError(
            f"{entry} 的 expected_units 必须是 tuple[ExpectedUnit, ...]，"
            f"收到 {expected_units!r}"
        )
    for unit in expected_units:
        if not isinstance(unit, ExpectedUnit):
            raise CostEdgeWiringError(
                f"{entry} 的 expected_units 元素必须是 ExpectedUnit，收到 {unit!r}"
            )
    return expected_units


def record_observation(
    *,
    ledger: CostLedger,
    run_id: RunId,
    branch_id: BranchId,
    stage: LedgerStage,
    attempt: int,
    planned: CostEstimate,
    requested: CostRequest | None,
    actual: CostActual | None,
) -> LedgerEntry:
    """把**一次遥测观测**原样搬进 ``[15b]``：``TelemetryObservation`` → ``CostLedger.record``。

    ⚠️ 七个字段**全部来自形参**（同名透传）：本边**不发明** stage、**不补**字段、
    **不把 ``None`` 折算成 0** —— ``actual=None`` 是"未观测到"这条**事实**
    （``INV-07``：与"测到 0"永不等价）。
    ⚠️ 这也是**唯一** ``record`` 调用点（``LCS-2``）：producer 侧的真实埋点不在本轮，
    本轮只提供这条**搬运缝**。
    """
    checked = _require_ledger(ledger, entry="record_observation")
    observation = TelemetryObservation(
        run_id=run_id,
        branch_id=branch_id,
        stage=stage,
        attempt=attempt,
        planned=planned,
        requested=requested,
        actual=actual,
    )
    return checked.record(observation)


def note_expected_units(
    *,
    builder: CostSummaryBuilder,
    ledger: CostLedger,
    run_id: RunId,
    expected_units: tuple[ExpectedUnit, ...],
) -> tuple[tuple[BranchId, int], ...]:
    """按**显式分母**把 ``[15c]`` 的 coverage unit 收齐，并返回**未观测**的 unit 身份。

    顺序与口径（§105.3 的防假绿要点，行为测试锁定）：

    1. 先复用**冻结**的 ``note_from_ledger`` —— 账本里出现过的 unit（含被拒单位的观测）
       按 ``(branch_id, attempt)`` 归集，其 ``planned`` 用**账本那条**（观测到的事实）；
    2. 再按**显式分母**补记**账本里没有**的 unit：``observed=False``、无精确值、无量级
       —— **绝不**用 ``0.0`` 或"已完成"填充；
    3. 补记前用 ``builder.unit_keys`` 做**幂等判断** ⇒ 不重复记同一 unit（既避免
       ``note_unit`` 的重复身份 ``ValueError``，也避免把**已有观测**覆盖成"未观测"）。

    ⚠️ 本函数**不保证分母的正确性**（§103.12 已登记）：它只保证"给多少覆盖多少、
    缺的**显式返回**"。分母少给是调用方的责任 —— 本边不越权替它定义全集。
    """
    checked_builder = _require_builder(builder, entry="note_expected_units")
    checked_ledger = _require_ledger(ledger, entry="note_expected_units")
    units = _require_expected_units(expected_units, entry="note_expected_units")

    checked_builder.note_from_ledger(ledger=checked_ledger, run_id=run_id)

    noted = set(checked_builder.unit_keys)
    unobserved: list[tuple[BranchId, int]] = []
    for unit in units:
        key = (unit.branch_id, unit.attempt)
        if key in noted:
            continue
        checked_builder.note_unit(
            branch_id=unit.branch_id,
            attempt=unit.attempt,
            planned=unit.planned,
            observed=False,
            confirmed_cost_exact=None,
            cost_accounting_key=None,
            magnitudes=(),
        )
        noted.add(key)
        unobserved.append(key)
    return tuple(unobserved)


def build_run_summary(
    *,
    builder: CostSummaryBuilder,
    routing_run: RoutingRun,
    complete_total_magnitude: CostMagnitude,
    confidence: Confidence,
    extra_notes: tuple[str, ...],
) -> CostSummary:
    """**机械 gate**：仅当 ``[10]`` 的收敛事实为真时，才允许进入 ``[15c]`` 并产出汇总。

    ⚠️ 收敛事实**只读**（``routing_run.is_converged`` = ``terminal`` ∧ 分支全终止 ∧
    ``control_flow_converged``）：本边**不推断、不默认、不自行判收敛**；
    为假 ⇒ ``CostEdgeWiringError``（架构 4.5：**失败一次不得开始最终汇总**）。
    ⚠️ ``mark_converged()`` 在本模块内**只有这一处**调用点（``LCS-3``）——
    它把 ``[15c]`` 的手动旗标换成**由真实事实驱动的门**。
    ⚠️ ``build(...)`` 的**触发时机**属**编排点**（§103.5）：本边只提供 carrier，不自触发。
    """
    checked_builder = _require_builder(builder, entry="build_run_summary")
    _require_routing_run(routing_run, entry="build_run_summary")
    if not routing_run.is_converged:
        raise CostEdgeWiringError(
            "RoutingRun 尚未收敛（terminal ∧ 分支全终止 ∧ control_flow_converged），"
            "不得进入 [15c] Cost Summary（架构 4.5：失败一次不得开始最终汇总）"
        )
    checked_builder.mark_converged()
    return checked_builder.build(
        complete_total_magnitude=complete_total_magnitude,
        confidence=confidence,
        extra_notes=extra_notes,
    )
