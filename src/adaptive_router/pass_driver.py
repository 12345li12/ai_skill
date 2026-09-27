"""``C2d-2c pass driver``（`authoritative ExecutionGraph + 单 pass 驱动`）。

架构依据：`docs/architecture.md` **A11.3c**（首次 / 后续 scheduling pass 的两种来源）·
**A11.6b**（单 pass 槽分配）· **A13.4**（`active_subagent_count` 的 definition）·
**A13.5**（`RuntimeControlFacts` 是**显式用户控制命令**）· **A10.4**（每个 `Plan.branches` child 预构造一个 bundle）。

范围、冻结与授权见 `tests/behavior/README_step5b.md` **§180**（C2d-2c Preflight）/
**§181**（Scope Review）/ **§182**（Scope Correction）/ **§183**（extension 授权）/
**§185**（extension 冻结记录）/ **§186**（C2d-2c 实现授权申请）。

**本模块的责任（一句话）：**

    **驱动一个 pass** —— 调用已冻结的 `ExecutionCompositor.run_pass` 一次，原样交回它的报告。

**逐条边界（`§186.4` 冻结）：**

  · **不是**第二个状态机：唯一可变内部状态是 ``first_pass``（首轮标志）；
  · **不**判定终态 · **不**判 PASS/FAIL · **不**判收敛 —— 那些属编排 / ``close_run`` 侧；
  · **不**持有业务决策 · **不**选节点 · **不**改 ``Plan`` · **不**自行推进 attempt；
  · **不**写运行态：唯一的运行态写入者是 ``[10]``（``run_pass`` 内部间接写入）；
  · **零循环**（``for`` / ``while`` 都不出现）· **零 sleep / 零 polling / 零时钟**；
  · **不**自行决定"是否还要下一轮" —— 下一次调用由**外部状态变化**触发（`K4` 冻结）。

**为什么是"单 pass"而不是"跑到没活为止"（`§182.6` 的 `K4`）：**

    ``run_pass`` 可以把 ``START`` 交出去（此时分支**尚未** ``RUNNING``），同时把别的分支留成 ``WAITING``
    ⇒ 一次 pass 返回 **≠** 真实执行完成。若在"没有任何外部状态变化"时立刻再跑一轮，
    容量已满 / ceiling 未知时可用槽恒为 0 ⇒ ``QUEUE → QUEUE → QUEUE`` **无进展自旋**。
    那会与架构 **L39**（"后台运行不高频轮询"）直接冲突。
    ⇒ 因此本模块**只交付单 pass 入口**；"等待运行中 agent 完成"是**调用方**的责任，
      **不得**伪装成 Core 内部的忙循环。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** ``close_run`` / ``converge_run`` /
   ``recheck`` / ``record_outcome`` 等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。
"""

from __future__ import annotations

from .composition_root import CompositionRoot, ExecutionGraph
from .contracts import RoutingPlan
from .execution import ExecutionPassReport
from .scheduling_runtime_facts import SchedulingRuntimeFacts

__all__ = ["PassDriver", "PassDriverError"]


class PassDriverError(RuntimeError):
    """``C2d-2c`` 的显式失败：注入面形状非法 / 被传入**外部图**。

    ⚠️ **不是** ``ContractError``（与 ``CompositionRootError`` / ``RouterWrapperError`` 同型）：
       这些是**装配期 / 注入期**的形状错误，不是契约数据错误。
    """


