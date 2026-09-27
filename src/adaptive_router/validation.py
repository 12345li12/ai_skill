"""``[13] Validation`` —— A16 的实现（FEEDBACK 层的**事实判定**）。

架构依据：`docs/architecture.md` 2 节 ``[13] Validation``（**Amendment A16**，FROZEN；
含不变量 **INV-19 — Validation 独立判定**）；范围、锁清单（LV-1 – LV-8）与 probe 计划见
`tests/behavior/README_step5b.md` §72。

**判定权归 ``[13]``，但 ``[13]`` 没有事实发明权（A16.1）。**

    ValidationJudgmentSource（**注入**的可调用，边界级）
        ↓ 只产出**判定依据事实**（ValidationJudgment）
    Validator.validate_spec(...)
        ↓ 做 PASS/FAIL 判定 + 装配
    ValidationRecord（contracts 层）

**流程（九步，显式穷尽 —— 禁静默默认）：**

    1. **身份一致**：`branch_return.branch_id == spec.branch_id`，否则显式失败
    2. **标准一致**：`spec.completion_criteria == bundle.completion_criteria`（**逐字**），否则显式失败
       （A16.3：这是**契约传递错误**，**不判 FAIL** —— 判 FAIL 会把接线缺陷吸收成"分支没做到"）
    3. 调用 `judgment_source` **恰好一次**
    4. **锚定校验**：每条 `criterion` 去空白后必须是权威标准文本的**子串或相等**
    5. `passed = all(v.satisfied for v in verdicts)`
    6. `failed_criteria = tuple(v.criterion for v in verdicts if not v.satisfied)`
    7. `evidence` = 各条目证据的**保序去重**
    8. `detail` = 判定理由（非空，由 seam DTO 保证）
    9. 装配 `ValidationRecord`（**唯一构造点**）

**两条设计性质：**

- 第 5/6 步使 ``ValidationRecord`` 的两条契约不变量**结构性成立**（全满足 ⇒ 空；任一不满足 ⇒ 非空）
  ⇒ 本模块**不可能**造出违反契约的记录；
- **INV-19 的落点就是第 5 步**：``passed`` 只由 **verdicts** 决定 ——
  ``branch_return.validation_state``（分支**自报**）**不在** ``passed`` 的输入里；
  它至多作为 producer 的**证据之一**出现在某条 verdict 的 ``evidence`` 中。

**明确不做：** 不重执行 branch · 不修改执行事实 · 不生成不存在的证据 · 不复制自报状态作为结论 ·
不写运行态（单写者是 ``[10]``）· **不 import ``[14]``**（``PASS → 终态`` / ``FAIL → [14]`` 的分叉属 wiring）·
不读时钟 / 不做 IO。
"""

from __future__ import annotations

from typing import Callable

from .contracts import (
    BranchReturn,
    BranchSpec,
    ContextBundle,
    ContractError,
    ValidationRecord,
)
from .validation_judgment import ValidationJudgment

__all__ = ["Validator"]


def _ordered_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    """**保序去重**（判定证据汇总用；不排序 —— 顺序也是信息）。"""
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return tuple(seen)


class Validator:
    """``[13]`` —— 做 PASS/FAIL 判定并装配 ``ValidationRecord``。

    ``judgment_source`` 是**注入的可调用**（裁定 ③：不建类），签名冻结为
    ``(BranchReturn, str) -> ValidationJudgment`` —— 第二个参数是**权威完成标准文本**。
    """

    def __init__(
        self,
        *,
        judgment_source: Callable[[BranchReturn, str], ValidationJudgment],
    ) -> None:
        self._judgment_source = judgment_source

    def validate_spec(
        self,
        *,
        branch_return: BranchReturn,
        spec: BranchSpec,
        bundle: ContextBundle,
    ) -> ValidationRecord:
        """按 §72.5 的九步流程判定一个分支。

        ⚠️ **权威完成标准 = ``spec.completion_criteria``**（A16.3：[05] 的原始承诺）；
        ``bundle.completion_criteria`` 是 **A10.4 的传递副本**，**只用于一致性核对** ——
        它**不得**成为判定输入（否则 bundle 会变成隐藏的 judgment source）。
        ⚠️ ``bundle`` 入场而非传两个裸字符串：副本只能从 ``bundle`` **取**，
        "同一个字符串传两次"这种假校验在**结构上不可能**。
        """
        # ① 身份一致 —— 否则会拿 A 分支的回传去比 B 分支的标准（静默错配）
        if branch_return.branch_id != spec.branch_id:
            raise ContractError(
                f"回传的分支 {branch_return.branch_id} 与完成标准的分支 {spec.branch_id} 不一致"
                "（身份必须一致，不得跨分支核对）"
            )

        # ② 标准一致 —— **逐字**比较，不做任何规范化（规范化会掩盖真实的契约漂移）
        if spec.completion_criteria != bundle.completion_criteria:
            raise ContractError(
                f"{spec.branch_id} 的完成标准在 Plan 与 ContextBundle 之间不一致"
                "（A16.3：这是契约传递错误，不判 FAIL）"
            )

        # ③ 判定依据事实 —— **恰好一次**（`[13]` 不自行合成判定）
        judgment = self._judgment_source(branch_return, spec.completion_criteria)
        if not isinstance(judgment, ValidationJudgment):
            raise ContractError(
                f"judgment_source 必须返回 ValidationJudgment，收到 {judgment!r}"
            )

        # ④ 锚定校验 —— 非空 + 每条标准必须锚定在权威文本内（否则 = 引入了不存在的标准）
        if not judgment.verdicts:
            raise ContractError(
                "判定依据不得为空（空元组会让 all(...) 静默判 PASS）"
            )
        for verdict in judgment.verdicts:
            if verdict.criterion.strip() not in spec.completion_criteria:
                raise ContractError(
                    f"判定条目 {verdict.criterion!r} 不是权威完成标准的片段"
                    "（producer 不得引入权威文本之外的标准）"
                )

        # ⑤⑥⑦⑧ 判定与派生
        passed = all(verdict.satisfied for verdict in judgment.verdicts)
        failed_criteria = tuple(
            verdict.criterion for verdict in judgment.verdicts if not verdict.satisfied
        )
        evidence = _ordered_unique(
            tuple(item for verdict in judgment.verdicts for item in verdict.evidence)
        )

        # ⑨ 唯一构造点（passed / failed_criteria 都由上面的派生结果给出）
        return ValidationRecord(
            branch_id=branch_return.branch_id,
            passed=passed,
            evidence=evidence,
            failed_criteria=failed_criteria,
            detail=judgment.detail,
        )
