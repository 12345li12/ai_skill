"""``[09]`` 消费的运行期事实（只读投影）—— Amendment A11.6b 的实现级 DTO。

架构依据：`docs/architecture.md` 2 节 ``[09] Scheduler``（**Amendment A11**，FROZEN）。

⚠️ **它不是 ``[10]`` 的 authoritative run state**，而是 ``[10]`` 只读运行态在 ``[09]``
处的**投影** —— 因此叫 ``SchedulingRuntimeFacts`` 而不是 ``SchedulingRunState``
（避免日后出现第二份"运行态"）。

**只放 ``[09]`` 真正读的字段**：``active_subagent_count``。
"pending-admission child ids" 属 ``[10]`` 的状态，由调用方按 A11.3c 使用，**不放这里**。

⚠️ A11 冻结的 UNKNOWN 是 **capability ceiling**（``concurrency_limit is None`` ⇒ QUEUE），
**没有**冻结"未知占用" ⇒ 本字段**必填、无默认值**，缺失即 ``TypeError``（**不得偷当 0**）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SchedulingRuntimeFacts"]


@dataclass(frozen=True)
class SchedulingRuntimeFacts:
    """``[09]`` 为容量计算消费的、来自 ``[10]`` 只读运行态的显式事实。"""

    active_subagent_count: int

    def __post_init__(self) -> None:
        # `type is int` —— 显式拒 bool（Python 的 bool 是 int 子类，必须排除）
        if type(self.active_subagent_count) is not int:
            raise TypeError(
                f"active_subagent_count 必须是 int（拒 bool），收到 {self.active_subagent_count!r}"
            )
        if self.active_subagent_count < 0:
            raise ValueError(
                f"active_subagent_count 不得为负，收到 {self.active_subagent_count!r}"
            )
