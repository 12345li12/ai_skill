"""``[11] Runtime Adapter`` 的接缝契约 —— Amendment A14.3 / A14.4 的实现级 DTO。

架构依据：`docs/architecture.md` 2 节 ``[11] Runtime Adapter``（**Amendment A14**，FROZEN）；
范围、锁清单（L11-1 – L11-7）与 probe 计划见 `tests/behavior/README_step5b.md` §50。

**这一层只做三件事（A14.4）：**

    能力不匹配（平台不支持所请求的能力）  →  ``CapabilityMismatch``（**已冻结于 contracts/**，本模块不重复）
    非能力失败（崩溃 / 超时 / 拒绝 / 网络） →  ``AdapterFailure``（本模块）
    无法归类                              →  ``AdapterError``（**显式失败**，绝不静默归类）

⚠️ **本模块刻意不 import 任何项目内模块**：三个 DTO 全部由 ``str`` 与自身枚举构成 ——
于是 A14.8 的"`adapters/` 只消费冻结输入"与 L11-3 的导入白名单**天然成立**，
且 ``AgentStartReceipt`` 携带的是**平台事实**而非 Core 概念。

⚠️ **不得预置字段（A14.3）**：``AdapterStartReceipt`` 只有 ``handle`` / ``started_at`` 两个字段；
**新增字段需 amendment**。``AdapterFailure`` 只有 ``failure_kind`` / ``message`` / ``timestamp``；
**不得**携带业务结论、重试建议或降级建议（那属 ``[14]`` / ``[06]``）。

⚠️ **``handle`` ≠ ``agent_id``（A14.2）**：``handle`` 是**平台句柄**（owner = ``[11]``）；
``agent_id`` 是 **Core 内部运行实例身份**（owner = ``[10]``，commit 时 mint）。
两者**禁止混用、禁止互相派生**，因此本模块**不出现** ``agent_id`` 这个字段。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    "AdapterError",
    "AdapterFailure",
    "AdapterFailureKind",
    "AdapterStartReceipt",
]


class AdapterFailureKind(Enum):
    """A14.4 冻结的 **V1 闭集**。**四个成员，不得增删。**

    这四个值是**运行类别**，**不携带任何重试 / 降级决定** ——
    是否重试属 ``[14] Recovery``，是否降级属 ``[06]`` / ``[14]``。
    """

    CRASH = "crash"  # 已派发后运行环境崩溃 / 非下列三类的运行期错误
    TIMEOUT = "timeout"  # 等待预算内没有收到结果
    REFUSED = "refused"  # 平台拒绝执行（策略 / 配额 / 权限）
    NETWORK = "network"  # 传输层失败（**没有**任何平台侧执行开始的证据）


@dataclass(frozen=True)
class AdapterStartReceipt:
    """启动事实（A14.3）：**最小两字段，不得预置其它字段**。

    路径：``[11] → P1 → [10].commit_started(branch_id, attempt, handle, started_at)``。

    ⚠️ **P1 必须把它拆成显式标量**再交给 ``[10]``（A14.6 / L11-7）：
    ``[10]`` **不得** import 本类型 —— 它只需要两个字符串。
    ⚠️ **没有启动事实 ⇒ 不得提交 ``RUNNING``**（A13.2 的失败路径闭合规则）。
    """

    handle: str
    started_at: str

    def __post_init__(self) -> None:
        # `type is str` + 非空：handle 是**平台事实**，[10] 不得发明、P1 只搬运
        if type(self.handle) is not str or not self.handle.strip():
            raise ValueError(f"handle 必须是非空 str（平台事实），收到 {self.handle!r}")
        if type(self.started_at) is not str or not self.started_at.strip():
            raise ValueError(
                f"started_at 必须是非空 str（显式传入，见 A14.3），收到 {self.started_at!r}"
            )


@dataclass(frozen=True)
class AdapterFailure:
    """非能力失败的唯一载体（A14.4）。

    ⚠️ **禁止**把它塞进 ``BranchReturn`` —— ``BranchReturn`` 是**结果事实**，
    其 ``conclusion_summary`` / ``validation_state`` 必填非空，
    没有真实结果时填任何值都是**制造事实**（A8.2）。
    """

    failure_kind: AdapterFailureKind
    message: str
    timestamp: str

    def __post_init__(self) -> None:
        if not isinstance(self.failure_kind, AdapterFailureKind):
            raise TypeError(
                f"failure_kind 必须是 AdapterFailureKind，收到 {self.failure_kind!r}"
            )
        if type(self.message) is not str or not self.message.strip():
            raise ValueError(f"message 必须是非空 str，收到 {self.message!r}")
        if type(self.timestamp) is not str or not self.timestamp.strip():
            raise ValueError(
                f"timestamp 必须是非空 str（显式传入），收到 {self.timestamp!r}"
            )


class AdapterError(RuntimeError):
    """**无法归类**的失败 —— 显式失败，**绝不静默归类**（A14.4 / 裁定 ③）。

    ⚠️ 兜底是**禁止**的：把未知异常当成 ``TIMEOUT``（或任何其它 kind）都是**发明事实**。

    本异常**可选携带**一个已分类的 ``AdapterFailure``：``start`` 用**返回值**表达三种结局
    （回执 / 能力不匹配 / 非能力失败），而 ``append`` / ``stop`` 的签名是 ``-> None``，
    它们的失败只能经异常通道传出 —— 携带 ``failure`` 才不至于丢掉 A14.4 的**闭集分类**。
    """

    def __init__(self, message: str, *, failure: AdapterFailure | None = None) -> None:
        super().__init__(message)
        self.failure = failure
