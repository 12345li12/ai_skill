"""``completion_wiring`` —— 外部 completion 进入**既有** lifecycle scope 的唯一接线入口。

架构依据：`docs/architecture.md` **A11.4b**（配对事件不可重拼）· **A15.3**（``ResultHandoff``
的消费属调用方）· **A16.1 / A16.3**（``[13]`` 拥有判定权）· **A18.6**（FAIL 侧先关 attempt）·
**``LTW-1`` / ``LTW-4``**（唯一收口点）。

范围、锁清单（``LCW-1…15``）与变异清单（``MCW-1…9``）见 `tests/behavior/README_step5b.md`
**§237 / §238 / §239**。

**本模块只有一件事**：把已经到达 Core 的**已规范化**结果事实，送进**调用方指定的那只**
authoritative ``PassDriver`` 所持有的**同一张**运行态图，走完既有回程链，
并**原样交回**同一次配对见证。

**逐条边界（越界即缺陷）：**

- ⛔ 不解释终态：``passed`` 的**解释权在 ``[13]``**，本层只做**机械二分搬运**；
- ⛔ 不决定恢复 · ⛔ 不进入 ``[14]`` · ⛔ 不产出恢复决策；
- ⛔ 不驱动下一轮（本模块内**没有任何** pass 驱动调用，也⛔ 不判"是否还要一轮"）；
- ⛔ 不缓存任何事实（零实例状态 · 零模块级可变容器 · 零跨调用持有者）；
- ⛔ 不读时钟：``ended_at`` **只透传**（``[10]`` 禁止自己读时钟）；
- ⛔ 不再次物化运行态图 · ⛔ 不构造组合根 · ⛔ 不构造驱动程序；
- ⛔ 不复制下游守卫（规则只有一份：``branch_id`` / ``attempt`` / ``payload`` 的类型与一致性
  各有**唯一**实现，本模块只**搬运**）。

⚠️ **禁止子串扫描**：本文件 docstring 里**刻意不写出**任何禁区名的**调用形**（不带左括号提及），
以免自指式假红 —— 同类教训见 ``adapters/in_memory.py`` 的 ``L11-4`` 记录。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .execution_results import build_branch_return
from .pass_driver import PassDriver
from .terminal_wiring import settle_failure, settle_pass
from .validation import Validator
from .validation_wiring import validate_branch

if TYPE_CHECKING:  # pragma: no cover - 仅供静态检查
    from .contracts import BranchId, BranchReturn
    from .result_handoff import RuntimeResultPayload
    from .validation_judgment import ValidationJudgment
    from .validation_wiring import BranchValidationOutcome

__all__ = ["CompletionWiringError", "accept_completion"]


class CompletionWiringError(RuntimeError):
    """**只**覆盖本层**前置违规**（**恰三类**）。

    ⚠️ **所有 delegated exceptions 原样传播**（`§237.6` ⑤）—— 本异常 ⛔ **不包装**它们中的任何一个；
       尤其 ⛔ 不得把 ``ContractError`` / ``AgentManagerError`` / 接线层异常，
       或注入的 ``judgment_source`` 抛出的**任何**异常，降级成"写一个默认终态 / 默认 PASS"
       （同 `ValidationWiringError` / `TerminalWiringError` 的既有禁令）。
    """


def _require_driver(driver: object) -> PassDriver:
    """① **invalid driver shape** ⇒ 显式失败（先身份，⛔ 不靠 duck typing）。"""
    if not isinstance(driver, PassDriver):
        raise CompletionWiringError(f"driver 必须是 PassDriver，收到 {driver!r}")
    return driver


def _require_ended_at(ended_at: object) -> str:
    """② **invalid ended_at shape** ⇒ 显式失败（非空 str）。

    ⚠️ 本层⛔ 不读时钟 · ⛔ 不猜 · ⛔ 不默认空值 —— ``ended_at`` 只能**显式传入**
       （与 ``settle_pass`` / ``settle_failure`` 的既有纪律同形）。
    """
    if type(ended_at) is not str or not ended_at.strip():
        raise CompletionWiringError(f"ended_at 必须是非空 str（显式传入），收到 {ended_at!r}")
    return ended_at


def _require_judgment_source(source: object) -> Callable[[BranchReturn, str], ValidationJudgment]:
    """③ **non-callable judgment_source** ⇒ 显式失败。"""
    if not callable(source):
        raise CompletionWiringError(f"judgment_source 必须可调用，收到 {source!r}")
    return source


def accept_completion(
    *,
    driver: PassDriver,
    branch_id: BranchId,
    attempt: int,
    payload: RuntimeResultPayload,
    ended_at: str,
    judgment_source: Callable[[BranchReturn, str], ValidationJudgment],
) -> BranchValidationOutcome:
    """把一次外部 completion 送进 ``driver`` 所持有的**同一张**图，并原样交回配对见证。

    ⚠️ 三条本层守卫**全部先于任何被委托调用** —— 尤其 ``ended_at`` 的形状检查**先于**
       ``judgment_source`` 的任何触发（`§237.6` 的顺序硬要求：避免明知最终不能 settle 时，
       **仍先触发一次外部判断能力**）。
    ⚠️ ``driver._graph`` 是本模块**唯一**的 friend read（`γ′`，`§235.3`）；
       ⛔ 本模块 ⛔ 不物化新图、⛔ 不构造组合根、⛔ 不构造驱动程序。
    ⚠️ 返回值 = **本次唯一** ``validate_branch(...)`` 调用产生的 **exact** 配对见证（⛔ 不重建、
       ⛔ 不 copy、⛔ 不二次配对）⇒ 后续 FAIL 侧的恢复入口可以消费**同一个对象**（A11.4b）。
    """
    checked_driver = _require_driver(driver)
    checked_ended_at = _require_ended_at(ended_at)
    checked_source = _require_judgment_source(judgment_source)

    graph = checked_driver._graph

    handoff = graph.compositor.assemble_result_handoff(
        routing=graph.routing,
        branch_id=branch_id,
        attempt=attempt,
        payload=payload,
    )
    branch_return = build_branch_return(handoff=handoff)
    paired = validate_branch(
        plan=graph.routing.plan,
        bundles=graph.bundles,
        branch_return=branch_return,
        validator=Validator(judgment_source=checked_source),
    )
    if paired.validation.passed is True:
        settle_pass(outcome=paired, manager=graph.agent_manager, ended_at=checked_ended_at)
    else:
        settle_failure(outcome=paired, manager=graph.agent_manager, ended_at=checked_ended_at)
    return paired
