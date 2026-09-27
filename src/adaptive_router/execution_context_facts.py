"""``[08] Context Builder`` 的执行上下文事实 seam —— Amendment A10.3 的实现级 DTO。

架构依据：`docs/architecture.md` 2 节 ``[08] Context Builder``（**Amendment A10**，FROZEN）。

**为什么单独一个模块（与 A3 / A6 的 seam 同构）：**

``ExecutionContextFacts`` 是"**Core 之外 / 上游 producer**"与 ``[08]`` 之间共享的
seam 类型。A10.3 定死了依赖方向：producer 在**进入 ``[08]`` 之前**形成这四项事实，
``[08]`` **只消费与校验**、**不拥有生成权**（不得增删条目、不得改写成新事实）。

- producer（执行侧调用方）从这里 import；
- ``context_builder.py``（consumer）也**直接**从这里 import。

两边都**不依赖对方的模块**，避免 ``producer → consumer`` 这种与数据流方向相反的实现依赖
（与 ``planning_constraints.py`` / ``approval_constraints.py`` 同一做法）。

**三条冻结语义（A10.3）：**

1. **逐 child 供给。** 进入 ``[08]`` 的是
   ``Mapping[BranchId, ExecutionContextFacts]``，键集必须与 ``Plan.branches``
   **精确覆盖**；**不得**把同一份全局事实复制给所有 child —— 那会破坏 L35 的
   "**最小且相关**"，也**不得**让 ``[08]`` 自己去挑（那是未冻结的 relevance 判断权）。
2. **内容必须是 Core-neutral 的**（INV-03）。Core **可以**持有平台无关的 operation
   语义；**不可以**持有 Codex / OpenAI / 具体工具 API 的名称或字段形状 ——
   原始平台 tool token 的翻译属 ``[11] Runtime Adapter``。
3. **缺失即显式失败。** 不给默认值、不静默降级为空。

⚠️ 它是**实现级 DTO**：**不在** ``contracts/`` 里，也**不新增架构节点**（A10.8）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ExecutionContextFacts"]


@dataclass(frozen=True)
class ExecutionContextFacts:
    """A10.3 冻结的实现级 DTO：**某个 child** 的四项已规范化执行上下文事实。

    四个字段与 ``ContextBundle`` 的同名四项**一一对应**
    （``background`` / ``constraints`` / ``allowed_operations`` / ``forbidden_operations``），
    **不改契约十字段**。

    ⚠️ 它是"事实载体"，**不是决策者**：本模块不做任何判定，也不解释自由文本。
    """

    background: tuple[str, ...]
    constraints: tuple[str, ...]
    allowed_operations: tuple[str, ...]
    forbidden_operations: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "background",
            "constraints",
            "allowed_operations",
            "forbidden_operations",
        ):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not all(
                isinstance(item, str) for item in value
            ):
                raise TypeError(f"{name} 必须是 tuple[str, ...]，收到 {value!r}")
        overlap = set(self.allowed_operations) & set(self.forbidden_operations)
        if overlap:
            # 与 ContextBundle 契约同一条不变量，但在**更靠近来源**处先失败。
            raise ValueError(f"同一操作不得既允许又禁止：{sorted(overlap)}")
