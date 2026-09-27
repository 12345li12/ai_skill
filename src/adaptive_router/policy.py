"""[02] Policy + Authorization Context Builder —— CONTROL。

架构依据：`docs/architecture.md`

- 2 节 ``[02] Policy`` —— 优先级序（L11）、模式（观察/审批）、运行期干预（L45）
- 2 节 ``Authorization Context Builder`` —— 所有授权事实的**唯一聚合点**，
  且必须在 Approval Judgment **之前**完成
- Amendment A3 —— ``EffectivePlanningConstraints``（实现级 DTO，**不进** contracts）

本模块的职责边界（Step 5B.1 冻结）：

1. **只做决定，不做执行。** L45 的 9 类干预只被**识别与路由**，
   全部 ``executed=False`` —— 真正生效（追加 / 停止 / 重建分支）属 ``[10]``。
2. **不重实现 SessionGate。** ``[02]`` 位于**已经 gated** 的路径里，
   它只**消费**上游已成立的授权事实（``session_authorized``），
   不重新决定 Session 状态，也不认识 BYPASS。
3. **不改 ``contracts/``。** 授权聚合复用冻结的 ``AuthorizationContext``；
   新增的 ``PolicyFacts`` / ``EffectivePolicy`` / ``EffectivePlanningConstraints``
   都是**实现级** DTO，只做输入规范化与显式产出。

**禁止 fail-open：** ``auto_split_allowed=True`` 是 Policy **显式**判断的结果
（"没有任何更高层限制"），每个键都必须给出 ``rationale``。
fail-open 指的是 Planner 在 Policy 缺失 / 为 ``None`` 时自己假设 ``True`` ——
那是 5B.2 的 seam，不在这里。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .approval_constraints import (
    EffectiveApprovalConstraints as _EffectiveApprovalConstraints,
)
from .contracts import AuthorizationContext
from .planning_constraints import (
    EffectivePlanningConstraints as _EffectivePlanningConstraints,
)

__all__ = [
    "KEY_APPROVAL_MODE",
    "KEY_AUTO_SPLIT",
    "KEY_MODEL_CHOICE",
    "KEY_PROTOCOL",
    "KEY_STOP_BRANCHES",
    "MODES",
    "EffectiveApprovalConstraints",
    "EffectivePlanningConstraints",
    "EffectivePolicy",
    "Intervention",
    "InterventionRoute",
    "Policy",
    "PolicyError",
    "PolicyFacts",
    "PolicyLayer",
    "PolicyResult",
]


class PolicyError(ValueError):
    """Policy 的输入或查询不成立。

    宁可显式失败，也不要静默兜底 —— 兜底会让"没被授权"看起来像"被授权"。
    """


# --------------------------------------------------------------------------
# 输入规范化（实现级 DTO，不属于 contracts/）
# --------------------------------------------------------------------------

#: ``[02]`` 输出的模式（架构 2 节 ``[02]``："模式（观察/审批）"）。
MODES: tuple[str, ...] = ("observe", "approval")


def _require_mode(value: object, name: str) -> None:
    if value not in MODES:
        raise PolicyError(f"{name} 必须是 {MODES} 之一，得到 {value!r}")


class Intervention(Enum):
    """L45 的运行期干预命令（**9 类**）。

    计数口径：L45 原文按 `、` 分组为 7 组，其中两组为斜杠并列
    （"开启/关闭协议"、"关闭/恢复自动分流"），展开后为 9 条。
    Amendment A3.7 把"仅用当前模型"与"调高/调低或指定模型"当作
    **两件不同**干预，故模型那一组不可合并 —— 唯一自洽读数为 9。

    ``docs/architecture.md`` 8 节原记"8 类"为计数笔误，已在 5B.1 窄幅订正
    （Test ID 不变）。
    """

    ENABLE_PROTOCOL = "enable_protocol"
    DISABLE_PROTOCOL = "disable_protocol"
    USE_CURRENT_MODEL = "use_current_model"
    DISABLE_AUTO_SPLIT = "disable_auto_split"
    RESUME_AUTO_SPLIT = "resume_auto_split"
    APPROVE_ALL = "approve_all"
    STOP_BRANCHES = "stop_branches"
    SET_MODEL_TIER = "set_model_tier"
    SWITCH_MODE = "switch_mode"


@dataclass(frozen=True)
class PolicyFacts:
    """``[02]`` 的**纯输入**：优先级序需要的全部事实，规范化后一次性传入。

    ⚠️ 它**不是**事实的来源。所有字段都由上游（``[01]`` SessionGate、
    Approval Gate 的 MODIFY、项目适配、用户输入）已经确定；
    本模块不读文件、不读环境变量、不调用工具 —— 它只是纯 resolver。

    ``session_authorized`` 是**被消费**的上游结论，不是本模块的判断。

    ``user_read_only_override`` 是一条**显式用户授权事实**。
    Approval Gate 的 ``MODIFY`` 是它的**典型来源之一**
    （架构 2 节 Approval Gate："可以，但只允许读、不允许写"），
    **不是唯一来源** —— 用户当前指令同样可以直接产生该事实（R4）。
    本模块只**转发**这条事实，不发明它，也**不属** L45 干预。
    """

    session_authorized: bool
    system_denied: tuple[str, ...]
    developer_denied: tuple[str, ...]
    safety_denied: tuple[str, ...]
    platform_denied: tuple[str, ...]
    user_denied: tuple[str, ...]
    project_denied: tuple[str, ...]
    user_granted_scopes: tuple[str, ...]
    user_commands: tuple[Intervention, ...]
    user_read_only_override: bool
    project_model_preference: str | None
    project_approval_preference: str | None
    protocol_default_mode: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_authorized, bool):
            raise PolicyError("session_authorized 必须是 bool")
        if not isinstance(self.user_read_only_override, bool):
            raise PolicyError("user_read_only_override 必须是 bool")
        for name in (
            "system_denied",
            "developer_denied",
            "safety_denied",
            "platform_denied",
            "user_denied",
            "project_denied",
            "user_granted_scopes",
        ):
            value = getattr(self, name)
            if not isinstance(value, tuple) or any(
                not isinstance(item, str) for item in value
            ):
                raise PolicyError(f"{name} 必须是 str 的 tuple")
        if not isinstance(self.user_commands, tuple) or any(
            not isinstance(item, Intervention) for item in self.user_commands
        ):
            raise PolicyError("user_commands 必须是 Intervention 的 tuple")
        if self.project_model_preference is not None and not isinstance(
            self.project_model_preference, str
        ):
            raise PolicyError("project_model_preference 必须是 str 或 None")
        if self.project_approval_preference is not None:
            _require_mode(self.project_approval_preference, "project_approval_preference")
        _require_mode(self.protocol_default_mode, "protocol_default_mode")


# --------------------------------------------------------------------------
# 输出（全部显式产出，没有"不传就默认放开"的捷径）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectivePolicy:
    """优先级序解算后的**生效策略**（每个键只有一个 reconciled 结论）。"""

    auto_split_enabled: bool
    require_approval_for_all: bool
    use_current_model: bool
    protocol_enabled: bool
    stop_branches_requested: bool
    model_preference: str | None
    mode: str


# ``EffectivePlanningConstraints`` 是 ``[02] → [05]`` 的**共享 seam 类型**，
# 定义在 ``planning_constraints.py``（中性模块），此处 re-export ——
# 既保持 5B.1 已冻结的 ``adaptive_router.policy.EffectivePlanningConstraints``
# 可用，又避免 ``[05]`` 反向依赖本模块。
EffectivePlanningConstraints = _EffectivePlanningConstraints

# ``EffectiveApprovalConstraints`` 是 ``[02] → Approval Judgment`` 的**共享 seam
# 类型**（Amendment A6），同理由 ``approval_constraints.py`` re-export ——
# 使将来的 ``approval_judgment.py`` 只依赖中性 DTO，**不依赖整个本模块**。
EffectiveApprovalConstraints = _EffectiveApprovalConstraints


@dataclass(frozen=True)
class InterventionRoute:
    """一条干预的**路由意图** —— 不是执行结果。

    ``executed`` 在本步恒为 ``False``：``[02]`` 只声明"该由谁去做"，
    真正追加 / 停止 / 重建需要 ``[10]`` 的真实能力（TEST-POLICY-003 = PARTIAL）。
    """

    intervention: Intervention
    target: str
    reason: str
    requires_argument: bool
    executed: bool

    def __post_init__(self) -> None:
        for name in ("target", "reason"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PolicyError(f"{name} 必须是非空 str")
        for name in ("requires_argument", "executed"):
            if not isinstance(getattr(self, name), bool):
                raise PolicyError(f"{name} 必须是 bool")


@dataclass(frozen=True)
class PolicyResult:
    """一次 ``resolve()`` 的全部产出（三件套 + 两个 seam 投影 + 干预路由）。

    ``planning`` 与 ``approval`` 是**两条独立 seam 的投影**：

        planning  → [05] Planner        （Amendment A3）
        approval  → Approval Judgment   （Amendment A6）

    两者都只暴露下游**真正需要**的那一个事实，而不是整份 ``effective``。
    """

    effective: EffectivePolicy
    planning: EffectivePlanningConstraints
    approval: EffectiveApprovalConstraints
    authorization: AuthorizationContext
    recognized_interventions: tuple[Intervention, ...]
    routes: tuple[InterventionRoute, ...]

    def route_for(self, intervention: Intervention) -> InterventionRoute:
        """取本次**已被用户下达**的干预的路由。

        未下达的干预没有本次路由 —— 询问它是调用方的编程错误，显式失败。
        """
        for route in self.routes:
            if route.intervention is intervention:
                return route
        raise PolicyError(
            f"{intervention} 不在本次用户干预中，没有可返回的路由意图"
        )


# --------------------------------------------------------------------------
# 优先级序（L11）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyLayer:
    """优先级序里的一层。

    ``sources`` 是本层在 :class:`PolicyFacts` 里**全部**限制集字段名；
    空元组表示本层不提供"限制集"（``THIS_PROTOCOL`` 只提供模式默认值）。

    ⚠️ 一层可以有**多个**来源。L11 的第三层是「**安全与权限**」，
    它由 ``safety_denied`` **和** ``platform_denied`` 共同构成 ——
    平台权限限制属于这一层，**不是**独立的一层。
    """

    name: str
    label: str
    sources: tuple[str, ...]
    is_user_command: bool = False


#: 优先级序（高 → 低，L11）—— 冻结顺序，不得重排，**恰好 7 层**。
#:
#: ⚠️ 第三层是「安全与权限」，不是"只有 Safety"：
#: 平台权限限制（``platform_denied``）与安全限制同属这一层，共同高于用户当前指令。
_PRIORITY: tuple[PolicyLayer, ...] = (
    PolicyLayer("SYSTEM", "system", ("system_denied",)),
    PolicyLayer("DEVELOPER", "developer", ("developer_denied",)),
    PolicyLayer("SAFETY_PERMISSIONS", "safety", ("safety_denied", "platform_denied")),
    PolicyLayer("USER_CURRENT", "user", ("user_commands",), is_user_command=True),
    PolicyLayer("USER_OLDER_VALID", "user_older", ("user_denied",)),
    PolicyLayer("PROJECT_CONFIG", "project", ("project_denied",)),
    PolicyLayer("THIS_PROTOCOL", "protocol", ()),
)

# --------------------------------------------------------------------------
# 策略键 —— 优先级序作用的对象
# --------------------------------------------------------------------------

KEY_AUTO_SPLIT = "auto_split"
KEY_APPROVAL_MODE = "approval_mode"
KEY_MODEL_CHOICE = "model_choice"
KEY_PROTOCOL = "protocol"
KEY_STOP_BRANCHES = "stop_branches"

#: 各键的**显式**默认值（"没有任何更高层表态"时的基线）。
_KEY_DEFAULT: dict[str, bool] = {
    KEY_AUTO_SPLIT: True,
    KEY_APPROVAL_MODE: False,
    KEY_MODEL_CHOICE: False,
    KEY_PROTOCOL: True,
    KEY_STOP_BRANCHES: False,
}

#: 默认值的理由 —— 必须是**显式 allow/deny**，不是"忘了表态"。
_KEY_DEFAULT_REASON: dict[str, str] = {
    KEY_AUTO_SPLIT: "no higher-priority split restriction",
    KEY_APPROVAL_MODE: "no higher-priority restriction on requiring approval",
    KEY_MODEL_CHOICE: "no higher-priority restriction on model choice",
    KEY_PROTOCOL: "no higher-priority restriction on the protocol switch",
    KEY_STOP_BRANCHES: "no higher-priority restriction on stopping branches",
}

#: 用户当前指令（第 4 层）对各键的表态。
#:
#: 需要**参数**的干预（``SET_MODEL_TIER`` / ``SWITCH_MODE``）不在此表：
#: 没有参数就无从知道用户要调高还是调低、要切到哪一侧，
#: 因此它们**只路由、不表态**，绝不允许猜一个方向。
_COMMAND_STANCE: dict[Intervention, tuple[str, bool]] = {
    Intervention.DISABLE_AUTO_SPLIT: (KEY_AUTO_SPLIT, False),
    Intervention.RESUME_AUTO_SPLIT: (KEY_AUTO_SPLIT, True),
    Intervention.APPROVE_ALL: (KEY_APPROVAL_MODE, True),
    Intervention.USE_CURRENT_MODEL: (KEY_MODEL_CHOICE, True),
    Intervention.ENABLE_PROTOCOL: (KEY_PROTOCOL, True),
    Intervention.DISABLE_PROTOCOL: (KEY_PROTOCOL, False),
    Intervention.STOP_BRANCHES: (KEY_STOP_BRANCHES, True),
}


@dataclass(frozen=True)
class _Verdict:
    """单个策略键的解算结果 —— 值 + 理由（理由必须指向表态的那一层）。"""

    value: bool
    reason: str


# --------------------------------------------------------------------------
# 干预路由表（5B.1 冻结：干预 → 必须据此行动的承载节点）
# --------------------------------------------------------------------------

_ROUTES: tuple[InterventionRoute, ...] = (
    InterventionRoute(
        intervention=Intervention.ENABLE_PROTOCOL,
        target="session",
        reason="协议开关改变会话生命周期状态（L45「开启/关闭协议」）",
        requires_argument=False,
        executed=False,
    ),
    InterventionRoute(
        intervention=Intervention.DISABLE_PROTOCOL,
        target="session",
        reason="协议开关改变会话生命周期状态（L45「开启/关闭协议」）",
        requires_argument=False,
        executed=False,
    ),
    InterventionRoute(
        intervention=Intervention.USE_CURRENT_MODEL,
        target="model_router",
        reason="模型来源约束由 [06] Model Router 落实（A3.7）",
        requires_argument=False,
        executed=False,
    ),
    InterventionRoute(
        intervention=Intervention.SET_MODEL_TIER,
        target="model_router",
        reason="调高/调低/指定档位由 [06] Model Router 落实（A3.7）",
        requires_argument=True,
        executed=False,
    ),
    InterventionRoute(
        intervention=Intervention.DISABLE_AUTO_SPLIT,
        target="planner",
        reason="分流权限经 EffectivePlanningConstraints 作用于 [05] Planner",
        requires_argument=False,
        executed=False,
    ),
    InterventionRoute(
        intervention=Intervention.RESUME_AUTO_SPLIT,
        target="planner",
        reason="分流权限经 EffectivePlanningConstraints 作用于 [05] Planner",
        requires_argument=False,
        executed=False,
    ),
    InterventionRoute(
        intervention=Intervention.APPROVE_ALL,
        target="approval_judgment",
        reason=(
            "「全部先审批」已解析为 require_approval_for_all=True；"
            "是否需要暂停由 Approval Judgment 决定 —— "
            "Approval Gate 只做暂停/交还控制权/等待人类输入，不是 Policy 命令的执行器"
        ),
        requires_argument=False,
        executed=False,
    ),
    InterventionRoute(
        intervention=Intervention.SWITCH_MODE,
        target="policy",
        reason=(
            "观察/审批模式本就是 [02] 自己的输出；需目标模式参数，"
            "由 [02] 在拿到参数后形成 mode"
        ),
        requires_argument=True,
        executed=False,
    ),
    InterventionRoute(
        intervention=Intervention.STOP_BRANCHES,
        target="agent_manager",
        reason="停止分支需 [10] 的真实能力；[02] 只声明意图（TEST-POLICY-003）",
        requires_argument=False,
        executed=False,
    ),
)

_ROUTE_BY_INTERVENTION: dict[Intervention, InterventionRoute] = {
    route.intervention: route for route in _ROUTES
}


def _sorted_union(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """去重并排序 —— 让相同输入必然得到相同输出（结果可比较、可审计）。"""
    return tuple(sorted({item for group in groups for item in group}))


def _dedup(commands: tuple[Intervention, ...]) -> tuple[Intervention, ...]:
    """按用户下达顺序去重：``recognized_interventions`` 是"下达过什么"。"""
    ordered: list[Intervention] = []
    for command in commands:
        if command not in ordered:
            ordered.append(command)
    return tuple(ordered)


# --------------------------------------------------------------------------
# [02] Policy
# --------------------------------------------------------------------------


class Policy:
    """L11 优先级序的**唯一**实现，并产出授权聚合与分流约束。"""

    #: 优先级序（高 → 低，L11）—— 冻结顺序，不得重排。
    PRIORITY: tuple[PolicyLayer, ...] = _PRIORITY

    def resolve(self, facts: PolicyFacts) -> PolicyResult:
        """把纯事实解算成生效策略 + 规划约束 + 授权聚合 + 干预路由。"""
        if not isinstance(facts, PolicyFacts):
            raise PolicyError(
                f"resolve() 需要 PolicyFacts（显式输入），得到 {type(facts).__name__}"
            )

        auto_split = self._verdict(facts, KEY_AUTO_SPLIT)
        approval_verdict = self._verdict(facts, KEY_APPROVAL_MODE)
        model_choice = self._verdict(facts, KEY_MODEL_CHOICE)
        protocol = self._verdict(facts, KEY_PROTOCOL)
        stopping = self._verdict(facts, KEY_STOP_BRANCHES)

        effective = EffectivePolicy(
            auto_split_enabled=auto_split.value,
            require_approval_for_all=approval_verdict.value,
            use_current_model=model_choice.value,
            protocol_enabled=protocol.value,
            stop_branches_requested=stopping.value,
            # 用户当前指令压制项目偏好；压制后不保留"被压制的偏好"以免误导
            model_preference=(
                None if model_choice.value else facts.project_model_preference
            ),
            # 第 6 层（项目审批偏好）> 第 7 层（协议默认模式）
            mode=facts.project_approval_preference or facts.protocol_default_mode,
        )

        planning = EffectivePlanningConstraints(
            auto_split_allowed=effective.auto_split_enabled,
            rationale=auto_split.reason,
        )

        # A6：``[02]`` 是 approval-policy 的**唯一**解算者。
        # ⚠️ 这里必须保留 ``approval.reason`` —— 5B.1 曾经只用 ``.value``
        # 而把该 verdict 的理由丢掉，那样会**永久丢失**
        # "这个强制审批结论是怎么由 L11 优先级解析出来的"这一审计证据。
        approval = EffectiveApprovalConstraints(
            require_approval_for_all=effective.require_approval_for_all,
            rationale=approval_verdict.reason,
        )

        recognized = _dedup(facts.user_commands)
        return PolicyResult(
            effective=effective,
            planning=planning,
            approval=approval,
            authorization=self._authorize(facts),
            recognized_interventions=recognized,
            routes=tuple(_ROUTE_BY_INTERVENTION[command] for command in recognized),
        )

    # -- 内部：优先级序 --------------------------------------------------

    def _verdict(self, facts: PolicyFacts, key: str) -> _Verdict:
        """按 L11 从高到低找**第一个**对 ``key`` 表态的层。

        更高层"没表态"（``None``）不等于"允许" —— 只是把决定权让给下一层；
        全部层都不表态时才落到**显式**默认值。

        ⚠️ 一层的限制是它**所有** ``sources`` 的并集：
        例如第三层「安全与权限」= ``safety_denied ∪ platform_denied``，
        任何一侧限制了 ``key``，该层就表态。少读一个来源 = 该层被削弱，
        用户当前指令就能穿透它。
        """
        for layer in self.PRIORITY:
            if layer.is_user_command:
                command = self._latest_command(facts.user_commands, key)
                if command is not None:
                    return _Verdict(
                        _COMMAND_STANCE[command][1],
                        f"{layer.label}: 用户当前指令 {command.name}（L45）",
                    )
                continue
            for source in layer.sources:
                if key in getattr(facts, source):
                    # 更高层对同一键的限制优先于用户当前指令（L11）
                    return _Verdict(
                        False, f"{layer.label}: {key} 被该层（{source}）限制（L11）"
                    )
        return _Verdict(_KEY_DEFAULT[key], _KEY_DEFAULT_REASON[key])

    @staticmethod
    def _latest_command(
        commands: tuple[Intervention, ...], key: str
    ) -> Intervention | None:
        """同一层内多条冲突指令时**后者优先**（视为用户最新指令）。"""
        latest: Intervention | None = None
        for command in commands:
            stance = _COMMAND_STANCE.get(command)
            if stance is not None and stance[0] == key:
                latest = command
        return latest

    # -- 内部：授权事实聚合 ----------------------------------------------

    @staticmethod
    def _authorize(facts: PolicyFacts) -> AuthorizationContext:
        """所有授权事实的**唯一聚合点**（Judgment 之前完成）。

        ``contracts.AuthorizationContext`` 只有 ``system / platform / project``
        三档边界，没有 developer / safety 字段。5B.1 **不改 contracts**，
        因此把更高层（系统 / 开发者 / 安全）统一并入 ``system_denied``：
        它们对契约而言都是"比项目更高的边界"，只收窄、不放宽。
        """
        higher = _sorted_union(
            facts.system_denied, facts.developer_denied, facts.safety_denied
        )
        effective_denied = _sorted_union(
            higher,
            facts.platform_denied,
            facts.project_denied,
            facts.user_denied,
        )
        return AuthorizationContext(
            session_authorized=facts.session_authorized,
            user_granted_scopes=tuple(facts.user_granted_scopes),
            system_denied=higher,
            project_denied=tuple(facts.project_denied),
            platform_denied=tuple(facts.platform_denied),
            effective_denied=effective_denied,
            user_read_only_override=facts.user_read_only_override,
            rationale=(
                f"session_authorized={facts.session_authorized}; "
                f"higher-layer denials={len(higher)}; "
                f"project denials={len(facts.project_denied)}; "
                f"user denials={len(facts.user_denied)}; "
                f"read_only={facts.user_read_only_override}"
            ),
        )
