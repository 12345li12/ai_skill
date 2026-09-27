"""``[12] → [13]`` **接线** —— A11.4b 的"配对事件"落点（范围稿见 README §76）。

架构依据：`docs/architecture.md`

- **A11.4b**（`architecture.md:1974-1985`）：``ReusableResultRef`` 里的 ``BranchReturn`` +
  ``ValidationRecord`` **必须来自同一次真实的 ``[12] → [13]`` 配对事件**；
  **不得**事后仅凭 ``branch_id`` 从两个独立集合重新配对；
- **A15.4**（`architecture.md:2511-2512`）："谁持有 ``BranchReturn`` 直到 ``[13]`` 消费"
  **不在 A15 内** ⇒ 属**本配对轮**；
- **A16.3**：完成标准的**唯一权威来源** = ``Plan.branches`` 的 ``BranchSpec.completion_criteria``；
  ``ContextBundle.completion_criteria`` 是 A10.4 的**传递副本**（一致性核对在 ``[13]`` 内做）；
- **A16.6**：``[12]`` 产生事实 / ``[13]`` 确认有效 / A11.4b 维护复用链 —— **三者不合并**；
- **A10.1b**：``bundle`` 的 carrier = ``Mapping[BranchId, ContextBundle]``（0 child ⇒ ``{}``）。

**本模块只做搬运 + 配对，不做任何决策。**

```text
调用方（composition / orchestration）
    plan · bundles · branch_return · validator          ← 全部**显式传入**
        ↓
validate_branch(...)
    ① 按 branch_return.branch_id 从 Plan.branches 取 spec（**权威标准**）
    ② 按同一 branch_id 从 bundles 取 bundle（A10.1b carrier）
    ③ validator.validate_spec(...)                      ← **恰好一次**
    ④
        ↓
BranchValidationOutcome(branch_return=同一个对象, validation=[13] 的返回对象)
```

**✅ 允许：** 读 ``plan.branches`` 的 ``branch_id`` 与那一个 ``BranchSpec`` ·
按 key 取 ``bundles`` 里的那一个 ``ContextBundle`` · 调用 ``Validator.validate_spec`` ·
把两个**已经存在**的对象装进配对载体。

**❌ 禁止（逐条对应 §76 的锁）：**

- ❌ 反查 ``[10]`` / ``[09]`` / ``[08]`` / P1 / 任何运行态（输入**全部**由调用方给出）；
- ❌ 构造 ``BranchReturn`` / ``ValidationRecord``（**不重建、不修补**：重造等于换了一个事实载体，
  A11.4b 的"同一次真实事件"当场失效）—— 两个对象都是**原样透传**；
- ❌ 读 ``validation_judgment``（本模块**不 import 它**）：wiring **不得**重算 verdict、
  不得重新解释判定依据、不得再生成一条 ``ValidationRecord``；
- ❌ 读 ``bundle`` 的**字段内容**（尤其 ``completion_criteria``）：一致性核对是 ``[13]`` 的职责；
- ❌ 把接线缺陷吸收成 ``ValidationRecord(passed=False)`` ——
  **FAIL 表示"分支执行事实存在但未通过验证"**，而接线错误表示**"验证流程本身无法建立"**，
  两者**不得**进入同一个失败分类（否则污染 ``[14]``，同 A16.3 在 `architecture.md:2623-2624` 的理由）；
- ❌ 读自报字段（``validation_state`` / ``key_evidence`` / …）—— INV-19 在接线侧**同样成立**；
- ❌ 写运行态（单写者是 ``[10]``）· ❌ 决定 ``PASS → 终态`` / ``FAIL → [14]`` 的去路（下一轮）；
- ❌ 任何状态（无缓存 / registry / 时钟 / IO）—— 每次调用**新建一个**配对载体。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .contracts import (
    BranchId,
    BranchReturn,
    BranchSpec,
    ContextBundle,
    Plan,
    ValidationRecord,
)
from .validation import Validator

__all__ = ["BranchValidationOutcome", "ValidationWiringError", "validate_branch"]


class ValidationWiringError(RuntimeError):
    """接线级显式失败：输入不足以确定 ``(branch_return, spec, bundle)`` 三者。

    ⚠️ **它永远不能被降级成 ``ValidationRecord(passed=False)``** ——
    那会把"验证流程无法建立"写成"分支没做到"，并污染 ``[14]`` 的失败分类。
    """


@dataclass(frozen=True)
class BranchValidationOutcome:
    """一次真实 ``[12] → [13]`` **配对事件的见证**（A11.4b）。

    ⚠️ 本 DTO **只能证明结构不变量**（类型 + 身份一致）。
    "两个对象来自**同一次**事件"这件事，由**单一构造点**（``validate_branch`` 的唯一 return）
    + 全 ``src/`` 只有该处构造它的锁共同保证 —— 与 ``reusable_results.py`` 对
    ``ReusableResultRef`` 的登记同形：DTO 本身不声称能自证 provenance。

    ⚠️ 两个字段都是**原样引用**（不复制内容、不重建对象）⇒
    ``outcome.branch_return is 传入的 branch_return`` 与
    ``outcome.validation is validator 的返回对象`` 在行为上可断言。
    """

    branch_return: BranchReturn
    validation: ValidationRecord

    def __post_init__(self) -> None:
        # 先身份：不靠 duck typing（"对象恰好有 .branch_id"不算）
        if not isinstance(self.branch_return, BranchReturn):
            raise TypeError(
                f"branch_return 必须是 BranchReturn，收到 {self.branch_return!r}"
            )
        if not isinstance(self.validation, ValidationRecord):
            raise TypeError(
                f"validation 必须是 ValidationRecord，收到 {self.validation!r}"
            )
        # 再结构：配对的两半必须属于**同一个分支**
        if self.branch_return.branch_id != self.validation.branch_id:
            raise ValueError(
                "branch_return.branch_id 与 validation.branch_id 必须一致"
                "（A11.4 ① 的身份前提）"
            )


def _spec_for(plan: Plan, branch_id: BranchId) -> BranchSpec:
    """从 ``Plan.branches`` 取该分支的**权威** ``BranchSpec``（A16.3）。

    ⚠️ 唯一"按 ``branch_id`` 找权威标准"的点：找不到 ⇒ **显式失败**，
    **不得**从 bundle 副本反推、也**不得**替调用方猜一个完成标准。
    """
    for spec in plan.branches:
        if spec.branch_id == branch_id:
            return spec
    raise ValidationWiringError(
        f"未知分支 {branch_id!r}：它不在 Plan.branches 里"
        "（接线缺陷；权威完成标准只能来自 Plan.branches，不得猜）"
    )


def validate_branch(
    *,
    plan: Plan,
    bundles: Mapping[BranchId, ContextBundle],
    branch_return: BranchReturn,
    validator: Validator,
) -> BranchValidationOutcome:
    """把**一次** ``[12]`` 的 ``BranchReturn`` 接到 ``[13]``，并交回**配对见证**。

    ``validator`` 是**必需入参、无默认值**：本模块**不装配**判定依据来源
    （A16.2 的 producer 属边界级注入；给默认值就等于替调用方发明判定依据）。
    """
    if not isinstance(plan, Plan):
        raise ValidationWiringError(f"plan 必须是 Plan，收到 {plan!r}")
    if not isinstance(bundles, Mapping):
        raise ValidationWiringError(
            f"bundles 必须是 Mapping[BranchId, ContextBundle]，收到 {bundles!r}"
        )
    if not isinstance(branch_return, BranchReturn):
        raise ValidationWiringError(
            f"branch_return 必须是 BranchReturn，收到 {branch_return!r}"
        )
    if not isinstance(validator, Validator):
        raise ValidationWiringError(f"validator 必须是 Validator，收到 {validator!r}")

    branch_id = branch_return.branch_id
    spec = _spec_for(plan, branch_id)
    if branch_id not in bundles:
        raise ValidationWiringError(
            f"分支 {branch_id} 没有 prepared ContextBundle"
            "（A10.1b：被派发的 child 必有副本；缺失 = 接线缺陷，不是 FAIL）"
        )

    # 唯一判定调用点：`[13]` 拥有判定权（A16.1），本模块只搬运。
    validation = validator.validate_spec(
        branch_return=branch_return, spec=spec, bundle=bundles[branch_id]
    )

    # 唯一构造点：两个**同一个事件里**的对象装进配对见证（A11.4b）。
    return BranchValidationOutcome(branch_return=branch_return, validation=validation)
