"""``[09] Scheduler`` 的可复用结果 seam —— Amendment A11.4 / A11.4b 的实现级 DTO。

架构依据：`docs/architecture.md` 2 节 ``[09] Scheduler``（**Amendment A11**，FROZEN）。

**为什么必须独立成 seam：** ``AgentRun`` 只有 run / branch / agent / attempt / state /
timestamps / handle —— **没有结果内容**。因此"看到 ``COMPLETED`` 就 REUSE"是错的；
可复用结果必须有一条**显式、已验证**的来源。

**三条一致性不变量（A11.4）：**

    ① source_branch_id == branch_return.branch_id == validation.branch_id
    ② validation.passed is True
    ③ reusable_results_by_branch 的 **key 必须属于当前待 admission 的 child**
       （mapping 层不变量，由 ``[09]`` 校验）

⚠️ **本 DTO 只能证明"结构不变量"。** 它**不能**证明
"``BranchReturn`` + ``ValidationRecord`` 来自**同一次真实 ``[12] → [13]`` 配对事件**" ——
那件事的 owner 是**边界级** ``ReusableResultSource``（A11.4b）：
**不得**事后仅凭 ``branch_id`` 从两个独立集合重新配对。
本轮**不创建**该 Source 的类（concrete typing 留到 P1 / ``[10]`` wiring）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import BranchId, BranchReturn, ValidationRecord
from .contracts.common import validate_branch_id

__all__ = ["ReusableResultRef"]


@dataclass(frozen=True)
class ReusableResultRef:
    """一个**已形成且已通过验证**的可复用结果，以及它合法的来源分支。"""

    source_branch_id: BranchId
    branch_return: BranchReturn
    validation: ValidationRecord

    def __post_init__(self) -> None:
        # 先身份：不靠 duck typing（"对象恰好有 .branch_id/.passed"不算）
        if not isinstance(self.branch_return, BranchReturn):
            raise TypeError(f"branch_return 必须是 BranchReturn，收到 {self.branch_return!r}")
        if not isinstance(self.validation, ValidationRecord):
            raise TypeError(f"validation 必须是 ValidationRecord，收到 {self.validation!r}")
        validate_branch_id(self.source_branch_id)
        # 再结构：三处 branch_id 必须一致
        if not (
            self.source_branch_id
            == self.branch_return.branch_id
            == self.validation.branch_id
        ):
            raise ValueError(
                "source_branch_id / branch_return.branch_id / validation.branch_id 必须一致（A11.4 ①）"
            )
        # 且必须真的通过验证（FAIL 永远不能变成 REUSE）
        if self.validation.passed is not True:
            raise ValueError("只有 validation.passed is True 的结果才是可复用的（A11.4 ②）")
