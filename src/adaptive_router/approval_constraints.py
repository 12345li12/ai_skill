"""``[02] → Approval Judgment`` 的 approval-policy seam —— Amendment A6 的冻结 DTO。

架构依据：`docs/architecture.md` 2 节 ``Approval Judgment``（**Amendment A6**，FROZEN）。

**为什么单独一个模块（与 planning seam 同构）：**

``EffectiveApprovalConstraints`` 是 ``[02] Policy`` 与 ``Approval Judgment``
之间的**共享 seam 类型**。数据流方向是 ``[02] → Judgment``，因此：

- ``policy.py``（producer）从这里 import 并 re-export，
  保持 ``adaptive_router.policy.EffectiveApprovalConstraints`` 可用；
- 将来的 ``approval_judgment.py``（consumer）也**直接**从这里 import。

这样 Judgment **不依赖整个 ``policy.py``**，也不会顺带拿到
``auto_split_enabled`` / ``protocol_enabled`` / ``use_current_model`` /
``model_preference`` / ``mode`` / ``stop_branches_requested`` 这些与审批无关的字段。

⚠️ 它是**实现级 DTO**：**不在** ``contracts/`` 里，也**不新增架构节点**
（A6.7：``contracts/`` / ``AuthorizationContext`` / ``ApprovalJudgment``
五字段 / Approval Gate 全未改）。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["EffectiveApprovalConstraints"]


@dataclass(frozen=True)
class EffectiveApprovalConstraints:
    """A6 冻结的实现级 DTO：Approval Judgment 唯一需要的审批策略投影。

    ``require_approval_for_all`` 来自 L45 的「全部先审批」干预，
    经 ``[02]`` 的 L11 优先级序解算后的结果。

    ⚠️ **`rationale` 不是展示字段，而是审计证据**：
    它记录"这个强制审批结论是如何由优先级解析出来的"。
    只留 ``bool`` 会**永久丢弃**该证据。

    ⚠️ Judgment **可以**用它形成真实、用户可理解的审批说明，
    但**不得**被要求"原样输出 `rationale`" —— 它可能包含内部层级描述
    （例如 ``system: approval_mode 被该层限制（L11）``），
    逐字暴露既无必要也不友好（A6.6）。
    """

    require_approval_for_all: bool
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.require_approval_for_all, bool):
            raise TypeError(
                "require_approval_for_all 必须是 bool，"
                f"收到 {self.require_approval_for_all!r}"
            )
        if not isinstance(self.rationale, str):
            raise TypeError(f"rationale 必须是 str，收到 {self.rationale!r}")
        if not self.rationale.strip():
            raise ValueError("rationale 必须是非空 str（它是审计证据，A6.3）")
