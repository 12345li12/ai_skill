"""``[09] Scheduler`` 的准入事实 seam —— Amendment A11.3 的实现级 DTO。

架构依据：`docs/architecture.md` 2 节 ``[09] Scheduler``（**Amendment A11**，FROZEN）。

**七个语义维度、统一 blocker 极性**（A11.3）：

    True  = blocker 已明确成立      → 可进入 REJECT
    False = blocker 已明确不成立    → 不构成 veto
    None  = UNKNOWN                → 既不视为通过，也不产生 REJECT / REUSE

⚠️ **极性按 blocker 读，不按"字段名听起来是好事"读**：

- ``evidence_sufficient=True`` 表示"**证据已经够了，不应继续派发**"（L47 的停止条件），
  因此它**是** blocker；
- ``insufficient_independent_value=True`` 才是"**缺乏独立价值**"（**取反命名**）。

⚠️ **本 DTO 只做事实载体**：不提供 ``has_blocker`` 等聚合 —— 聚合属 ``[09]``（A11.8 第 2 步）。

⚠️ **producer 不在这里**：``AdmissionFactsSource`` 是**边界级** producer（A11.3b），
由 host / execution boundary 注入；``[09]`` 只消费、**不拥有生成权**。
本轮**不创建**该 Source 的类（concrete typing 留到 P1 / ``[10]`` wiring）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import BranchId
from .contracts.common import validate_branch_id

__all__ = ["AdmissionFacts", "RelationFact"]


@dataclass(frozen=True)
class RelationFact:
    """关系型维度（``duplicate`` / ``context_duplicate``）的三态事实。

    ⚠️ **两条不变量（A11.3）——`None` 不再一值两用**：

        status is True                ⇒ related_branch_id **必须**给出且合法
        status is False  or  is None  ⇒ **不得**携带 related_branch_id

    因而 ``status=None`` 只表示 **UNKNOWN**；"明确检查过且没有重复"是 ``status=False``。
    """

    status: bool | None
    related_branch_id: BranchId | None = None

    def __post_init__(self) -> None:
        # 信任边界：Python 类型提示不做运行时检查。`1 is True` 为假 ⇒ 放过即 fail-open。
        if self.status is not None and type(self.status) is not bool:
            raise TypeError(f"status 必须是 None 或 bool，收到 {self.status!r}")
        if self.status is True:
            if self.related_branch_id is None:
                raise ValueError("status=True 时必须给出 related_branch_id（A11.3）")
            validate_branch_id(self.related_branch_id)
        elif self.related_branch_id is not None:
            raise ValueError(
                "status 不是 True 时不得携带 related_branch_id（A11.3：None 只表示 UNKNOWN）"
            )


@dataclass(frozen=True)
class AdmissionFacts:
    """A11.3 的实现级 DTO：**某个既有 child 当前是否准入派发**的规范化事实。"""

    duplicate: RelationFact
    context_duplicate: RelationFact
    evidence_sufficient: bool | None
    insufficient_independent_value: bool | None
    remaining_small: bool | None
    dependency_too_strong: bool | None
    cost_exceeds_benefit: bool | None

    def __post_init__(self) -> None:
        # ① 两个关系槽必须真的是 RelationFact（不接受 duck-typed 替身）
        for name in ("duplicate", "context_duplicate"):
            value = getattr(self, name)
            if not isinstance(value, RelationFact):
                raise TypeError(f"{name} 必须是 RelationFact，收到 {value!r}")
        # ② 五个 bool 槽必须是 None 或 bool（type is bool ⇒ 拒 0 / 1 / ""）
        for name in (
            "evidence_sufficient",
            "insufficient_independent_value",
            "remaining_small",
            "dependency_too_strong",
            "cost_exceeds_benefit",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} 必须是 None 或 bool，收到 {value!r}")
