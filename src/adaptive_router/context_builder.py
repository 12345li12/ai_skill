"""``[08] Context Builder`` —— Amendment A10 的实现（FROZEN）。

架构依据：`docs/architecture.md` 2 节 ``[08] Context Builder``（**Amendment A10**）；
范围与结构锁见 `tests/behavior/README_step5b.md` §26。

**它做什么：** 把**已经形成**的事实**装配**成逐 child 的最小上下文包
（``Mapping[BranchId, ContextBundle]``）。**纯投影，不发明事实**（A8.2 同一条纪律）。

**接收者与基数（A10.2 / A10.2b）：**

```text
preparation（本模块）  每个 Plan.branches child **预构造**恰好一个 bundle
dispatch（不在本模块） 只有经 [09]/[10] 实际启动/派发的 child 才向 [11] 逐次传递一个
DIRECT / STAGED        ⇒ {}（**正常结果**，不是"漏做"）
main thread            **不是**接收者（它没有 completion_criteria，不得为凑数发明一个）
```

⚠️ 本模块**不预测** ``[09]`` 的裁决：它构造 bundle 时**还不知道**谁会真正派发，
所以基数按 ``Plan.branches``（预构造），**不是**按"实际派发"。

**精确 read-set（A10.7 的实现口径）：**

- 读 ``plan.branches`` 的 ``branch_id`` / ``goal`` / ``completion_criteria`` **三个字段**
  （不读 ``mode``：``Plan`` 契约已保证 mode↔branches 一致，0 分支时键集自然是空）；
- 读 ``facts_by_branch`` 与 ``confirmed_conclusions_by_branch``，**按该 branch_id 取**；
- ``output_format`` 机械引用 ``contracts.BRANCH_RETURN_FIELDS``
  （L37 十项的唯一权威清单）——**不手抄十项、不做自然语言总结**。

**deliberate non-read（N1 已批准）：** 本模块**不读** ``RoutingPlan`` /
``ProjectContext`` / ``AuthorizationContext``，它们**也不进入** ``build()`` 的签名：

- ``ProjectContext`` / ``AuthorizationContext`` 的事实已由 ``ExecutionContextFacts``
  承载（A10.3 明令 ``[08]`` **不得重新解释**它们）；
- ``RoutingPlan`` 在边界上与 bundle **同行**交给 ``[11]``，但**不是** bundle 构造的输入 ——
  本模块**不为"证明用过"制造交叉校验**。

**本模块不做的事：** 不做 relevance 判断（不挑事实、不裁剪、不把一个 child 的 facts
复用给另一个）；不读文件系统、不重新加载项目、不读 session/bypass；
不构造 ``Plan`` / ``ModelSelection`` / ``BranchState``；不实现 ``[09]``/``[10]``/``[11]``。
"""

from __future__ import annotations

from typing import Mapping

from .contracts import (
    BRANCH_RETURN_FIELDS,
    BranchId,
    ContextBundle,
    Plan,
)
from .execution_context_facts import ExecutionContextFacts

__all__ = ["ContextBuilder", "ContextBuilderError"]


class ContextBuilderError(RuntimeError):
    """``[08]`` 的显式失败：输入形状与 ``Plan.branches`` **不精确覆盖**。"""


def _require_exact_keys(
    actual: Mapping[object, object],
    expected: set[str],
    *,
    label: str,
) -> None:
    """**唯一**的精确覆盖校验点（A10.2b：不多、不少）。

    只接受**恰好相等**：subset（少一个 child）与 superset（多一个不属于本计划的 child）
    都**显式失败** —— 与 A3 "禁止 fail-open"、A6.4 "缺失即显式失败"同一纪律。
    """
    if set(actual) == set(expected):
        return
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    raise ContextBuilderError(
        f"{label} 的 branch_id 必须与 Plan.branches 精确覆盖"
        f"（缺少 {missing}；多余 {extra}）"
    )


class ContextBuilder:
    """``[08]`` 的构造器：只装配，不判定 relevance、不发明事实。"""

    def build(
        self,
        *,
        plan: Plan,
        facts_by_branch: Mapping[BranchId, ExecutionContextFacts],
        confirmed_conclusions_by_branch: Mapping[BranchId, tuple[str, ...]],
    ) -> Mapping[BranchId, ContextBundle]:
        """为 ``plan.branches`` 的每个 child 预构造一个 ``ContextBundle``。

        0 child（``DIRECT`` / ``STAGED_SINGLE_AGENT``）⇒ 返回 ``{}``。
        两张映射的 ``branch_id`` 集合必须与 ``plan.branches`` **精确覆盖**，
        否则 ``ContextBuilderError``；两个映射都**必填**（无默认值）。
        """
        expected = {spec.branch_id for spec in plan.branches}
        _require_exact_keys(facts_by_branch, expected, label="facts_by_branch")
        _require_exact_keys(
            confirmed_conclusions_by_branch,
            expected,
            label="confirmed_conclusions_by_branch",
        )
        bundles: dict[BranchId, ContextBundle] = {}
        for spec in plan.branches:
            facts = facts_by_branch[spec.branch_id]
            bundles[spec.branch_id] = ContextBundle(
                goal=spec.goal,
                background=facts.background,
                constraints=facts.constraints,
                allowed_operations=facts.allowed_operations,
                forbidden_operations=facts.forbidden_operations,
                output_format=" / ".join(BRANCH_RETURN_FIELDS),
                completion_criteria=spec.completion_criteria,
                confirmed_conclusions=confirmed_conclusions_by_branch[spec.branch_id],
                excluded=(),
                includes_history=False,
            )
        return bundles
