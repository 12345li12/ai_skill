"""session —— [01] SessionGate：Router 的唯一入口点与唯一 gate。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 [01] Session / SessionGate
  - 4.1 Session BYPASS 流
  - 7.2 INV-S1 / INV-S2

⚠️ 本模块承载 INV-01：
   SessionGate 未返回 ENABLED 时，**不得进入 Router 主执行链**。
   判定标准是"主执行链一次都不能被进入"，而不是"被进入后提前返回" ——
   所以这里只产出判定与 bypass 理由，不做任何探测、读取或调度。

⚠️ 三条不应出现在本模块的结构约束（L9）：

   1. 不得携带模型 / 推理强度字段  —— 关闭期间不得因本协议改变执行方式
   2. 不得携带任何计划 / 分支      —— 不得继续未启动计划，也不得借此重启
   3. 不得携带平台 / 界面控制字段  —— 本协议不声称控制平台自身路由或界面设置

   这三条由测试以 ``__dataclass_fields__`` 白名单机械核对。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .contracts import SessionGateResult

__all__ = [
    "SessionState",
    "DisableCause",
    "RouterBypassReason",
    "EmittableReply",
    "PersistenceProvider",
    "SessionGate",
    "SessionGateError",
    "requires_bypass",
]


class SessionState(Enum):
    """会话级协议状态位。**默认仅限当前会话**（L7）。"""

    UNASKED = "unasked"
    DISABLED = "disabled"
    ENABLED = "enabled"


class DisableCause(Enum):
    """关闭原因。用于区分"明确拒绝"与"未明确回答"。"""

    NONE = "none"
    REFUSED = "refused"
    UNANSWERED = "unanswered"


class RouterBypassReason(Enum):
    """BYPASS 的合法理由。

    与 ``SessionGateResult`` 一一对应但语义不同：前者是"状态"，
    后者是"因此必须旁路"的理由，便于审计与复盘时归因。
    """

    NOT_YET_ASKED = "not_yet_asked"
    PROTOCOL_DISABLED = "protocol_disabled"
    # 以下三者不由本模块产出，但同属架构 6.3 的 bypass / 降级理由枚举：
    NO_REAL_SUBAGENT = "no_real_subagent"
    USER_SCOPED_OVERRIDE = "user_scoped_override"
    HIGHER_RULE_LIMIT = "higher_rule_limit"


@dataclass(frozen=True)
class EmittableReply:
    """首次启用询问所允许的回复形状（L5）。

    L5 原文：发出该首次询问的回复必须**只包含启用问题并立即结束**，
    禁止在同一回复中继续用户任务、读取文件、调用工具、创建子代理或加载本 Skill。

    契约层能机械强制的部分就是本类型：``payload`` 与 ``side_effects`` 必须为空。
    """

    kind: str
    must_end_immediately: bool
    payload: tuple[str, ...]
    side_effects: tuple[str, ...]


class PersistenceProvider(Protocol):
    """跨会话持久化后端。

    INV-15：**默认不存在**这样的后端。只有用户明确 opt-in 后才绑定。
    """

    def save(self, payload: Any) -> None:  # pragma: no cover - 协议声明
        ...


class SessionGateError(RuntimeError):
    """SessionGate 的调用方违规（非契约错误）。"""


@dataclass
class SessionGate:
    """[01] SessionGate —— 只读一个会话状态位，输出唯一判定。

    ⚠️ 唯一输入是**会话级状态位**。不得读项目文件、不得读工作目录、
       不得探测能力（那些属 [03] / [17]）。

    ⚠️ 默认不持久化：``persistence is None`` 是**结构事实**，
       不是一个可以被改错的布尔开关（INV-15）。
    """

    state: SessionState = SessionState.UNASKED
    asked: bool = False
    user_sent_next_message: bool = False
    disable_cause: DisableCause = DisableCause.NONE
    persistence: PersistenceProvider | None = None
    self_invoked: bool = field(default=False)

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    def evaluate(self) -> SessionGateResult:
        """唯一判定入口。**只有 ENABLED 允许进入 Router 主执行链**（INV-01）。"""
        if self.state is SessionState.ENABLED:
            return SessionGateResult.ENABLED
        if self.state is SessionState.DISABLED:
            return SessionGateResult.DISABLED
        # UNASKED：即使已经问过，只要用户还没发下一条消息，**仍然是 UNASKED**。
        # 不得提前判定为"未明确回答"（L5）。
        return SessionGateResult.UNASKED

    def bypass_reason(self) -> RouterBypassReason | None:
        """若判定不是 ENABLED，给出可审计的旁路理由。"""
        result = self.evaluate()
        if result is SessionGateResult.ENABLED:
            return None
        if result is SessionGateResult.UNASKED:
            return RouterBypassReason.NOT_YET_ASKED
        return RouterBypassReason.PROTOCOL_DISABLED

    # ------------------------------------------------------------------
    # UNASKED：首次询问
    # ------------------------------------------------------------------

    def mark_asked(self) -> None:
        """记录"首次询问已发出"。只应发生一次（L5）。"""
        self.asked = True

    def may_ask_now(self) -> bool:
        """是否允许发出首次启用询问。

        L7：拒绝或未明确回答后**不重复询问**。
        """
        if self.state is not SessionState.UNASKED:
            return False
        return not self.asked

    def compose_first_question(self) -> EmittableReply:
        """构造首次询问的回复 —— **只含问题，立即结束**。

        若已经问过，再次构造属于违规：L5 只允许询问一次。
        """
        if not self.may_ask_now():
            raise SessionGateError(
                "首次启用询问只允许发出一次（L5）；"
                f"当前 state={self.state.value}, asked={self.asked}"
            )
        return EmittableReply(
            kind="question_only",
            must_end_immediately=True,
            payload=(),
            side_effects=(),
        )

    # ------------------------------------------------------------------
    # 状态迁移
    # ------------------------------------------------------------------

    def note_user_message(self) -> None:
        """记录"用户实际发送了下一条消息"这一事实。

        **这是 UNASKED → DISABLED 的必要条件**（L5）：
        只有"已问过 + 用户实际发了下一条消息 + 仍未回答"才可判为未明确回答。
        """
        if not self.asked or self.state is not SessionState.UNASKED:
            return
        self.user_sent_next_message = True
        self.state = SessionState.DISABLED
        self.disable_cause = DisableCause.UNANSWERED

    def record_decision(self, *, enable: bool) -> None:
        """记录用户的明确取舍。

        ``enable=False`` → 静默关闭（L7）。
        ``enable=True``  → 进入自动观察模式。
        用户可随时反向操作（L45：支持开启 / 关闭协议）。
        """
        if enable:
            self.state = SessionState.ENABLED
            self.disable_cause = DisableCause.NONE
        else:
            self.state = SessionState.DISABLED
            self.disable_cause = DisableCause.REFUSED

    def note_self_invocation(self) -> None:
        """用户显式调用 ``$adaptive-agent-router``。

        这是 DISABLED 状态下**唯一合法的再入途径**（L7：直到用户主动开启）。
        """
        self.self_invoked = True
        self.state = SessionState.ENABLED
        self.disable_cause = DisableCause.NONE

    # ------------------------------------------------------------------
    # 持久化（默认不存在）
    # ------------------------------------------------------------------

    def bind_persistence(self, provider: PersistenceProvider) -> None:
        """显式 opt-in 后才绑定跨会话持久化后端（INV-15）。

        ⚠️ 绑定动作本身**不写入任何东西** —— 是否保存由调用方另行决定。
        """
        self.persistence = provider


def requires_bypass(result: SessionGateResult) -> bool:
    """该判定是否要求 Router **完全旁路**，而不是"进入后提前返回"。

    INV-01 / INV-S1：BYPASS 时 Router 主执行链一次都不能被进入。
    """
    return not result.enters_router
