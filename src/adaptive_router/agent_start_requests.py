"""``[10] → P1`` 的一次派发许可 —— Amendment A13.2 的实现级 DTO。

架构依据：`docs/architecture.md` 2 节 ``[10] Agent Manager``（**Amendment A13**，FROZEN）。

**``START`` 只是 admission licence，不是状态写入（A13.2）。**
``DispatchDecision(action=START)`` 表示"**获准立即派发**"；``[10]`` 据此产出本 DTO，
由 **P1** 把它搬到 ``[11]``。**只有**收到显式的"成功启动"事实后，
``[10]`` 才提交 ``AgentRun(state=RUNNING)`` 并写入 ``handle``。

⚠️ ``[10]`` **连占位记录都不能建**：A13.4 把 "pending-admission" 定义为
"**最新 attempt 状态为 ``WAITING``**"，若 ``START`` 先建一条 ``WAITING`` 记录，
该 child 会被**再次判为 pending** ⇒ **重复派发**。

**为什么必须独立成 DTO：** 若只传裸 ``tuple[BranchId, ...]``，就回到 A10.1b
明确**拒绝**的"靠顺序 / 位置猜归属"（`ContextBundle` 自身没有 ``branch_id`` 字段）。

**``bundle`` 是已形成的事实**（A10.1b 的 carrier = ``Mapping[BranchId, ContextBundle]``；
A11.9 已写明 ``[10]`` "按 ``branch_id`` 取对应 ``ContextBundle``"）：
``[10]`` **不得**自己构造 bundle、**不得**回读 Planner。
"""

from __future__ import annotations

from dataclasses import dataclass

from .contracts import BranchId, ContextBundle
from .contracts.common import validate_branch_id

__all__ = ["AgentStartRequest"]


@dataclass(frozen=True)
class AgentStartRequest:
    """``[10]`` 要求 P1 派发的**一个** child（不可变）。"""

    branch_id: BranchId
    attempt: int
    bundle: ContextBundle

    def __post_init__(self) -> None:
        validate_branch_id(self.branch_id)
        # `type is int` —— 显式拒 bool（Python 的 bool 是 int 子类，必须排除）
        if type(self.attempt) is not int:
            raise TypeError(f"attempt 必须是 int（拒 bool），收到 {self.attempt!r}")
        if self.attempt < 1:
            raise ValueError(f"attempt 从 1 开始计数（架构 9.1），收到 {self.attempt!r}")
        if not isinstance(self.bundle, ContextBundle):
            raise TypeError(f"bundle 必须是 ContextBundle，收到 {self.bundle!r}")