class PassDriver:
    """**持有一张 authoritative 运行态图**的单 pass 驱动者（`§186.4` 冻结稿 v2）。

    **构造（keyword-only，恰好 2 项）：**

        ``PassDriver(composition_root=..., routing=...)``

    ⚠️ **不接受** ``ExecutionGraph`` 入参（`LPD-9`）：图必须由**本类在构造期自己**从组合根物化 ——
       否则会与 ``RouterWrapper`` 那张"装配证明"图混淆，产生**两个运行态载体**的歧义
       （``§175.3`` 冻结的三条理由）。

    ⚠️ ``materialize`` 在**全生命周期恰好一次**，且在**构造期**发生（`LPD-3`）：
       ⇒ ``run_id`` 一致性守卫在**构造时**即触发 ⇒ 错配**早失败**，不必等第一次 pass。
    """

    def __init__(
        self,
        *,
        composition_root: CompositionRoot,
        routing: RoutingPlan,
    ) -> None:
        # ① 形状守卫：显式失败优先于静默默认（A3 / A6.4）。
        #    ⚠️ 单独判 `ExecutionGraph` 是**必须的**：`isinstance(graph, CompositionRoot)` 只会给出
        #       含糊的"必须是 CompositionRoot"，而真实错误是"**不要**把外部图传进来"（`LPD-9`）。
        if isinstance(composition_root, ExecutionGraph):
            raise PassDriverError(
                "本驱动者**不接受**外部传入的 `ExecutionGraph`（`LPD-9`）—— 运行态图必须由本类"
                "在构造期从 `composition_root` 自行物化（否则会同时存在两个运行态载体）"
            )
        if not isinstance(composition_root, CompositionRoot):
            raise PassDriverError(
                f"composition_root 必须是 CompositionRoot，收到 {composition_root!r}"
            )
        if not isinstance(routing, RoutingPlan):
            raise PassDriverError(f"routing 必须是 RoutingPlan，收到 {routing!r}")

        # ② **唯一**一次物化（`LPD-2` / `LPD-3`）；此后 `drive_next_pass` 只复用这一张图。
        self._graph: ExecutionGraph = composition_root.materialize(routing)
        # ③ 保存组合根：`capability` 与 `control_facts_source` 都从**同一个**根读取（`§183.2`）。
        self._composition_root: CompositionRoot = composition_root
        # ④ 唯一可变内部状态（首轮标志；**不得**用"pending 为空"之类反推 —— `execution.py` L150-151 硬禁）
        self._first_pass: bool = True

    # ------------------------------------------------------------------
    # 唯一业务入口
    # ------------------------------------------------------------------

    def drive_next_pass(self) -> ExecutionPassReport:
        """驱动**一个** pass，返回 ``run_pass`` 的报告（**原样**，不改写、不包装、不解释）。

        ⚠️ **一次调用 = 一个 pass**（`K4` 冻结）：
           本方法**不循环**、**不 sleep**、**不轮询**、**不判断**"是否还要下一轮"。
           下一次调用由**外部状态变化**触发（调用方负责观察运行中分支的完成）。

        ⚠️ ``capability`` 与 ``control_facts_source`` 都取自**同一个** ``composition_root``
           （`§183.2`）：⇒ "同一个 registry / 同一个控制事实来源"由**结构**保证，不靠调用方承诺。
           只调 ``snapshot()``（**读取缓存快照**；无缓存时才做一次全量探测）——
           **不调** ``recheck()``（六类重查触发的 emit 方仍是 `K1-b` OPEN GAP）。

        ⚠️ ``runtime_facts`` 的 ``active_subagent_count`` 是**观测派生**（A13.4 的 definition 等式，
           `K3-a` 已关闭）：`stop=True` 时调度根本不会执行、`stop=False` 时 control fold 不改变计数
           ⇒ 在本调用**之前**读取与 ``run_pass`` 内部调度时刻**同值**。

        ⚠️ ``first_pass`` 在**本次 pass 被发起时消费**（`§186.5` 裁定 **(甲)**）：
           翻转放在 ``finally`` ⇒ **异常也不会**让下一次以"首次全量"重跑（不制造重复首轮语义）；
           异常**原样上抛**（**不吞** —— `LEC-10` 的"零吞异常"纪律在行为上延续）。
        """
        capability = self._composition_root.capabilities.snapshot()
        runtime_facts = SchedulingRuntimeFacts(
            active_subagent_count=self._graph.agent_manager.active_subagent_count
        )
        first_pass = self._first_pass
        try:
            report = self._graph.compositor.run_pass(
                routing=self._graph.routing,
                bundles=self._graph.bundles,
                capability=capability,
                runtime_facts=runtime_facts,
                control_facts_source=self._composition_root.control_facts_source,
                first_pass=first_pass,
            )
        finally:
            # **发起即消费**：即使 `run_pass` 中途抛异常（可能已产生副作用），
            # 下一次也不得以"首次全量"重跑 —— 那是重复首轮语义。
            self._first_pass = False
        return report
