"""``[09] Scheduler`` 的调度决定 DTO —— Amendment A11.5 的实现级最小三字段。

架构依据：`docs/architecture.md` 2 节 ``[09] Scheduler``（**Amendment A11**，FROZEN）。

**四值固定**：``START`` / ``QUEUE`` / ``REJECT`` / ``REUSE``。
**输出固定为三字段**：``branch_id`` / ``action`` / ``reason``（``reason`` 非空）。

**V1 不放进这个 DTO**（A11.5）：``BranchState``、模型、成本、``ContextBundle``、运行句柄，
**也不塞 REUSE 的结果本体** —— 结果的 provenance 由 ``ReusableResultRef`` 承担。

⚠️ ``DispatchDecision(action=QUEUE)`` **本身就是给 ``[10]`` 的显式请求**（A11.7）：
本模块**不写任何运行态**，物化（例如 ``WAITING``）由 ``[10]`` 决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import BranchId
from .contracts.common import validate_branch_id

__all__ = ["DispatchAction", "DispatchDecision"]


class DispatchAction(Enum):
    """A11.5 冻结的四值。**不得增删。**"""

    START = "start"
    QUEUE = "queue"
    REJECT = "reject"
    REUSE = "reuse"


@dataclass(frozen=True)
class DispatchDecision:
    """``[09]`` 对**一个** child 的派发裁决。"""

    branch_id: BranchId
    action: DispatchAction
    reason: str

    def __post_init__(self) -> None:
        validate_branch_id(self.branch_id)
        if not isinstance(self.action, DispatchAction):
            raise TypeError(f"action 必须是 DispatchAction，收到 {self.action!r}")
        if type(self.reason) is not str or not self.reason.strip():
            raise ValueError(f"reason 必须是非空 str，收到 {self.reason!r}")
