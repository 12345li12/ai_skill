"""``[14] Recovery`` 的**输入 seam DTO** —— A17.5 的字段归属三分（范围稿见 README §83）。

架构依据：`docs/architecture.md`

- **A17.5**（`[14] Recovery Router` 的 Amendment A17，FROZEN）：`RecoveryContext` 的字段分三类 ——
  **上游提供的事实** / **`[14]` 计算** / **仅审计**；两条纪律：
  **计算字段不得由上游注入**（否则 `[14]` 被架空）、**审计字段不得参与判定**（否则上游有绕开 A17.1/A17.2 的暗门）；
- **A17.6**：`RecoveryDecision.attempt` = **分支级 attempt**；`ValidationRecord` 与
  `ExecutionPassReport.failures` **都不带 `attempt`** ⇒ 由**上游显式传入**；
- **A17.3**：V1 里 `[14]` 的可达类别只有四个（`COMPLETION_CRITERIA` + `MODEL`/`PERMISSION`/`EXTERNAL_SERVICE`）；
- **A17.4**：L49 六类可复用资产（`CarryForwardKind`）—— 本模块持有该词汇表，
  **不进 `contracts/`**（A17.9 ④ 裁决：实现级 seam 候选）。

**本模块只定义"输入长什么样"**，不做任何判定：

```text
上游（[13] 的 ValidationRecord ／ [11] 经 P1 的 failures ／ 显式 attempt）
        ↓  只装**事实**，不装判定
RecoveryContext（本模块）
        ↓
[14] recovery.decide_recovery(...)      ← 分类 / 路由 / 上限全在那里
```

**⚠️ `failure_kind` 的取值是 A14.4 四值的「字符串投影」**（`"crash"` / `"timeout"` /
`"refused"` / `"network"`），**不是** ``AdapterFailureKind`` ——
`execution.py:7` 冻结"Core 内唯一允许 import `adapters` 的模块"，`result_handoff.py:8` 冻结
"Core 一律不得 import `adapters`"（L11-1 / L11-2 / L12-2）⇒ 接线层以 **duck-typed** 方式取
``failure.failure_kind.value`` 后传入（P1 对 adapter 的 duck-typing 先例）。

**两条构造期不变量（本 DTO 的核心价值）：**

    ① **恰好一路**：`validation` 与 `failure_kind` **恰有一个非 None**
       —— 一次失败事件只有一个来源；两个都缺 / 两个都有 ⇒ 显式失败
       （否则 `[14]` 就得"猜"用哪一路 ⇒ 违反 A17.5 的「审计字段不得参与判定」）
    ② **路径与审计一致**：`validation` 路径 ⇒ `message` / `timestamp` **必须为 None**；
       `failure_kind` 路径 ⇒ **必须是非空 str**（`AdapterFailure` 自身要求两者非空）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import BranchId, ValidationRecord
from .contracts.common import validate_branch_id

__all__ = ["CarryForwardKind", "RecoveryContext"]

#: A14.4 四值的**字符串投影**（V1 闭集；不得增删）。
#:
#: ⚠️ 这是 `[14]` 侧**唯一**认得的运行类别词汇 —— 它**不 import `adapters`**。
#: 与 `AdapterFailureKind` 的等价性由**跨模块锁**保证（测试侧 import adapter 枚举对照）。
_ALLOWED_FAILURE_KINDS = frozenset({"crash", "timeout", "refused", "network"})


class CarryForwardKind(Enum):
    """L49「复用事实、摘要、日志、排除方向、修改和验证状态」的**机读分类位**（A17.4）。

    ⚠️ **实现级 seam**（`[14]` 范围稿裁决 ⑦ / A17.9 ④）：**不进 `contracts/`**。
    将来若 `[14]` 的输出被外部节点直接消费，或追溯矩阵要求契约级稳定，
    **再开 contract amendment**。
    """

    VALIDATION_STATE = "validation_state"
    EXCLUDED_DIRECTIONS = "excluded_directions"
    FACTS = "facts"
    SUMMARY = "summary"
    LOGS = "logs"
    MODIFICATIONS = "modifications"


@dataclass(frozen=True)
class RecoveryContext:
    """`[14]` 的**输入事实**（A17.5 三分：上游提供 / `[14]` 计算 / 仅审计）。

    ⚠️ 本 DTO **只装上游事实与审计信息**：`failure_class` / `recovery_target` /
    `minimal_correction` / `carry_forward` / `confidence` **都不是字段** ——
    它们是 `[14]` 的**计算结果**（A17.5：计算字段不得由上游注入）。
    """

    branch_id: BranchId
    attempt: int
    total_attempt_limit: int
    validation: ValidationRecord | None
    failure_kind: str | None
    message: str | None
    timestamp: str | None

    def __post_init__(self) -> None:
        validate_branch_id(self.branch_id)
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise TypeError(f"attempt 必须是 int，收到 {self.attempt!r}")
        if self.attempt < 1:
            raise ValueError("attempt 从 1 开始计数（9.1：Attempt 1 = 初次执行）")
        if not isinstance(self.total_attempt_limit, int) or isinstance(
            self.total_attempt_limit, bool
        ):
            raise TypeError(
                f"total_attempt_limit 必须是 int，收到 {self.total_attempt_limit!r}"
            )
        if self.total_attempt_limit < 1:
            raise ValueError("total_attempt_limit 至少为 1（9.1 / V1 默认 2）")

        # ---- ① 恰好一路 --------------------------------------------------
        if (self.validation is None) == (self.failure_kind is None):
            raise ValueError(
                "validation 与 failure_kind 必须恰有一个非 None"
                "（一次失败事件只有一个来源；两个都缺/都有都是接线缺陷，"
                "不得让 [14] 去猜用哪一路 —— A17.5）"
            )

        if self.validation is not None:
            if not isinstance(self.validation, ValidationRecord):
                raise TypeError(
                    f"validation 必须是 ValidationRecord | None，收到 {self.validation!r}"
                )
            if self.validation.passed is not False:
                # PASS 永不进入 Recovery（4.3 / 4.4 的分叉是单向的）
                raise ValueError(
                    "只有 FAIL 的 ValidationRecord 才能进入 Recovery"
                    "（PASS → 终态；不得把 PASS 送进失败回边 —— 4.3 / 4.4）"
                )
            if self.validation.branch_id != self.branch_id:
                raise ValueError(
                    "validation.branch_id 必须与 branch_id 一致"
                    "（与 [13] LV-4 / 配对载体同纪律：不得跨分支核对）"
                )
            if self.message is not None or self.timestamp is not None:
                raise ValueError(
                    "validation 路径不得携带 message / timestamp"
                    "（它们是 adapter 路径的审计信息 —— 路径与审计必须一致）"
                )
            return

        # ---- failure_kind 路径 -------------------------------------------
        if type(self.failure_kind) is not str or self.failure_kind not in _ALLOWED_FAILURE_KINDS:
            raise ValueError(
                f"failure_kind 必须是 A14.4 四值之一 {sorted(_ALLOWED_FAILURE_KINDS)}，"
                f"收到 {self.failure_kind!r}（禁止兜底：未知类别不得默认成任何一类）"
            )
        for name in ("message", "timestamp"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError(
                    f"{name} 在 failure_kind 路径上必须是非空 str，收到 {value!r}"
                    "（AdapterFailure 自身要求两者非空）"
                )

    @property
    def failed_criteria(self) -> tuple[str, ...]:
        """**仅审计**：从 `validation` 派生（A17.5；**不参与**分类 / 路由）。

        ⚠️ 它是派生的**只读**属性，不是存储字段 —— 避免与 `validation.failed_criteria` 两处真相。
        """
        if self.validation is None:
            return ()
        return self.validation.failed_criteria
