"""contracts.runtime —— 运行期事实契约（可变状态的快照 / 声明）。

架构依据：`docs/architecture.md` 第 11.1 节（runtime.py 归属）
依赖方向：仅依赖 ``contracts.common``。

本模块内容（架构冻结清单）：
  SessionGateResult    [01] 三态判定结果（UNASKED / DISABLED / ENABLED）
  UnavailableReason    "无法给出事实"的合法理由（L15/L17）
  CapabilitySnapshot   [03] 环境能力快照
  ProjectContext       [17] 项目上下文（**不可信输入**，INV-02）
  AuthorizationContext 授权事实聚合（**Judgment 之前完成**）
  ContextBundle        [08] 最小上下文包（INV-11）
  RoutingRun           [04.5] 聚合边界
  BranchState          [10/18] 9 个极简状态
  AgentRun             [10] 单次 Agent 运行
  BranchReturn         [12] 分支回传（严格 L37 的 10 项）
  ValidationRecord     [13] 验证记录
  CapabilityMismatch   [11] Adapter 能力不匹配上报（INV-12）

⚠️ 本模块承载 INV-02：ProjectContext 是不可信输入，只能收窄不能放宽。
   落实方式是 ``AuthorizationContext`` 构造期比较 —— 项目声明若比系统
   边界更宽，直接拒绝构造，而不是"相信项目配置"。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .common import (
    AgentId,
    BranchId,
    BranchTerminal,
    Confidence,
    ContractError,
    EffortLevel,
    ExecutionValue,
    ModelTier,
    RunId,
    RunTerminal,
    TerminalState,
    validate_agent_id,
    validate_branch_id,
    validate_run_id,
)

__all__ = [
    "SessionGateResult",
    "UnavailableReason",
    "CapabilitySnapshot",
    "ProjectContext",
    "AuthorizationContext",
    "ContextBundle",
    "RoutingRun",
    "BranchState",
    "AgentRun",
    "BranchReturn",
    "ValidationRecord",
    "CapabilityMismatch",
]


def _require_enum(value: Any, expected: type[Enum], field_name: str) -> None:
    if not isinstance(value, expected):
        raise ContractError(f"{field_name} 必须是 {expected.__name__}，收到 {value!r}")


def _require_non_empty_str(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field_name} 必须是非空字符串，收到 {value!r}")


def _require_str_tuple(value: Any, field_name: str) -> None:
    if not isinstance(value, tuple):
        raise ContractError(f"{field_name} 必须是 tuple（不可变契约）")
    for item in value:
        _require_non_empty_str(item, f"{field_name} 元素")


def _normalized(value: str) -> str:
    """仅用于**比较副本**的规范化：去空白 + 转小写。

    ⚠️ 绝不修改真正保存的 provider model ID —— 只影响比较结果。
    ``"STRONG"`` / ``" Strong "`` 与 ``"strong"`` 必须被同等拦下。
    """
    return value.strip().lower()


# --------------------------------------------------------------------------
# [01] SessionGate
# --------------------------------------------------------------------------


class SessionGateResult(Enum):
    """[01] SessionGate 的三态（架构 4.1）。

    ⚠️ ``DISABLED`` **不是** Router 的一种运行模式，而是 **bypass**。
       判定标准：DISABLED 下 Router 主执行链**一次都不能被进入**，
       而不是被进入后提前返回（INV-01 / INV-S1）。
    """

    UNASKED = "unasked"
    DISABLED = "disabled"
    ENABLED = "enabled"

    @property
    def enters_router(self) -> bool:
        """是否允许进入 Project Context 与 Router Core（唯一判据）。"""
        return self is SessionGateResult.ENABLED

    @property
    def must_ask_first(self) -> bool:
        """UNASKED 比 DISABLED 更严：该回复只含启用问题并立即结束（L5）。"""
        return self is SessionGateResult.UNASKED


# --------------------------------------------------------------------------
# [03] Capability
# --------------------------------------------------------------------------


class UnavailableReason(Enum):
    """"无法给出事实"的合法理由。``unknown`` 是一等状态，不是异常。"""

    TOOL_FAILURE = "tool_failure"
    MANIFEST_CHANGED = "manifest_changed"
    REQUEST_REJECTED = "request_rejected"
    CONCURRENCY_ANOMALY = "concurrency_anomaly"
    ENVIRONMENT_SWITCHED = "environment_switched"
    SESSION_RESUMED = "session_resumed"
    NOT_PROBED = "not_probed"


@dataclass(frozen=True)
class CapabilitySnapshot:
    """[03] 环境能力快照（L13 / L15 / L17）。

    架构 3 条要点：
      1. 它是"执行期观测的事实"（PERCEPTION），不是决策。
      2. Probe 有副作用且可失效；Snapshot 只读可缓存 —— 本契约只表达快照。
      3. 不做探测、不做重查判断。何时重查是 [03] 的行为（第 3 步）。

    ``unknown`` 用法：某项能力无法确认时置 ``None`` 并给出 ``unavailable_reason``，
    **不得**用 ``False`` 冒充"不支持"（那会触发不该有的降级）。
    """

    subagents: bool | None
    async_background: bool | None
    concurrency_limit: int | None
    available_models: tuple[str, ...]
    available_effort_levels: tuple[EffortLevel, ...]
    state_wait_append_stop: bool | None
    hot_switch: bool | None
    reliable_cost_data: bool | None
    probed_dimensions: tuple[str, ...]
    unavailable_reason: UnavailableReason | None
    probed_at: str

    def __post_init__(self) -> None:
        for name in (
            "subagents",
            "async_background",
            "state_wait_append_stop",
            "hot_switch",
            "reliable_cost_data",
        ):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise ContractError(f"{name} 必须是 bool | None，收到 {value!r}")

        if self.concurrency_limit is not None:
            if not isinstance(self.concurrency_limit, int) or isinstance(
                self.concurrency_limit, bool
            ):
                raise ContractError("concurrency_limit 必须是 int | None")
            if self.concurrency_limit < 0:
                raise ContractError("concurrency_limit 不得为负")

        _require_str_tuple(self.available_models, "available_models")
        if not isinstance(self.available_effort_levels, tuple):
            raise ContractError("available_effort_levels 必须是 tuple")
        for level in self.available_effort_levels:
            _require_enum(level, EffortLevel, "available_effort_levels 元素")

        _require_str_tuple(self.probed_dimensions, "probed_dimensions")
        _require_non_empty_str(self.probed_at, "probed_at")
        if self.unavailable_reason is not None:
            _require_enum(
                self.unavailable_reason, UnavailableReason, "unavailable_reason"
            )

        # unknown 必须带理由，否则无法区分"没探"和"探了但失败"
        unknowns = [
            name
            for name in (
                "subagents",
                "async_background",
                "concurrency_limit",
                "state_wait_append_stop",
                "hot_switch",
                "reliable_cost_data",
            )
            if getattr(self, name) is None
        ]
        if unknowns and self.unavailable_reason is None:
            raise ContractError(
                f"存在无法确认的能力 {unknowns}，必须给出 unavailable_reason"
            )

    @property
    def has_real_subagents(self) -> bool:
        """是否有真实子代理。**未确认时返回 False，但调用方必须看 unknown**。"""
        return self.subagents is True

    @property
    def is_unknown(self) -> bool:
        return self.unavailable_reason is not None


# --------------------------------------------------------------------------
# [17] Project Context
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectContext:
    """[17] Project Context Loader 的输出（L15 / L53）。

    ⚠️ **不可信输入**（INV-02）。``AGENTS.md`` 等是沙箱外文本：
       只能**收窄**边界，**不能放宽**权限。Core 只走"取事实"通道，
       不走"取指令"通道。

    加载时机（架构 2 节 [17] 冻结）：
      仅在 SessionGate = ENABLED 后进入；首次进入 Router Core 时**轻量加载**；
      可为空；禁止深度扫描；可缓存；仅 R5 失效条件出现时重读。
    """

    root: str
    present: bool
    boundary_dirs: tuple[str, ...]
    writable: tuple[str, ...]
    denied: tuple[str, ...]
    readme_refs: tuple[str, ...]
    agents_md_present: bool
    agents_md_digest: str | None
    test_commands: tuple[str, ...]
    high_risk_files: tuple[str, ...]
    model_preference: str | None
    approval_preference: str | None
    source_path: str | None
    read_at: str
    trust: str = "untrusted_project_input"

    def __post_init__(self) -> None:
        _require_non_empty_str(self.root, "root")
        if not isinstance(self.present, bool):
            raise ContractError("present 必须是 bool")
        for name in (
            "boundary_dirs",
            "writable",
            "denied",
            "readme_refs",
            "test_commands",
            "high_risk_files",
        ):
            _require_str_tuple(getattr(self, name), name)
        if not isinstance(self.agents_md_present, bool):
            raise ContractError("agents_md_present 必须是 bool")
        if self.agents_md_present and self.agents_md_digest is None:
            raise ContractError("声明存在 AGENTS.md 时必须给出 digest（来源标记）")
        if self.agents_md_digest is not None:
            _require_non_empty_str(self.agents_md_digest, "agents_md_digest")
        _require_non_empty_str(self.read_at, "read_at")
        if self.trust != "untrusted_project_input":
            raise ContractError(
                "ProjectContext.trust 固定为 'untrusted_project_input'（INV-02），"
                f"收到 {self.trust!r}"
            )

    @classmethod
    def absent(cls, root: str, read_at: str) -> "ProjectContext":
        """项目无配置时的合法空值 —— **不是错误，正常工作**（L53）。"""
        return cls(
            root=root,
            present=False,
            boundary_dirs=(),
            writable=(),
            denied=(),
            readme_refs=(),
            agents_md_present=False,
            agents_md_digest=None,
            test_commands=(),
            high_risk_files=(),
            model_preference=None,
            approval_preference=None,
            source_path=None,
            read_at=read_at,
        )


# --------------------------------------------------------------------------
# 授权事实聚合（Judgment 之前完成）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationContext:
    """所有授权事实的**唯一聚合点**（架构 2 节 Authorization Context Builder）。

    ⚠️ 位置约束：权限聚合**必须在 Approval Judgment 之前完成**。
       否则 Judgment 会依赖一个比自己更晚形成的东西，时间顺序不成立。

    ⚠️ 纯事实，**不做审批**。它回答"当前被授权做什么"，
       不回答"是否需要审批"（后者是 ApprovalJudgment）。

    INV-02 的类型层落实：``project_denied`` 可以收窄 ``system_denied``，
    但项目声明**不得**放宽系统边界 —— 构造期直接拒绝。
    """

    session_authorized: bool
    user_granted_scopes: tuple[str, ...]
    system_denied: tuple[str, ...]
    project_denied: tuple[str, ...]
    platform_denied: tuple[str, ...]
    effective_denied: tuple[str, ...]
    user_read_only_override: bool
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_authorized, bool):
            raise ContractError("session_authorized 必须是 bool")
        if not isinstance(self.user_read_only_override, bool):
            raise ContractError("user_read_only_override 必须是 bool")
        for name in (
            "user_granted_scopes",
            "system_denied",
            "project_denied",
            "platform_denied",
            "effective_denied",
        ):
            _require_str_tuple(getattr(self, name), name)
        _require_non_empty_str(self.rationale, "rationale")

        higher = set(self.system_denied) | set(self.platform_denied)
        effective = set(self.effective_denied)

        # INV-02：项目只能收窄，不能放宽
        weakened = higher - effective
        if weakened:
            raise ContractError(
                f"effective_denied 缺少更高层限制 {sorted(weakened)} —— "
                "Project Context 只能收窄权限，不能扩大（INV-02）"
            )
        if not set(self.project_denied) <= effective:
            raise ContractError("effective_denied 必须包含 project_denied")

    @property
    def is_read_only(self) -> bool:
        """用户是否已把本次执行限制为只读（R4 / MODIFY 的常见结果）。"""
        return self.user_read_only_override


# --------------------------------------------------------------------------
# [08] Context
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextBundle:
    """[08] 最小上下文包（L35 / INV-11）。

    子代理**只接收**：目标、必要背景/文件/数据、约束、允许与禁止操作、
    输出格式、完成标准、相关已确认结论。

    ``excluded`` 记录排除理由 —— 使"最小化"可审计，而不是靠感觉。
    """

    goal: str
    background: tuple[str, ...]
    constraints: tuple[str, ...]
    allowed_operations: tuple[str, ...]
    forbidden_operations: tuple[str, ...]
    output_format: str
    completion_criteria: str
    confirmed_conclusions: tuple[str, ...]
    excluded: tuple[str, ...]
    includes_history: bool

    def __post_init__(self) -> None:
        for name in ("goal", "output_format", "completion_criteria"):
            _require_non_empty_str(getattr(self, name), name)
        for name in (
            "background",
            "constraints",
            "allowed_operations",
            "forbidden_operations",
            "confirmed_conclusions",
            "excluded",
        ):
            _require_str_tuple(getattr(self, name), name)
        if not isinstance(self.includes_history, bool):
            raise ContractError("includes_history 必须是 bool")
        overlap = set(self.allowed_operations) & set(self.forbidden_operations)
        if overlap:
            raise ContractError(f"同一操作不得既允许又禁止：{sorted(overlap)}")


# --------------------------------------------------------------------------
# RoutingRun / BranchState / AgentRun
# --------------------------------------------------------------------------


class BranchState(Enum):
    """[10] 维护的极简状态（L47）。**9 个，不得增删。**"""

    WAITING = "waiting"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    ADOPTED = "adopted"
    REJECTED = "rejected"
    OVERRIDDEN = "overridden"


_TERMINAL_BRANCH_STATES = frozenset(
    {
        BranchState.COMPLETED,
        BranchState.FAILED,
        BranchState.STOPPED,
        BranchState.ADOPTED,
        BranchState.REJECTED,
        BranchState.OVERRIDDEN,
    }
)


@dataclass(frozen=True)
class AgentRun:
    """[10] 单次 Agent 运行的事实记录。"""

    run_id: RunId
    branch_id: BranchId
    agent_id: AgentId
    attempt: int
    state: BranchState
    started_at: str | None = None
    ended_at: str | None = None
    handle: str | None = None

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        validate_branch_id(self.branch_id)
        validate_agent_id(self.agent_id)
        _require_enum(self.state, BranchState, "state")
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise ContractError("attempt 必须是 int")
        if self.attempt < 1:
            raise ContractError("attempt 从 1 开始计数（架构 9.1）")

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_BRANCH_STATES


@dataclass(frozen=True)
class RoutingRun:
    """聚合边界（架构 4.5）。

    RoutingRun = 一次顶层用户任务进入 Router 到产生一个终态的完整执行周期。

    ⚠️ 架构 4.5 硬说明：``terminal`` 指 **RoutingRun 自身完成收敛后的最终终态**，
       **不是任意一个 Branch 首次进入 TerminalState**。Run 必须**同时**满足：

         ① 所属分支均已终止
         ② Recovery / Approval 等控制流均已收敛

       才允许产生 RunTerminal 并进入 [15c] Cost Summary。

       第 2 步审查裁决（阻塞问题 B）：原实现只检查了 ①，
       于是存在"所有 Branch 都终止、Approval 仍 WAITING、RunTerminal 却可创建"
       的合法构造 —— 这正是架构明令禁止的。

    ``control_flow_converged`` 是条件 ② 的事实位。默认 ``False``：
    **不得把它默认当作真**。第 3 步再决定它由 Approval / Recovery 的哪些
    状态产生；本契约只要求它在进入终态时必须为真。

    ⚠️ **``branches`` 是 attempt history（Architecture Amendment A13.3）。**
    它不是"每个 branch 恰好一条"，而是同一 RoutingRun 内**全部 attempt 记录的历史**：

      · ``(branch_id, attempt)`` 在 ``branches`` 内**唯一**；
      · **新 attempt 只能在前一 attempt 已终止之后产生**（与 4.4 的
        ``attempt < total_attempt_limit``、9.1 的"Attempt 1 = 初次执行 /
        Attempt 2 = 唯一一次自动恢复机会"一致）；
      · 因此读取方一律按**最新 attempt** 取该 branch 的现状 ——
        见 ``branch_by_id()`` 与 ``branch_terminals``。

    构造期强制这两条（违反即 ``ContractError``），使"attempt 只能顺序推进"
    成为**机器可验证的不变量**，而不是只写在架构正文里的文字。
    """

    run_id: RunId
    branches: tuple[AgentRun, ...]
    terminal: RunTerminal | None
    attempt: int
    total_attempt_limit: int
    control_flow_converged: bool = False

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)
        if not isinstance(self.branches, tuple):
            raise ContractError("branches 必须是 tuple")
        for branch in self.branches:
            if not isinstance(branch, AgentRun):
                raise ContractError(f"branches 元素必须是 AgentRun，收到 {branch!r}")
            if branch.run_id != self.run_id:
                raise ContractError(
                    f"分支 {branch.branch_id} 的 run_id 与 RoutingRun 不一致"
                    "（成本与状态会跨 run 串号）"
                )
        if not isinstance(self.control_flow_converged, bool):
            raise ContractError("control_flow_converged 必须是 bool")

        # ---- A13.3：branches 是 attempt history ----------------------------
        keys = [(branch.branch_id, branch.attempt) for branch in self.branches]
        if len(set(keys)) != len(keys):
            raise ContractError(
                "同一 (branch_id, attempt) 不得在 branches 内重复"
                "（A13.3：branches 是 attempt history）"
            )
        highest: dict[BranchId, int] = {}
        for branch in self.branches:
            if (
                branch.branch_id not in highest
                or branch.attempt > highest[branch.branch_id]
            ):
                highest[branch.branch_id] = branch.attempt
        for branch in self.branches:
            if branch.attempt != highest[branch.branch_id] and not branch.is_terminal:
                raise ContractError(
                    f"分支 {branch.branch_id} 的 attempt {branch.attempt} 尚未终止，"
                    f"不得存在更高的 attempt {highest[branch.branch_id]}"
                    "（A13.3：新 attempt 只能在前一 attempt 已终止后产生）"
                )

        if self.terminal is not None:
            if not isinstance(self.terminal, RunTerminal):
                raise ContractError("terminal 必须是 RunTerminal | None")
            if self.terminal.run_id != self.run_id:
                raise ContractError("RunTerminal.run_id 必须与 RoutingRun 一致")
            # 条件 ①
            if not self.all_branches_terminated:
                unfinished = [b.branch_id for b in self.branches if not b.is_terminal]
                raise ContractError(
                    "RoutingRun 不得在分支未全部终止时进入终态"
                    f"（未终止分支：{unfinished}）"
                )
            # 条件 ②
            if not self.control_flow_converged:
                raise ContractError(
                    "RoutingRun 不得在 Recovery / Approval 等控制流未收敛时进入终态"
                    "（architecture 4.5 条件 ②）"
                )

        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise ContractError("attempt 必须是 int")
        if self.attempt < 1:
            raise ContractError("attempt 从 1 开始计数")
        if self.total_attempt_limit < 1:
            raise ContractError("total_attempt_limit 至少为 1（架构 9.1 默认 2）")

    @property
    def all_branches_terminated(self) -> bool:
        """条件 ①：所属分支均已终止。

        ⚠️ A13.3：在"新 attempt 只能在前一 attempt 已终止后产生"这一构造期不变量成立时，
        逐条检查 attempt history 与"每个 branch 的最新 attempt 均已终止"**等价**
        （每个 branch 至多一个未终止的 attempt）—— 因此本属性**行为不变**，
        这里只补语义说明。
        """
        return all(branch.is_terminal for branch in self.branches)

    @property
    def is_converged(self) -> bool:
        """Run 是否已收敛 —— [15c] Cost Summary 的进入条件。

        **必须同时满足条件 ① 与 ②**（架构 4.5）。
        失败一次不得开始最终汇总。
        """
        return (
            self.terminal is not None
            and self.all_branches_terminated
            and self.control_flow_converged
        )

    @property
    def branch_terminals(self) -> tuple[BranchTerminal, ...]:
        """已终止分支的终止记录 —— **每个 branch 最多一条**（取最新 attempt）。

        架构 4.5：分支终态**不等于** Run 终态 —— 未终止的分支不出现在这里，
        也不代表 Run 可以结束。

        ⚠️ A13.3：``branches`` 是 attempt history，因此**必须按 branch 取最新 attempt** ——
        否则同一 branch 的每个已终止 attempt 都会各出一条记录（一个 branch 会被报两次终结）。
        输出顺序沿用 ``branches`` 的插入顺序。
        """
        latest: dict[BranchId, AgentRun] = {}
        for branch in self.branches:
            if (
                branch.branch_id not in latest
                or branch.attempt > latest[branch.branch_id].attempt
            ):
                latest[branch.branch_id] = branch
        records: list[BranchTerminal] = []
        for branch in self.branches:
            if branch.attempt != latest[branch.branch_id].attempt:
                continue
            terminal_state = _branch_terminal_state(branch.state)
            if terminal_state is not None:
                records.append(
                    BranchTerminal(branch_id=branch.branch_id, state=terminal_state)
                )
        return tuple(records)

    def branch_by_id(self, branch_id: BranchId) -> AgentRun:
        """该 branch 的**最新 attempt** 记录（A13.3）。

        ``branches`` 是 attempt history ⇒ 必须返回 ``attempt`` **最大**的那条，
        而不是第一个匹配项（否则重试后可能取到已经失败的历史 attempt）。
        """
        found: AgentRun | None = None
        for branch in self.branches:
            if branch.branch_id != branch_id:
                continue
            if found is None or branch.attempt > found.attempt:
                found = branch
        if found is None:
            raise ContractError(f"未知分支 {branch_id!r}")
        return found


# 9 态 → 6 值终止原因的映射。仅此映射，不含业务判断：
# 它只回答"这个分支是怎么结束的"。非终止态返回 None。
_BRANCH_TERMINAL_MAP: dict[BranchState, TerminalState] = {
    BranchState.COMPLETED: TerminalState.SUCCESS,
    BranchState.ADOPTED: TerminalState.SUCCESS,
    BranchState.REJECTED: TerminalState.TERMINAL_FAILURE,
    BranchState.FAILED: TerminalState.TERMINAL_FAILURE,
    BranchState.STOPPED: TerminalState.USER_CANCELLED,
    BranchState.OVERRIDDEN: TerminalState.SUPERSEDED,
}


def _branch_terminal_state(state: BranchState) -> TerminalState | None:
    return _BRANCH_TERMINAL_MAP.get(state)


# --------------------------------------------------------------------------
# [12] BranchReturn —— 严格 L37 的 10 项
# --------------------------------------------------------------------------

BRANCH_RETURN_FIELDS: tuple[str, ...] = (
    "branch_id",  # 分支
    "agent_id",  # 分支归属（运行实例）
    "attempt",  # 分支归属（第几次尝试）
    "model",  # 计划/请求/实际模型及确认来源
    "effort",  # 计划/请求/实际推理强度及确认来源
    "conclusion_summary",  # 结论摘要
    "key_evidence",  # 关键证据
    "validation_state",  # 验证状态
    "residual_risks",  # 遗留风险
    "main_agent_todo",  # 主代理待办
)


@dataclass(frozen=True)
class BranchReturn:
    """[12] 分支回传。**严格等于 L37 规定的结构，多一个字段都不许。**

    L37 原文：仅返回 分支；计划/请求/实际模型及确认来源；计划/请求/实际
    推理强度及确认来源；结论摘要；关键证据；验证状态；遗留风险；主代理待办。

    **不得返回完整思维过程或重复背景。**

    ⚠️ 契约通过 ``__dataclass_fields__`` 白名单强制这条 —— 这是
       "禁止返回思维过程"唯一可机械检查的形式。

    ⚠️ **INV-04 的真正落点。**
       三值计划/请求/实际在此时才同时存在，所以"计划配置不得伪装成实际配置"
       只能在这里强制。构造期检查（见 ``_require_model_dimension``）：

         model.planned    必须 ModelTier
         model.requested  必须非空 str，且不得是抽象档位
         model.actual     observed 时必须非空 str，且不得是抽象档位
         effort.planned / requested / actual  必须 EffortLevel

       注意：``ExecutionValue[ModelTier, str, str]`` 的泛型标注在运行时
       **不做检查**，所以必须显式做领域校验。
    """

    branch_id: BranchId
    agent_id: AgentId
    attempt: int
    model: ExecutionValue[ModelTier, str, str]
    effort: ExecutionValue[EffortLevel, EffortLevel, EffortLevel]
    conclusion_summary: str
    key_evidence: tuple[str, ...]
    validation_state: str
    residual_risks: tuple[str, ...]
    main_agent_todo: tuple[str, ...]

    def __post_init__(self) -> None:
        validate_branch_id(self.branch_id)
        validate_agent_id(self.agent_id)
        if not isinstance(self.attempt, int) or isinstance(self.attempt, bool):
            raise ContractError("attempt 必须是 int")
        if self.attempt < 1:
            raise ContractError("attempt 从 1 开始计数")
        if not isinstance(self.model, ExecutionValue):
            raise ContractError("model 必须是 ExecutionValue")
        if not isinstance(self.effort, ExecutionValue):
            raise ContractError("effort 必须是 ExecutionValue")
        _require_non_empty_str(self.conclusion_summary, "conclusion_summary")
        _require_str_tuple(self.key_evidence, "key_evidence")
        _require_non_empty_str(self.validation_state, "validation_state")
        _require_str_tuple(self.residual_risks, "residual_risks")
        _require_str_tuple(self.main_agent_todo, "main_agent_todo")

        # INV-04 的**领域级**强制。
        #
        # Python 的 Generic[TPlan, TRequest, TActual] 在运行时**不检查**类型，
        # 所以 `ExecutionValue[ModelTier, str, str]` 这个标注本身拦不住
        # `actual="strong"`。必须在这里做领域检查，否则 README 里
        # "INV-04 已机械强制" 这句话不成立。
        self._require_model_dimension()
        self._require_effort_dimension()

    def _require_model_dimension(self) -> None:
        """模型维：planned 是抽象档位；requested / actual 是提供方 model ID。"""
        if not isinstance(self.model.planned, ModelTier):
            raise ContractError(
                "model.planned 必须是 ModelTier（[06] 只产出抽象档位），"
                f"收到 {self.model.planned!r}"
            )
        tier_values = {tier.value for tier in ModelTier} | {
            tier.name.lower() for tier in ModelTier
        }

        _require_non_empty_str(self.model.requested, "model.requested")
        if _normalized(self.model.requested) in tier_values:
            raise ContractError(
                "model.requested 不得是抽象档位"
                "（[11] Adapter 请求的是提供方 model ID，不是档位）"
            )

        if self.model.observed:
            _require_non_empty_str(self.model.actual, "model.actual")
            # INV-04 的核心：不得把计划配置伪装成实际配置
            if _normalized(self.model.actual) in tier_values:
                raise ContractError(
                    "model.actual 不得是抽象档位"
                    "（INV-04：不得把计划配置伪装成实际配置）"
                )

    def _require_effort_dimension(self) -> None:
        """强度维：三个值都必须是 EffortLevel。"""
        for field_name in ("planned", "requested"):
            value = getattr(self.effort, field_name)
            if not isinstance(value, EffortLevel):
                raise ContractError(
                    f"effort.{field_name} 必须是 EffortLevel，收到 {value!r}"
                )
        if self.effort.observed and not isinstance(self.effort.actual, EffortLevel):
            raise ContractError(
                f"effort.actual 已观测时必须是 EffortLevel，收到 {self.effort.actual!r}"
            )

    @classmethod
    def allowed_field_names(cls) -> tuple[str, ...]:
        """L37 的 10 项白名单。"""
        return BRANCH_RETURN_FIELDS

    @classmethod
    def assert_shape_is_frozen(cls) -> None:
        """若有人给 BranchReturn 加字段，这里会失败。"""
        actual = tuple(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        if actual != BRANCH_RETURN_FIELDS:
            raise ContractError(
                "BranchReturn 结构已偏离 L37 冻结清单："
                f"期望 {BRANCH_RETURN_FIELDS}，实际 {actual}"
            )


# --------------------------------------------------------------------------
# [13] Validation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationRecord:
    """[13] 验证记录。证据检查 + 完成标准核对。"""

    branch_id: BranchId
    passed: bool
    evidence: tuple[str, ...]
    failed_criteria: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        validate_branch_id(self.branch_id)
        if not isinstance(self.passed, bool):
            raise ContractError("passed 必须是 bool")
        _require_str_tuple(self.evidence, "evidence")
        _require_str_tuple(self.failed_criteria, "failed_criteria")
        _require_non_empty_str(self.detail, "detail")

        if self.passed and self.failed_criteria:
            raise ContractError("PASS 时不得携带 failed_criteria")
        if not self.passed and not self.failed_criteria:
            raise ContractError("FAIL 时必须指出未满足的完成标准，供 [14] 分类")


# --------------------------------------------------------------------------
# [11] Adapter 能力不匹配（INV-12）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityMismatch:
    """[11] Adapter 上报的能力不匹配（INV-03 / INV-12）。

    ⚠️ Adapter **永远没有调度决策权**：不得模拟能力，不得自行降级，
       不得修改 ``RoutingPlan``。它只能上报，由 Core 重新决策。

    正确流程（架构 5.1）：
      Core 请求 → Adapter 检查 → CapabilityMismatch + alternatives
      → 返回 Core → 重新决策 → 必要时重新 Cost Estimate
      → Approval Judgment → 必要时 Gate → 重新请求 Adapter
    """

    requested: str
    supported: str
    alternatives: tuple[str, ...]
    detail: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.requested, "requested")
        _require_non_empty_str(self.supported, "supported")
        _require_str_tuple(self.alternatives, "alternatives")
        _require_non_empty_str(self.detail, "detail")

    @property
    def has_alternatives(self) -> bool:
        return len(self.alternatives) > 0
