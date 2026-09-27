"""``[13] Validation`` 的 seam DTO —— **判定依据事实**（A16.2 / A16.4）。

架构依据：`docs/architecture.md` 2 节 ``[13] Validation``（**Amendment A16**，FROZEN，
含不变量 **INV-19**）；范围与锁清单（LV-1 – LV-8）见 `tests/behavior/README_step5b.md` §72。

**这三层职责不得合并（A16.2 / A16.6）：**

    ValidationJudgmentSource（边界级、注入的可调用）   → 产出**判定依据事实**
        ↓
    ValidationJudgment / CriterionVerdict（**本模块**） → 依据的载体
        ↓
    [13] Validator                                     → **做 PASS/FAIL 判定**并装配
        ↓
    ValidationRecord（**contracts 层的最终载体**）      → 交给终态 / [14]

⚠️ **本模块不是 producer、也不是判定者**：它只定义"判定依据长什么样"。
⚠️ **不定义 `ValidationRecord`** —— 那是契约层已有类型；本模块**不进 `contracts/`**（A16.7）。

**A16.4 的"标准 ↔ 判定"可对照结构 = ``CriterionVerdict``**：把**标准文本**与**是否满足**成对载体化，
于是 ``ValidationRecord.failed_criteria`` 可以从 verdicts **派生**（而不是另造字符串），
`[14]` 拿到的失败条目也**可回指**权威标准、从而映射到 ``FailureClass.COMPLETION_CRITERIA``。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CriterionVerdict", "ValidationJudgment"]


def _require_non_empty_str(value: object, label: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} 必须是非空 str，收到 {value!r}")


def _require_str_tuple(value: object, label: str) -> None:
    if not isinstance(value, tuple):
        raise TypeError(f"{label} 必须是 tuple，收到 {value!r}")
    for item in value:
        if type(item) is not str:
            raise TypeError(f"{label} 的元素必须是 str，收到 {item!r}")


@dataclass(frozen=True)
class CriterionVerdict:
    """**单条**标准的判定依据（producer 产出；`[13]` 消费）。

    ⚠️ ``criterion`` **必须锚定在权威完成标准文本内**（去空白后为**子串或相等**）——
    该规则由 ``[13]`` 在装配前核对（它需要权威文本才能判），
    本 DTO 只保证它是**非空 str**（不复制那条规则）。
    """

    criterion: str
    satisfied: bool
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        # `criterion.strip()` 非空 ⇒ 不可能出现 `"" in authority` 这种"空串永远锚定成功"的漏洞
        _require_non_empty_str(self.criterion, "criterion")
        if not isinstance(self.satisfied, bool):
            raise TypeError(f"satisfied 必须是 bool，收到 {self.satisfied!r}")
        _require_str_tuple(self.evidence, "evidence")


@dataclass(frozen=True)
class ValidationJudgment:
    """producer 的**判定依据事实**（A16.2：它**不**产出 ``ValidationRecord``）。

    ⚠️ ``verdicts`` **至少 1 条**不是形式要求：若允许为空，``all(())`` 在 Python 里是 ``True``
    ⇒ ``[13]`` 会**静默判 PASS** —— 本项目反复禁止的"静默默认"。
    """

    verdicts: tuple[CriterionVerdict, ...]
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.verdicts, tuple):
            raise TypeError(f"verdicts 必须是 tuple，收到 {self.verdicts!r}")
        if not self.verdicts:
            raise ValueError(
                "verdicts 不得为空（空元组会让 all(...) 静默判 PASS；A16.2 要求给出判定依据）"
            )
        for verdict in self.verdicts:
            if not isinstance(verdict, CriterionVerdict):
                raise TypeError(f"verdicts 元素必须是 CriterionVerdict，收到 {verdict!r}")
        _require_non_empty_str(self.detail, "detail")
