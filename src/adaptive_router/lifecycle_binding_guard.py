"""``lifecycle_binding_guard`` —— ``C2d-2d`` **跨调用 continuation 极薄绑定守卫**（Core 侧）。

架构依据：`tests/behavior/README_step5b.md` **§219**（Thin Lifecycle Holder Scope Review）/
**§220**（Host Lifecycle Ownership · 结论 `B`）· **§222**（Host Actual Entry · 结论 `C` + `CONTRACT ONLY`）·
**§223**（方向 `B′`）· **§224**（实现授权申请）· **§225**（冻结状态模型）。

**它是什么（一句话）：**

    一个**极薄**的有状态守卫：**永久绑定恰好一个** authoritative ``PassDriver`` 实例，
    并把"每次外部 re-entry **恰好转发一次**"这件事变成**机械边界**。

**形态（`§225.2` 冻结）：**

    External Host owns lifecycle
            ↓
    Host **retains the same** ``LifecycleBindingGuard`` object across calls
            ↓
    Core ``LifecycleBindingGuard``
            ├─ ``_bound_pass_driver``：**对象引用本身**（⛔ 不是 bool · ⛔ 不是重建配方）
            ├─ ``_released``：单调标志（一旦 True **永不回 False**）
            └─ ``_guard``：**一把** non-blocking ``threading.Lock``

**为什么必须有这把锁（`§224.4.1` · `§225.1` ④）：**

    若两个调用**真正并发**，双方都可能先看到"未绑定 / 未释放"再各自设位
    ⇒ **单纯 bool check/set 不能提供可靠的原子拒绝**。
    ⇒ 因此**同一把** non-blocking 锁**覆盖 `bind()` / `drive_once()` / `release()` 三个入口的全部状态转换**；
      获取失败 ⇒ **立即** ``LifecycleBindingError``（⛔ 不 WAIT · ⛔ 不 QUEUE · ⛔ 不自动串行化）。
    ⇒ ⛔ 若只在 ``drive_once()`` 里加锁，``release()`` 与正在执行的 drive、或并发 ``bind`` 仍存在竞态。

**⛔ 本模块明确不做（逐条 · 越界即缺陷）：**

  · ⛔ **不构造** ``PassDriver``（``PassDriver(`` 构造点 = **0**）· ⛔ **不** ``materialize``（= **0**）；
  · ⛔ 不 import ``CompositionRoot`` / ``RouterWrapper`` / ``entrypoint`` / ``adapters``（零运行期依赖）；
  · ⛔ 不持有 ``routing`` / ``CompositionRoot`` / ``run_id`` / ``event_id`` / ``retry_count`` /
    ``queue`` / ``registry`` —— **"重建配方"是被禁止的形态**（`§219.2` 的红线）；
  · ⛔ 无 ``run_id`` 查找面 · ⛔ 无全局 registry · ⛔ 无 queue · ⛔ 无 dedupe · ⛔ 无 retry；
  · ⛔ 不解释终态 · ⛔ 不判定 PASS/FAIL · ⛔ 不自动 second-pass · ⛔ 不 poll / sleep / timer / loop；
  · ⛔ **不吞异常**：底层 ``drive_next_pass()`` 的异常**原样传播**，⛔ 不转换成 ``LifecycleBindingError``；
  · ⛔ 异常后**不**自动 ``release`` / ``retry`` / ``recovery`` / ``mark terminal``
    —— crash / interrupted disposition **仍未裁定**（`§225.6`）。

**与已裁规则的关系（⛔ 不是新裁定）：**

    ``same-scope concurrent re-entry -> REJECT`` · ``re-entry while drive active -> REJECT`` ·
    ``released lifecycle access -> REJECT`` 均为 `§219.5` / `§222.16` **已裁**；
    本模块只是它们的**机械载体**（`F4` / `F3` / `F5` / `F8`）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** ``PassDriver`` / ``materialize`` / ``run_id`` /
   ``registry`` 等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期；运行期**零** import 依赖
    from .pass_driver import PassDriver

__all__ = ["LifecycleBindingError", "LifecycleBindingGuard"]


class LifecycleBindingError(RuntimeError):
    """``C2d-2d`` 的显式失败：绑定 / 驱动 / 释放的**生命周期顺序错误**或**并发冲突**。

    ⚠️ **不是** ``ContractError``（与 ``PassDriverError`` / ``RouterWrapperError`` /
      ``CompositionRootError`` / ``ExecutionRecoveryCallbackError`` 同型）：
      这些是**装配期 / 使用期**的形状与顺序错误，不是契约数据错误。

    ⚠️ 本异常**只**表示"**这次操作被机械拒绝**"，⛔ **不**承载任何业务 / 终态 / 恢复语义。
    """


class LifecycleBindingGuard:
    """**持有恰好一个** authoritative ``PassDriver`` 实例的极薄绑定守卫（`§225.2`）。

    **公开能力恰好三项**（⛔ 无 ``retrieve`` / ``lookup`` / ``rebuild`` / ``queue`` / ``registry``）：

        ``bind(pass_driver)`` · ``drive_once()`` · ``release()``

    **内部状态恰好三项**（⛔ 不得再增加；``_driven`` / ``_active bool`` 已被裁定**删除**）：

        ``_bound_pass_driver`` · ``_released`` · ``_guard``

    **状态机（`§225.2` 冻结）：**

        ``UNBOUND``  --bind--> ``BOUND`` ；``drive`` / ``release`` ⇒ REJECT
        ``BOUND``    --drive--> ``ACTIVE`` ；``bind`` ⇒ REJECT ；``release`` --> ``RELEASED``
        ``ACTIVE``   ：``drive`` / ``bind`` / ``release`` ⇒ REJECT ；
                       ``drive`` 成功 ⇒ ``BOUND`` ；``drive`` 抛异常 ⇒ ``BOUND`` + **原异常传播**
        ``RELEASED`` ：``bind`` / ``drive`` / ``release`` **全部 REJECT**（**吸收态**）

    ⚠️ **不存在** ``BOUND -> permanently DRIVEN`` ⇒ 所以没有 ``_driven`` 字段：
      ``drive_once`` 的 "once" = "**每次 accepted re-entry 恰好调用一次底层 drive**"，
      ⛔ **不是**"一个 binding 生命周期只能 drive 一次"（同一 binding 可被**多次**合法驱动）。

    ⚠️ **`bind(None)` 必须被拒绝**：``None`` 同时是 ``UNBOUND`` 的内部表示
      ⇒ 允许它进入会**破坏状态机**（`§225` 的输入边界补充）。
    """

    __slots__ = ("_bound_pass_driver", "_released", "_guard")

    def __init__(self) -> None:
        # 内部状态**恰好三项**（`§225.2`）。⛔ 这里**不**接收任何入参：
        # 绑定发生在 `bind()`，⛔ 不在构造期 —— 否则等于把"重建配方"搬回构造面。
        self._bound_pass_driver: PassDriver | None = None
        self._released: bool = False
        # **唯一**一把 non-blocking 锁：覆盖三个入口的**全部**状态转换（`§225.1` ④）。
        self._guard: threading.Lock = threading.Lock()

    # ------------------------------------------------------------------
    # 唯一公开能力（恰好三项）
    # ------------------------------------------------------------------

    def bind(self, pass_driver: PassDriver) -> None:
        """**恰一次**绑定 authoritative ``PassDriver``；保留**对象引用本身**。

        ⛔ 拒绝：已释放（`F3`）· 已绑定（`F2`）· ``None``（输入边界）。
        ⛔ 不接收 ``composition_root`` / ``routing`` —— 那会**重新形成"重建配方"**（`§225.1` ①）。
        """
        self._acquire()  # 失败 ⇒ 立即 LifecycleBindingError（⛔ 不等待）
        try:
            if self._released:
                raise LifecycleBindingError(
                    "本 binding 已 RELEASED：**释放是永久吸收态**，⛔ 不允许重新 bind（F3）"
                )
            if self._bound_pass_driver is not None:
                raise LifecycleBindingError(
                    "本 binding 已绑定 authoritative PassDriver：**绑定恰一次**，"
                    "⛔ 不得替换已保留的实例（F2）"
                )
            if pass_driver is None:
                raise LifecycleBindingError(
                    "bind(None) 被拒绝：None 同时是 UNBOUND 的内部表示，"
                    "允许它进入会**破坏状态机**（⛔ 这不是静默默认可以处理的情形）"
                )
            # 保留**对象引用本身**（⛔ 不是重建配方、⛔ 不是 bool）。
            self._bound_pass_driver = pass_driver
        finally:
            self._guard.release()

    def drive_once(self) -> Any:
        """把**恰好一次**转发到**绑定的那个** ``PassDriver``，并原样返回其结果。

        ⚠️ **锁覆盖整个底层 ``drive_next_pass()`` 调用期间**（`§225` 的明确要求）
          ⇒ 其他线程同时 ``bind`` / ``drive_once`` / ``release`` **只会立即失败**，
          ⛔ **不会** WAIT / QUEUE / 自动串行化。

        ⚠️ 底层异常**原样传播**（⛔ 不转换成 ``LifecycleBindingError``）；
          且锁在 ``finally`` 中释放 ⇒ ⛔ **不留永久 active 状态**；之后 binding **仍是 BOUND**
          （⛔ 不自动 ``release`` / ``retry`` / ``recovery`` / ``mark terminal``）。
        """
        self._acquire()
        try:
            if self._released:
                raise LifecycleBindingError(
                    "本 binding 已 RELEASED：release 后任何操作都被拒绝（F5）"
                )
            if self._bound_pass_driver is None:
                raise LifecycleBindingError(
                    "本 binding 尚未绑定：drive before bind 被拒绝（F1），⛔ 不得隐式 bind"
                )
            # **恰好一次**转发到**绑定实例**；返回值原样交回（⛔ 不包装、不解释）。
            return self._bound_pass_driver.drive_next_pass()
        finally:
            self._guard.release()

    def release(self) -> None:
        """**永久丢弃**对 ``PassDriver`` 的 custody；``RELEASED`` 为**吸收态**。

        ⛔ 拒绝：已释放（`F8`）· 未绑定（`F6`）· drive 进行中（`F7`，锁被持有 ⇒ 获取失败）。
        ⛔ **不幂等** —— 生命周期顺序错误**不得被静默吞掉**（`§225.1` ②）。
        """
        self._acquire()
        try:
            if self._released:
                raise LifecycleBindingError(
                    "本 binding 已 RELEASED：**第二次 release 被拒绝**（F8 · ⛔ 不幂等）——"
                    "duplicate lifecycle signal ⛔ 不得被解释成合法行为"
                )
            if self._bound_pass_driver is None:
                raise LifecycleBindingError(
                    "本 binding 尚未绑定：release before bind 被拒绝（F6 · ⛔ 不幂等）"
                )
            # **真正丢弃** custody（⛔ 不是置一个 bool 而已）。
            self._bound_pass_driver = None
            self._released = True
        finally:
            self._guard.release()

    # ------------------------------------------------------------------
    # 内部：唯一的锁入口
    # ------------------------------------------------------------------

    def _acquire(self) -> None:
        """**非阻塞**获取状态锁；失败 ⇒ 立即显式失败（⛔ 不等待 · ⛔ 不重试）。

        ⚠️ 承载 `F4`（concurrent / active drive）与 `F7`（release while drive active）：
          两者从锁的角度是**同一件事** —— "本次操作无法独占状态转换"。
        """
        if not self._guard.acquire(blocking=False):
            raise LifecycleBindingError(
                "本 binding 的状态转换正被另一次调用独占（并发 bind / drive / release）："
                "**非阻塞守卫拒绝本次操作**（⛔ 不等待 · ⛔ 不排队 · ⛔ 不自动串行化）"
            )
