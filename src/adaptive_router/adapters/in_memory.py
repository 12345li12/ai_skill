"""``[11] Runtime Adapter`` 的**生产替身**：``InMemoryAdapter``（`C2d-2d`）。

架构依据：`docs/architecture.md` 2 节 ``[11] Runtime Adapter``（**Amendment A14**，FROZEN）。
范围、裁决与冻结项见 `tests/behavior/README_step5b.md` **§191**（Preflight）/ **§192**（裁决登记）/
**§193**（Scope Review）/ **§194**（实现授权申请）。

**本模块是什么（一句话）：**

    把 ``[10]` 的派发许可**在内存里**变成 ``AdapterStartReceipt`` ——
    让 adapter contract 具备**可执行闭环**，且**零外部调用**。

**本模块不是什么（三条，缺一不可）：**

  · **不是**真实平台适配器 —— 它**不**调用任何 SDK / 网络 / 子进程；
    V1 的"不含真实平台适配器"范围（`adapters/__init__.py` 的冻结 docstring）**未被改变**；
  · **不是**测试守卫 —— 那是 ``tests/behavior/test_adapter_fake.py`` 的 ``FakeAdapter``
    （它的 ``start`` 会 ``assert`` 失败，用于证明"不会真实启动"）。两者**并存、不合并**（`§192.1` B）；
  · **不是**编排者 —— **不**判能力、**不**建计划、**不**决定重试或降级。

**动作面：只有 ``start``（有意如此 · `§194.1` ①（甲））**

    本模块**不定义** ``append`` / ``stop``：它们的生产消费方尚未存在
    （``[10]`` 的 ``WAITING_INPUT`` 入口仍 deferred），因此"deferred"由**结构**表达 ——
    调用它们会得到 ``AttributeError``（响亮失败），而不是一个永远不可达的空壳实现。

**四条装配层边界（全部由既有锁机械保证，本模块逐条遵守）：**

  · ``L11-3``：导入面 ⊆ ``{__future__, annotations, dataclasses, dataclass, enum, Enum, contract}``
    ⇒ 因此 ``RoutingPlan`` / ``ContextBundle`` **只能出现在 docstring**，**不得**运行期导入
    （``from ..contracts import …`` 会让 ``L11-3`` 变红）；
  · ``L11-4``：**不**构造 ``Plan`` / ``RoutingPlan``（INV-03：不得自行改变计划）；
  · ``L11-5``：**零时钟** —— ``started_at`` 由**外部注入**（本模块自己不读时间）；
  · ``L11-6``：**零** ``.get(...)``（禁静默默认）· **零** ``open(...)``。
  另：``L11-8``（裁定 ④）**不引入** ``typing.Protocol`` / ``ABC`` —— duck typing 即可。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** ``Protocol`` / ``RoutingPlan`` 等词
   ⇒ 判定一律 **AST 级 / 绑定集级**。
⚠️ **一处实测记录**：``L11-4`` 的 ``test_no_plan_construction`` 用的是**子串**判定
   （在源码里找"计划类名紧跟左括号"这个字面串）⇒ 本模块 docstring 里**刻意不写出该字面串**
   （提到计划类名时不带左括号），否则会**假红** ——
   本文件上一稿正是因为在自己的说明里**引用了那个串**而触发了自指式的假红。
   ⚠️ 这**不是**绕过：本模块确实**没有**任何计划构造调用。实证 = ``L11-4`` 的
      ``test_no_routing_plan_construction``（同类子串判定，为真）与 ``LIA-5`` 的 **AST** 判定。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contract import AdapterError, AdapterStartReceipt

__all__ = ["InMemoryAdapter"]

#: 平台句柄的命名空间 —— **owner = `[11]`**（A14.2：`handle` 是平台句柄，**不是** `agent_id`）。
_PLATFORM_NAMESPACE = "mem"


@dataclass
class InMemoryAdapter:
    """in-memory 平台替身：**只**实现 ``start``（A14.5 的第一个动作面）。

    **注入面（恰 1 项）：**

        ``timestamp`` —— 启动事实里的 ``started_at``（``L11-5``：适配层**不读**时钟，
        时间由外部显式给出，因此本类**没有**默认值、也**不会**自己去取当前时间）。

    **为什么构造注入而不是每次调用注入**
        `A14.3` 说 ``started_at`` 是**发生的事实**。本替身把"平台时间"建模为**注入的固定值**，
        于是同一实例的多次 ``start`` 得到**可复现**的时间戳 —— CI 里不需要冻结真实时钟。
    """

    timestamp: str

    def __post_init__(self) -> None:
        if type(self.timestamp) is not str or not self.timestamp.strip():
            raise AdapterError(
                f"timestamp 必须是非空 str（平台事实，显式注入），收到 {self.timestamp!r}"
            )
        #: 仍然开着的平台句柄（A14.2：句柄的生命周期**属于适配器**，调用方不持有它）。
        #: ⚠️ 用 ``set[str]`` 而非映射：`L11-6` **禁止** ``.get(...)``（禁静默默认）。
        self._handles: set[str] = set()
        #: 已启动的 ``(branch_id, attempt)``（供测试观察，**不**暴露句柄本体）。
        self.started: list[tuple[str, int]] = []

    @property
    def open_handle_count(self) -> int:
        """仍然开着的平台句柄数（观察面；**不**暴露句柄本体）。"""
        return len(self._handles)

    def start(self, *, branch_id, attempt, plan, bundle):
        """A14.3 + A14.5：本替身**总是成功启动**并回 ``AdapterStartReceipt``。

        签名与 `P1` 的实参**逐字对应**（``execution.py`` 的 ``_dispatch``）：
        ``branch_id`` / ``attempt`` / ``plan``（= ``RoutingPlan``）/ ``bundle``（= ``ContextBundle``）。

        ⚠️ **不做能力不匹配分支**（`§192.1` ⑧ 的裁定）：本单元**只**实现成功启动路径；
        ``CapabilityMismatch`` 保留为后续边界，其行为已由 ``FakeAdapter`` 的行为测试覆盖。

        ⚠️ **类型校验的边界（重要）**：本模块**不得** import ``contracts``（`L11-3`），
        因此**无法**做 `isinstance` 级校验。这里只做**本替身自己拥有的**形状守卫 ——
        拒绝 ``None``（缺失即显式失败，禁静默默认）。
        上游冻结输入的类型严格性由**已冻结的构造期校验**承担 ——
        ``RoutingPlan`` 与 ``ContextBundle`` 各自的 ``__post_init__`` 已做强校验
        （例如 ``ContextBundle`` 校验 10 个字段的类型与非空、且禁止"同一操作既允许又禁止"）；
        本适配层**重复实现一遍**只会制造第二套判定面，而不是增加安全性。
        """
        if branch_id is None:
            raise AdapterError("branch_id 不得为 None（缺失即显式失败）")
        if type(attempt) is not int or attempt < 1:
            raise AdapterError(f"attempt 必须是 ≥ 1 的 int（拒 bool），收到 {attempt!r}")
        if plan is None:
            raise AdapterError("plan 不得为 None（缺失即显式失败）")
        if bundle is None:
            raise AdapterError("bundle 不得为 None（缺失即显式失败）")

        handle = f"{_PLATFORM_NAMESPACE}-{branch_id}-{attempt}"
        self._handles.add(handle)
        self.started.append((branch_id, attempt))
        return AdapterStartReceipt(handle=handle, started_at=self.timestamp)
