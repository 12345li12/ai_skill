"""``[02] → [05]`` 的调度约束 seam —— Amendment A3 的冻结 DTO。

架构依据：`docs/architecture.md` 2 节 ``[05] Planner``（Amendment A3）。

**为什么单独一个模块：**

``EffectivePlanningConstraints`` 是 ``[02] Policy`` 与 ``[05] Planner``
之间的**共享 seam 类型**。数据流方向是 ``[02] → [05]``，因此：

- ``policy.py``（producer）从这里 import 并 re-export，
  保持 5B.1 已冻结的 ``adaptive_router.policy.EffectivePlanningConstraints`` 可用；
- ``planner.py``（consumer）也**直接**从这里 import。

这样两个节点**都不依赖对方的模块**，不会形成
``planner → policy`` 这种与数据流方向相反的实现依赖。

⚠️ 它是**实现级 DTO**：**不在** ``contracts/`` 里，也**不新增架构节点**
（A3.9：``contracts/`` 未改）。将来若 ``max_children`` 等确成规格需要，
应显式记一次窄幅修订（建议编号 **A4**），而不是把它扩成"万能 Policy 袋子"。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["EffectivePlanningConstraints"]


@dataclass(frozen=True)
class EffectivePlanningConstraints:
    """A3 冻结的实现级 DTO：``[05]`` Planner 唯一需要的策略投影。

    ⚠️ **只含分流权限**。模型档位约束（``allowed_tiers`` 等）**不得**进来：
    ``[02]`` 另负责"仅用当前模型、调高/调低或指定模型"，而 ``[06]`` 才是
    模型档位决策者（A3.7）。将来若需要强制约束，走独立的 typed seam。
    """

    auto_split_allowed: bool
    rationale: str
