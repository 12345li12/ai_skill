"""``C2d-2b RouterCore wrapper``（`决策链 + 装配 → 一个可被注入的入口实现`）。

架构依据：`docs/architecture.md` **4.1**（冻结主流程）· **1.4 / 6.4 / L47**（`[10]` 运行态唯一写者）·
**A13.2**（两阶段物化）· **INV-03**（`RoutingPlan` 不可变，交给 Adapter）·
以及 `entrypoint.py` 的 ``RouterCore`` 协议形状（`__call__(task, context) -> Any`）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§174**（C2d-2b Scope Review）/
**§175**（范围稿）/ **§176**（实现授权）。

**本模块要守住的四条主张（逐条对应已冻结裁定）：**

  1. **它是包装边，不是执行边**（`§176.1` A 裁定原样）：
     允许 `decision_router() ↓ RouterOutcome ↓ materialize() ↓ callback`；
     ⛔ **禁止** `run_pass` / `scheduler` / `facts producer` / `adapter`；
  2. **零 DTO**（A 裁定原样："零 DTO 是正确约束"）⇒ 本模块**不新增任何载体**，
     复用既有 `RouterOutcome` 作返回值；
  3. **`materialize` 的语义 = 装配证明 + 顺序转交**（B2 裁定原样）：
     它证明"组合根可以生成执行上下文 · `routing` 可消费 · seam 连通"；
     **≠ 执行 pass** —— 执行责任属 `C2d-2c`；
  4. **无状态**（B/② 候选丙"回调收集"的本意）：只持有两个注入面，
     **不缓存**任何执行结果、**不持有** `ExecutionGraph`。

⚠️ **不 import `entrypoint`**（`§176.1` 裁定 ④）：靠**结构匹配**满足 ``RouterCore``，
   **不**依赖入口模块（与"Core 不 import 入口"的既有方向一致）。
⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `run_pass` / `scheduler` / `adapters` /
   `entrypoint` 等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。
"""

from __future__ import annotations

from typing import Any, Callable

from .composition_root import CompositionRoot
from .contracts import ProjectContext
from .contracts.decision import RoutingPlan
from .router import RouterOutcome

__all__ = ["RouterWrapper", "RouterWrapperError"]


class RouterWrapperError(RuntimeError):
    """``C2d-2b`` 的显式失败：注入面形状非法 / 必需项缺失。"""


class RouterWrapper:
    """**入口包装边**：把"决策链 + 装配"接成一个可被注入的 ``RouterCore``。

    ⚠️ **结构匹配**（不 import `entrypoint`）：本类只需提供
       ``__call__(self, task, context) -> Any``，即满足 ``entrypoint.RouterCore`` 的形状。

    ⚠️ **两个注入面（恰好两个，全部 keyword-only）**：

        ``composition_root``：**唯一**持有者 —— 驱动者需要的
          ``control_facts_source`` / ``admission_facts_source`` / ``reusable_result_source``
          **只在组合根里** ⇒ 回调据此取，**不必重新注入一份**（避免"两个来源"）。

        ``on_execution``：**可选**的"回调收集"seam。**默认 `None`**（不转交）。
          签名冻结为 ``on_execution(composition_root, routing) -> None``：
          传既有类型（`CompositionRoot` + `RoutingPlan`），**不引入任何新载体**。

    ⚠️ **无状态**：本类只持有上面两项；**不**保存 `ExecutionGraph`、
       **不**保存"最近一次执行"、**不**保存 pass 序号。
    """

    def __init__(
        self,
        *,
        composition_root: CompositionRoot,
        on_execution: Callable[[CompositionRoot, RoutingPlan], None] | None = None,
    ) -> None:
        if not isinstance(composition_root, CompositionRoot):
            raise RouterWrapperError(
                f"composition_root 必须是 CompositionRoot，收到 {composition_root!r}"
            )
        if on_execution is not None and not callable(on_execution):
            raise RouterWrapperError(
                f"on_execution 必须是可调用对象或 None，收到 {on_execution!r}"
            )
        self._composition_root = composition_root
        self._on_execution = on_execution

    def __call__(self, task: Any, context: ProjectContext) -> RouterOutcome:
        """跑决策链 → **物化执行半边**（装配证明）→ 转交执行请求 → 返回**既有载体**。

        ⚠️ **四步固定顺序**（`§175.3` 冻结）：
            ① ``outcome = decision_router(task, context)`` —— 跑**既有**决策链（本模块不重写它）
            ② ``materialize(outcome.routing)`` —— **装配证明 + 顺序转交**（**不是**执行 pass）
            ③ ``on_execution(composition_root, routing)`` —— **转交执行请求**（若注入了回调）
            ④ ``return outcome`` —— **复用既有载体**（零新 DTO）

        ⚠️ 第 ② 步**无条件执行**（与 `on_execution` 是否为 `None` 无关）：
           其语义是"**证明组合根可以生成执行上下文 · `routing` 可消费 · seam 连通**"。
           产出的 `ExecutionGraph` **不转交、不缓存**（传它反而制造"两个图"的歧义：
           驱动者需要的是**同一批次**的图，而 `materialize` 每次都新建记录）。

        ⚠️ `materialize` 内部的 **`run_id` 一致性守卫原样生效**：
           若决策链与组合根用了不同 `run_id` ⇒ 抛 ``CompositionRootError``，
           **本模块不吞、不降级**（"显式失败优先于静默默认"）。
        """
        outcome = self._composition_root.decision_router(task, context)
        self._composition_root.materialize(outcome.routing)
        if self._on_execution is not None:
            self._on_execution(self._composition_root, outcome.routing)
        return outcome
