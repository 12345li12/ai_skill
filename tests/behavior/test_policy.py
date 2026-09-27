"""Policy / AuthorizationContext Builder 行为测试（Step 5B.1）。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：

  TEST-POLICY-001  7 层优先级序                → **5B.1 COMPLETE**
  TEST-POLICY-002  9 类用户干预命令             → **5B.1 COMPLETE**（识别 / 路由）
  TEST-POLICY-003  干预立即应用于未开始工作      → **PARTIAL**
                   [02] 只证明**路由意图**；运行中真正追加/停止/重建需 [10]
  TEST-POLICY-004  用户当前指令优先于项目配置    → **5B.1 COMPLETE**
                   但不能突破更高层限制

  仍 PARTIAL（不属本批）：
  TEST-PLAN-005    等 5B.2 的 Policy → Constraints → Planner → 0 branches

架构依据：
  2 节 [02] Policy —— 优先级序（L11）、模式、运行期干预（L45）
  2 节 Authorization Context Builder —— 授权事实的唯一聚合点（Judgment 之前完成）
  2 节 Approval Gate —— 只做"暂停 → 交还控制权 → 等待人类输入"，
                        **不做审批判断**，因此**不是** L45 干预的执行器
  Amendment A3 —— `EffectivePlanningConstraints`

计数口径（5B.1 订正）：
  `docs/architecture.md` 8 节原记「L45 = 8 类用户干预命令」，但 L45 原文按 `、`
  分组为 7 组、两组斜杠（开启/关闭协议、关闭/恢复自动分流）展开后为 **9 条**。
  A3.7 已把「仅用当前模型」与「调高/调低或指定模型」当作两件不同干预，
  故模型那一组不可合并 —— 唯一自洽读数为 9。已窄幅订正该处计数（Test ID 不变）。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.contracts import AuthorizationContext
from adaptive_router.policy import (
    EffectiveApprovalConstraints,
    EffectivePlanningConstraints,
    Intervention,
    InterventionRoute,
    Policy,
    PolicyFacts,
)

from _import_detectors import imported_module_tokens, imported_names

MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "src/adaptive_router/policy.py"
)
MODULE_SOURCE = MODULE_PATH.read_text(encoding="utf-8")
_MODULE_TREE = ast.parse(MODULE_SOURCE)

APPROVAL_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src/adaptive_router/approval_constraints.py"
)
APPROVAL_MODULE_SOURCE = APPROVAL_MODULE_PATH.read_text(encoding="utf-8")
_APPROVAL_TREE = ast.parse(APPROVAL_MODULE_SOURCE)


def _modules_of(tree: ast.AST) -> set[str]:
    """任意语法树上的 import token（`_APPROVAL_TREE` / `_MODULE_TREE` 共用）。"""
    return imported_module_tokens(tree)


def _imported_names() -> set[str]:
    return imported_names(_MODULE_TREE)


def _imported_modules() -> set[str]:
    """被测模块的 import token（保守检测器，见 `_import_detectors`）。"""
    return imported_module_tokens(_MODULE_TREE)


def _identifier_names() -> set[str]:
    """真实作为标识符使用的名字（不含 docstring / 字符串）。"""
    return {n.id for n in ast.walk(_MODULE_TREE) if isinstance(n, ast.Name)}


def _docstring_nodes() -> set[int]:
    """所有 docstring 的 ``Constant`` 节点 id（需从字面量断言中排除）。"""
    excluded: set[int] = set()
    for node in ast.walk(_MODULE_TREE):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                excluded.add(id(body[0].value))
    return excluded


def _code_string_literals() -> list[str]:
    """源码中的字符串字面量，**排除 docstring**。"""
    excluded = _docstring_nodes()
    return [
        node.value
        for node in ast.walk(_MODULE_TREE)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in excluded
    ]


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


def facts(**overrides: object) -> PolicyFacts:
    """默认：没有任何更高层限制、也没有用户干预的干净事实。"""
    base: dict[str, object] = dict(
        session_authorized=True,
        system_denied=(),
        developer_denied=(),
        safety_denied=(),
        platform_denied=(),
        user_denied=(),
        project_denied=(),
        user_granted_scopes=(),
        user_commands=(),
        user_read_only_override=False,
        project_model_preference=None,
        project_approval_preference=None,
        protocol_default_mode="observe",
    )
    base.update(overrides)
    return PolicyFacts(**base)  # type: ignore[arg-type]


def resolve(**overrides: object):
    return Policy().resolve(facts(**overrides))


# ==========================================================================
# TEST-POLICY-001 —— 7 层优先级序
# ==========================================================================


class TestPolicy001SevenLayerPriority:
    def test_seven_layers_are_defined_in_the_frozen_order(self) -> None:
        """L11 的优先级序（高 → 低）。"""
        assert [layer.name for layer in Policy.PRIORITY] == [
            "SYSTEM",
            "DEVELOPER",
            "SAFETY_PERMISSIONS",
            "USER_CURRENT",
            "USER_OLDER_VALID",
            "PROJECT_CONFIG",
            "THIS_PROTOCOL",
        ]

    def test_exactly_seven_layers(self) -> None:
        assert len(Policy.PRIORITY) == 7

    def test_protocol_layer_is_the_weakest(self) -> None:
        assert Policy.PRIORITY[-1].name == "THIS_PROTOCOL"
        assert Policy.PRIORITY[0].name == "SYSTEM"

    def test_clean_facts_allow_split_explicitly(self) -> None:
        """**显式 allow**，不是 fail-open（A3）：Policy 明确产出 True + rationale。"""
        result = resolve()
        assert result.planning.auto_split_allowed is True
        assert result.planning.rationale.strip()

    def test_higher_layer_restriction_blocks_auto_split(self) -> None:
        for layer in ("system_denied", "developer_denied", "safety_denied"):
            result = resolve(**{layer: ("auto_split",)})
            assert result.planning.auto_split_allowed is False, layer

    #: 能携带"限制集"的层，按优先级高 → 低；每层可有**多个**来源。
    #: 第 4 层（用户当前指令）是"表态"而非限制集，单独由下面几条测试覆盖；
    #: 第 7 层（本协议）不提供限制，只提供模式默认值。
    #:
    #: ⚠️ 第三层是「**安全与权限**」= `safety_denied` **+** `platform_denied`。
    #: 平台权限限制**不是**独立的一层（否则会变成 8 层，违反冻结架构）。
    DENY_LAYERS = (
        (("system_denied",), "system"),
        (("developer_denied",), "developer"),
        (("safety_denied", "platform_denied"), "safety"),
        (("user_denied",), "user_older"),
        (("project_denied",), "project"),
    )

    def test_safety_permissions_layer_covers_safety_and_platform(self) -> None:
        """第三层是「安全与权限」，**不是**"只有 Safety"，也不是新的一层。"""
        layer = Policy.PRIORITY[2]
        assert layer.name == "SAFETY_PERMISSIONS"
        assert layer.sources == ("safety_denied", "platform_denied")
        # 仍然恰好 7 层：平台限制**不得**作为第 8 层插进来
        assert len(Policy.PRIORITY) == 7
        assert not any("PLATFORM" in item.name for item in Policy.PRIORITY)

    def test_every_senior_deny_layer_beats_every_junior_deny_layer(self) -> None:
        """**真正的层序证明**：每对层由更 senior 的那层赢，且**逐来源**验证。

        只填一层（如 `test_higher_layer_restriction_blocks_auto_split`）证明不了顺序 ——
        那种用例即使把 PRIORITY 整个重排也照样通过。这里每对都同时填两层，
        并且把 senior / junior 两侧的**每一个**来源都组合一遍：

        - 第三层少读 `platform_denied` 会被抓到（5B.1.2 的缺口正是这样漏掉的）；
        - `platform` 若被错放进第 1/2 层也会被抓到。
        """
        for index, (senior_sources, senior_label) in enumerate(self.DENY_LAYERS):
            for junior_sources, junior_label in self.DENY_LAYERS[index + 1 :]:
                for senior_source in senior_sources:
                    for junior_source in junior_sources:
                        result = resolve(
                            **{
                                senior_source: ("auto_split",),
                                junior_source: ("auto_split",),
                            }
                        )
                        case = (senior_source, junior_source)
                        assert result.planning.auto_split_allowed is False, case
                        assert senior_label in result.planning.rationale, case
                        assert junior_label not in result.planning.rationale, case

    def test_deny_layer_table_matches_the_declared_priority(self) -> None:
        """上表的层序与来源必须与 `Policy.PRIORITY` **逐条**一致。"""
        declared = [
            layer for layer in Policy.PRIORITY if layer.sources and not layer.is_user_command
        ]
        assert [layer.sources for layer in declared] == [
            sources for sources, _ in self.DENY_LAYERS
        ]
        assert [layer.label for layer in declared] == [
            label for _, label in self.DENY_LAYERS
        ]

    def test_the_pair_test_is_sensitive_to_layer_order(self, monkeypatch) -> None:
        """**空地对治**：把 PRIORITY 的相邻两层互换，上面那条配对战**必须失败**。

        5B.1 的变异探针实测发现：只填单层的用例（如
        ``test_higher_layer_restriction_blocks_auto_split``）在 PRIORITY 被整体
        重排后**照样通过** —— 单层写法证明不了顺序。这里把"能抓到重排"
        本身变成断言，防止将来有人把配对战测试改回单层写法而没人发现。
        """
        swapped = (
            Policy.PRIORITY[0],
            Policy.PRIORITY[2],
            Policy.PRIORITY[1],
            *Policy.PRIORITY[3:],
        )
        monkeypatch.setattr(Policy, "PRIORITY", swapped)
        with pytest.raises(AssertionError):
            self.test_every_senior_deny_layer_beats_every_junior_deny_layer()

    def test_restriction_rationale_names_the_winning_layer(self) -> None:
        result = resolve(system_denied=("auto_split",))
        assert "system" in result.planning.rationale

    def test_the_most_senior_layer_wins_the_rationale(self) -> None:
        """多层同时禁止时，rationale 指向**最高**层。"""
        result = resolve(
            system_denied=("auto_split",),
            safety_denied=("auto_split",),
        )
        assert "system" in result.planning.rationale
        assert "safety" not in result.planning.rationale

    def test_project_config_cannot_override_a_higher_layer(self) -> None:
        """项目配置是第 6 层 —— 不得放宽第 1–3 层的限制。"""
        result = resolve(
            system_denied=("auto_split",),
            # 项目层"什么也没禁"，但这不能消掉 system 的禁止
            project_denied=(),
        )
        assert result.planning.auto_split_allowed is False

    def test_protocol_default_never_overrides_any_higher_layer(self) -> None:
        result = resolve(
            developer_denied=("auto_split",),
            protocol_default_mode="observe",
        )
        assert result.planning.auto_split_allowed is False


# ==========================================================================
# TEST-POLICY-004 —— 用户当前指令优先于项目配置，但不突破更高层
# ==========================================================================


class TestPolicy004UserOverProjectNotOverHigher:
    def test_current_user_instruction_beats_project_default(self) -> None:
        """用户当前指令 > 项目配置（L11）。"""
        result = resolve(
            project_model_preference="strong",
            user_commands=(Intervention.USE_CURRENT_MODEL,),
        )
        assert result.effective.use_current_model is True
        # 用户压制了项目偏好
        assert result.effective.model_preference is None

    def test_user_cannot_break_system_layer(self) -> None:
        """用户指令**不得突破**更高层（L11 末句）。"""
        result = resolve(
            system_denied=("auto_split",),
            user_commands=(Intervention.RESUME_AUTO_SPLIT,),
        )
        assert result.effective.auto_split_enabled is False
        assert result.planning.auto_split_allowed is False

    def test_user_cannot_break_safety_layer(self) -> None:
        result = resolve(
            safety_denied=("auto_split",),
            user_commands=(Intervention.RESUME_AUTO_SPLIT,),
        )
        assert result.planning.auto_split_allowed is False

    def test_platform_restriction_outranks_the_current_user_instruction(self) -> None:
        """**TEST-POLICY-004 的权限侧**：第三层是「安全与**权限**」。

        `platform_denied` 与 `safety_denied` 同属第三层，因此平台权限限制
        必须能挡住用户当前指令的 `RESUME_AUTO_SPLIT`。

        ⚠️ 这是 5B.1.2 修掉的真实缺口：第三层原先只读 `safety_denied`，
        `platform_denied` 只在构造 `AuthorizationContext` 时才被用到，
        于是"平台说不得自动分流"可以被用户当前指令穿透 ——
        直接破坏 A3 的"更高规则限制 → Planner 不能 MULTI_AGENT"。
        """
        result = resolve(
            platform_denied=("auto_split",),
            user_commands=(Intervention.RESUME_AUTO_SPLIT,),
        )
        assert result.planning.auto_split_allowed is False
        assert result.effective.auto_split_enabled is False

    def test_platform_restriction_outranks_older_and_project_layers(self) -> None:
        """平台限制同样高于第 5、6 层（它属于第 3 层）。"""
        older = resolve(
            platform_denied=("auto_split",),
            user_denied=(),
        )
        assert older.planning.auto_split_allowed is False
        contested = resolve(
            platform_denied=("auto_split",),
            project_denied=(),
        )
        assert contested.planning.auto_split_allowed is False
        assert "safety" in contested.planning.rationale

    def test_higher_layer_vetoes_the_user_approval_override(self) -> None:
        """更高层对同一策略键的限制优先于用户当前指令（L11）。"""
        baseline = resolve(user_commands=(Intervention.APPROVE_ALL,))
        vetoed = resolve(
            safety_denied=("approval_mode",),
            user_commands=(Intervention.APPROVE_ALL,),
        )
        assert baseline.effective.require_approval_for_all is True
        assert vetoed.effective.require_approval_for_all is False

    def test_user_can_tighten_beyond_higher_layers(self) -> None:
        """用户**收窄**永远是允许的：没有任何更高层要求，用户仍可要求全部先审批。"""
        result = resolve(user_commands=(Intervention.APPROVE_ALL,))
        assert result.effective.require_approval_for_all is True
        # 收窄一个键不得牵连另一个键
        assert result.planning.auto_split_allowed is True

    def test_project_preference_beats_the_protocol_default(self) -> None:
        """第 6 层 > 第 7 层：项目审批偏好覆盖协议默认模式。"""
        assert resolve().effective.mode == "observe"
        assert (
            resolve(project_approval_preference="approval").effective.mode == "approval"
        )

    def test_older_user_instruction_is_weaker_than_current(self) -> None:
        """第 4 层 > 第 5 层。"""
        older = resolve(user_denied=("auto_split",))  # 旧指令：曾禁止分流
        current = resolve(
            user_denied=("auto_split",),
            user_commands=(Intervention.RESUME_AUTO_SPLIT,),
        )
        assert older.planning.auto_split_allowed is False
        assert current.planning.auto_split_allowed is True


# ==========================================================================
# TEST-POLICY-002 —— 9 类用户干预命令
# ==========================================================================


class TestPolicy002NineInterventions:
    #: 5B.1 冻结的路由表：干预 → 必须据此行动的承载节点（**只声明意图**）。
    #:
    #: ⚠️ **Approval Gate 永远不是 L45 Policy 命令的执行器。**
    #: 架构 2 节 Approval Gate："Gate 只做三件事：暂停 → 交还控制权 → 等待人类输入。
    #: 它不聚合权限、**不做审批判断**。" 因此审批相关干预路由到
    #: `approval_judgment`（系统决策）或 `policy`（模式本是 `[02]` 自己的输出），
    #: 都**不**路由到 `approval_gate`。
    ROUTE_TARGETS = {
        Intervention.ENABLE_PROTOCOL: "session",
        Intervention.DISABLE_PROTOCOL: "session",
        Intervention.USE_CURRENT_MODEL: "model_router",
        Intervention.SET_MODEL_TIER: "model_router",
        Intervention.DISABLE_AUTO_SPLIT: "planner",
        Intervention.RESUME_AUTO_SPLIT: "planner",
        Intervention.APPROVE_ALL: "approval_judgment",
        Intervention.SWITCH_MODE: "policy",
        Intervention.STOP_BRANCHES: "agent_manager",
    }

    def test_nine_interventions_are_defined(self) -> None:
        """L45 的 9 类干预命令（原记「8 类」为计数笔误，5B.1 订正）。"""
        assert len(Intervention) == 9
        assert {i.name for i in Intervention} == {
            "ENABLE_PROTOCOL",
            "DISABLE_PROTOCOL",
            "USE_CURRENT_MODEL",
            "DISABLE_AUTO_SPLIT",
            "RESUME_AUTO_SPLIT",
            "APPROVE_ALL",
            "STOP_BRANCHES",
            "SET_MODEL_TIER",
            "SWITCH_MODE",
        }

    def test_every_intervention_has_exactly_one_route_target(self) -> None:
        """路由表必须与枚举一样大 —— 不允许有"没想清楚去哪"的干预。"""
        assert set(self.ROUTE_TARGETS) == set(Intervention)

    def test_each_intervention_is_recognized(self) -> None:
        for intervention in Intervention:
            result = resolve(user_commands=(intervention,))
            assert intervention in result.recognized_interventions, intervention

    def test_each_intervention_is_routed_somewhere(self) -> None:
        for intervention in Intervention:
            result = resolve(user_commands=(intervention,))
            route = result.route_for(intervention)
            assert isinstance(route, InterventionRoute), intervention
            assert route.target, intervention
            assert route.reason.strip(), intervention

    def test_route_targets_match_the_frozen_table(self) -> None:
        for intervention, target in self.ROUTE_TARGETS.items():
            result = resolve(user_commands=(intervention,))
            assert result.route_for(intervention).target == target, intervention

    def test_no_l45_intervention_routes_to_the_human_gate(self) -> None:
        """**Approval Gate 永远不是 L45 Policy 命令的"执行器"。**

        架构 2 节 Approval Gate：*"Gate 只做三件事：暂停 → 交还控制权 → 等待人类输入。
        它不聚合权限、**不做审批判断**"*。把系统决策职责塞回人类 Gate
        会让 Gate 同时"决定是否需要审批"和"向人类要审批"，边界立刻失效。

        这条测试是**回归闸门**：以后谁把审批/模式职责又挂回 Gate 就会红。
        """
        for intervention in Intervention:
            result = resolve(user_commands=(intervention,))
            target = result.route_for(intervention).target
            assert target != "approval_gate", intervention

    def test_approval_interventions_route_to_judgment_and_policy(self) -> None:
        """两条审批相关干预的**正确**归属。"""
        approve = resolve(user_commands=(Intervention.APPROVE_ALL,))
        # 「全部先审批」是系统决策：由 Judgment 决定是否需要暂停
        assert approve.route_for(Intervention.APPROVE_ALL).target == (
            "approval_judgment"
        )
        assert approve.effective.require_approval_for_all is True
        switch = resolve(user_commands=(Intervention.SWITCH_MODE,))
        # 模式是 [02] 自己的输出，不是 Gate 的职责
        assert switch.route_for(Intervention.SWITCH_MODE).target == "policy"

    def test_stop_branches_is_only_routed_not_executed(self) -> None:
        """**TEST-POLICY-003 = PARTIAL**：[02] 只证明路由意图。

        运行中真正追加 / 停止 / 重建需要 [10] Agent Manager；
        本批**不得**越界去操作它。
        """
        result = resolve(user_commands=(Intervention.STOP_BRANCHES,))
        route = result.route_for(Intervention.STOP_BRANCHES)
        assert route.target == "agent_manager"
        assert route.executed is False
        # 只声明意图，不声称已执行
        assert result.effective.stop_branches_requested is True

    def test_no_intervention_is_executed_in_this_step(self) -> None:
        """5B.1 只有路由意图：9 条干预全部 `executed=False`（不得声称已生效）。"""
        result = resolve(user_commands=tuple(Intervention))
        assert len(result.routes) == 9
        for route in result.routes:
            assert route.executed is False, route.intervention

    def test_use_current_model_routes_to_model_router(self) -> None:
        result = resolve(user_commands=(Intervention.USE_CURRENT_MODEL,))
        assert result.route_for(Intervention.USE_CURRENT_MODEL).target == "model_router"
        assert result.effective.use_current_model is True

    def test_set_model_tier_requires_a_tier_argument(self) -> None:
        result = resolve(user_commands=(Intervention.SET_MODEL_TIER,))
        route = result.route_for(Intervention.SET_MODEL_TIER)
        assert route.target == "model_router"
        assert route.requires_argument is True

    def test_switch_mode_requires_an_argument(self) -> None:
        result = resolve(user_commands=(Intervention.SWITCH_MODE,))
        route = result.route_for(Intervention.SWITCH_MODE)
        assert route.requires_argument is True

    def test_switch_mode_does_not_invent_a_direction(self) -> None:
        """没有参数就不许猜切换方向 —— 模式保持协议默认（诚实失败，不静默猜测）。"""
        assert resolve().effective.mode == "observe"
        assert resolve(user_commands=(Intervention.SWITCH_MODE,)).effective.mode == (
            "observe"
        )

    def test_protocol_toggle_has_an_effective_flag(self) -> None:
        assert resolve().effective.protocol_enabled is True
        disabled = resolve(user_commands=(Intervention.DISABLE_PROTOCOL,))
        enabled = resolve(user_commands=(Intervention.ENABLE_PROTOCOL,))
        assert disabled.effective.protocol_enabled is False
        assert enabled.effective.protocol_enabled is True

    def test_multiple_interventions_are_all_routed(self) -> None:
        result = resolve(
            user_commands=(
                Intervention.DISABLE_AUTO_SPLIT,
                Intervention.STOP_BRANCHES,
                Intervention.SET_MODEL_TIER,
            )
        )
        assert len(result.recognized_interventions) == 3
        assert len(result.routes) == 3

    def test_disable_auto_split_turns_off_auto_split(self) -> None:
        result = resolve(user_commands=(Intervention.DISABLE_AUTO_SPLIT,))
        assert result.effective.auto_split_enabled is False
        assert result.planning.auto_split_allowed is False

    def test_approve_all_requires_approval_for_everything(self) -> None:
        """L45「全部先审批」= **更严**：所有动作都要先审批（不是免审批）。"""
        assert resolve().effective.require_approval_for_all is False
        result = resolve(user_commands=(Intervention.APPROVE_ALL,))
        assert result.effective.require_approval_for_all is True


# ==========================================================================
# AuthorizationContext Builder —— 唯一聚合点，且不改 contracts
# ==========================================================================


class TestAuthorizationContextBuilder:
    def test_produces_the_frozen_contract_type(self) -> None:
        """不另造第二套授权结果。"""
        result = resolve()
        assert isinstance(result.authorization, AuthorizationContext)

    def test_effective_denied_includes_every_higher_layer(self) -> None:
        result = resolve(
            system_denied=("deploy",),
            platform_denied=("write",),
            project_denied=("network",),
        )
        assert set(result.authorization.effective_denied) == {
            "deploy",
            "write",
            "network",
        }

    def test_project_denial_cannot_erase_a_system_denial(self) -> None:
        """契约层已强制"只能收窄"；本模块同样不得放宽。"""
        result = resolve(system_denied=("deploy",), project_denied=())
        assert "deploy" in result.authorization.effective_denied

    def test_user_can_tighten_effective_denied(self) -> None:
        result = resolve(user_denied=("extra",))
        assert "extra" in result.authorization.effective_denied

    def test_session_authorization_is_consumed_not_decided(self) -> None:
        """[02] **消费**上游已成立的授权事实，不重新决定 Session 状态。"""
        assert resolve(session_authorized=True).authorization.session_authorized is True
        assert (
            resolve(session_authorized=False).authorization.session_authorized is False
        )

    def test_read_only_override_is_forwarded_from_the_input_fact(self) -> None:
        """`user_read_only_override` 是**显式用户授权事实**；`[02]` 只转发，不发明。"""
        assert resolve().authorization.is_read_only is False
        assert resolve(user_read_only_override=True).authorization.is_read_only is True

    def test_no_l45_intervention_invents_read_only(self) -> None:
        """**9 条 L45 干预没有一条**会凭空打开只读。

        该事实的典型来源是 Approval Gate 的 ``MODIFY``
        （"可以，但只允许读、不允许写"），但**不是唯一来源** ——
        用户当前指令同样可以直接产生它（R4）。无论哪种来源，
        都必须在进入 `[02]` 之前**已经是事实**。
        """
        for intervention in Intervention:
            result = resolve(user_commands=(intervention,))
            assert result.authorization.is_read_only is False, intervention

    def test_rationale_is_present(self) -> None:
        assert resolve().authorization.rationale.strip()


# ==========================================================================
# A3 DTO —— 显式产出，不是 fail-open
# ==========================================================================


class TestEffectivePlanningConstraints:
    def test_is_an_implementation_level_dto(self) -> None:
        """A3：不属于 contracts/。"""
        from adaptive_router import contracts

        assert "EffectivePlanningConstraints" not in contracts.__all__
        assert not hasattr(contracts, "EffectivePlanningConstraints")

    def test_has_the_frozen_minimal_shape(self) -> None:
        field_names = {f.name for f in dataclasses.fields(EffectivePlanningConstraints)}
        assert field_names == {"auto_split_allowed", "rationale"}

    def test_carries_no_model_choice(self) -> None:
        """A3.7：`allowed_tiers` 之类的模型约束**不得**进本 DTO。"""
        field_names = {f.name for f in dataclasses.fields(EffectivePlanningConstraints)}
        for forbidden in ("allowed_tiers", "tier", "model", "model_preference"):
            assert forbidden not in field_names

    def test_carries_no_cost_or_approval(self) -> None:
        field_names = {f.name for f in dataclasses.fields(EffectivePlanningConstraints)}
        for forbidden in (
            "estimated_cost",
            "budget",
            "needs_approval",
            "approve_all",
            "require_approval_for_all",
        ):
            assert forbidden not in field_names

    def test_is_frozen(self) -> None:
        constraints = resolve().planning
        with pytest.raises(dataclasses.FrozenInstanceError):
            constraints.auto_split_allowed = False  # type: ignore[misc]

    def test_policy_always_produces_one_explicitly(self) -> None:
        """**禁止 fail-open**：任何路径都必须产出显式实例。"""
        for overrides in (
            {},
            {"system_denied": ("auto_split",)},
            {"user_commands": (Intervention.DISABLE_AUTO_SPLIT,)},
        ):
            result = resolve(**overrides)
            assert isinstance(result.planning, EffectivePlanningConstraints)
            assert result.planning.rationale.strip(), overrides

    def test_there_is_no_default_unrestricted_shortcut(self) -> None:
        """不得存在"不传就自动允许"的捷径构造。"""
        import inspect

        signature = inspect.signature(Policy.resolve)
        for name in signature.parameters:
            assert "constraints" not in name.lower()
        # 也不得有一个返回 unrestricted 的零参便捷构造被 Policy 内部当作默认
        public = {n for n in dir(Policy) if not n.startswith("_")}
        assert "unrestricted" not in public


# ==========================================================================
# 边界：不重实现 SessionGate、不读环境、不越界
# ==========================================================================


class TestPolicyBoundaries:
    def test_does_not_reimplement_session_gate(self) -> None:
        """[02] 位于已 gated 的路径里，不得出现第二套 Session 状态机。"""
        names = _imported_names()
        for forbidden in ("SessionGate", "SessionState", "SessionGateResult"):
            assert forbidden not in names, forbidden
        assert "session" not in _imported_modules()
        # 非 docstring 的真实标识符 / 字符串字面量里也不得出现 Session 词汇
        identifiers = _identifier_names()
        assert "UNASKED" not in identifiers
        literals = _code_string_literals()
        for forbidden in ("UNASKED", "bypass", "Bypass"):
            assert all(forbidden not in text for text in literals), forbidden

    def test_does_not_read_files_environment_or_tools(self) -> None:
        """PolicyFacts 只是输入规范化 —— Policy 自己是纯 resolver。"""
        modules = _imported_modules()
        for forbidden in (
            "pathlib",
            "os",
            "subprocess",
            "requests",
            "json",
            "socket",
        ):
            assert forbidden not in modules, forbidden
        names = _identifier_names()
        for forbidden in ("open", "environ", "getenv"):
            assert forbidden not in names, forbidden
        literals = _code_string_literals()
        for forbidden in ("os.environ", "open(", "rglob"):
            assert all(forbidden not in text for text in literals), forbidden

    def test_does_not_import_planner_or_model_router(self) -> None:
        """A3：本批不改 planner.py；[06] 的档位决策也不属本批。"""
        modules = _imported_modules()
        for forbidden in ("planner", "model_router", "risk", "cost"):
            assert forbidden not in modules, forbidden

    def test_does_not_import_agent_manager(self) -> None:
        """TEST-POLICY-003 只到路由意图 —— 不越界操作 [10]。"""
        assert "agent_mgr" not in _imported_modules()
        assert "agent_manager" not in _imported_modules()

    def test_does_not_touch_approval_gate(self) -> None:
        names = _imported_names()
        for forbidden in ("ApprovalJudgment", "ApprovalGate"):
            assert forbidden not in names, forbidden

    def test_policy_facts_reject_unknown_fields(self) -> None:
        with pytest.raises(TypeError):
            PolicyFacts(is_admin=True)  # type: ignore[call-arg]

    def test_policy_facts_are_frozen(self) -> None:
        f = facts()
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.session_authorized = False  # type: ignore[misc]

    def test_result_is_frozen(self) -> None:
        result = resolve()
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.planning = result.planning  # type: ignore[misc]

    def test_resolver_is_deterministic(self) -> None:
        assert resolve(system_denied=("x",)) == resolve(system_denied=("x",))


# ==========================================================================
# Amendment A6 —— [02] → Approval Judgment 的 approval-policy seam（producer 侧）
#
# ⚠️ 本批只覆盖 **producer 投影 + seam DTO**（A6.3）。
#    consumer 侧（ordinary judgment 的组合规则）属 5B.4 的 approval_judgment.py，
#    尚未实现 —— 见 README_step5b.md §13 记录的阻塞发现。
# ==========================================================================


class TestA6ApprovalSeamDto:
    """A6.3 冻结的 DTO 形状与校验。"""

    def test_is_the_same_object_across_neutral_module_and_policy(self) -> None:
        """`policy` 只是 re-export；两个节点拿到的是**同一个类对象**。"""
        from adaptive_router import approval_constraints as ac
        from adaptive_router import policy as policy_mod

        assert (
            policy_mod.EffectiveApprovalConstraints
            is ac.EffectiveApprovalConstraints
            is EffectiveApprovalConstraints
        )

    def test_is_an_implementation_level_dto(self) -> None:
        """A6.3 / A6.7：不属于 `contracts/`。"""
        from adaptive_router import contracts

        assert "EffectiveApprovalConstraints" not in contracts.__all__
        assert not hasattr(contracts, "EffectiveApprovalConstraints")

    def test_has_the_frozen_two_field_shape(self) -> None:
        field_names = {
            f.name for f in dataclasses.fields(EffectiveApprovalConstraints)
        }
        assert field_names == {"require_approval_for_all", "rationale"}

    def test_carries_no_authorization_fact(self) -> None:
        """边界①：不得把授权事实混进审批策略 seam。"""
        field_names = {
            f.name for f in dataclasses.fields(EffectiveApprovalConstraints)
        }
        for forbidden in (
            "session_authorized",
            "effective_denied",
            "user_granted_scopes",
            "is_read_only",
        ):
            assert forbidden not in field_names, forbidden

    def test_is_frozen(self) -> None:
        constraints = resolve().approval
        with pytest.raises(dataclasses.FrozenInstanceError):
            constraints.require_approval_for_all = False  # type: ignore[misc]

    def test_rejects_a_non_bool_flag(self) -> None:
        with pytest.raises(TypeError):
            EffectiveApprovalConstraints(
                require_approval_for_all="yes",  # type: ignore[arg-type]
                rationale="r",
            )

    def test_rejects_an_empty_or_blank_rationale(self) -> None:
        """A6.3：rationale 必须**非空**。"""
        for blank in ("", "   ", "\n"):
            with pytest.raises(ValueError):
                EffectiveApprovalConstraints(
                    require_approval_for_all=True, rationale=blank
                )

    def test_rejects_a_non_str_rationale(self) -> None:
        with pytest.raises(TypeError):
            EffectiveApprovalConstraints(
                require_approval_for_all=True,
                rationale=None,  # type: ignore[arg-type]
            )

    def test_has_no_defaults(self) -> None:
        import inspect

        parameters = inspect.signature(EffectiveApprovalConstraints).parameters
        assert parameters["require_approval_for_all"].default is inspect.Parameter.empty
        assert parameters["rationale"].default is inspect.Parameter.empty


class TestA6ApprovalProducerProjection:
    """A6.3：`PolicyResult.approval` 的 producer 投影。"""

    def test_result_exposes_the_approval_projection(self) -> None:
        assert isinstance(resolve().approval, EffectiveApprovalConstraints)

    def test_clean_facts_do_not_force_approval(self) -> None:
        assert resolve().approval.require_approval_for_all is False

    def test_approve_all_forces_approval(self) -> None:
        result = resolve(user_commands=(Intervention.APPROVE_ALL,))
        assert result.approval.require_approval_for_all is True

    def test_higher_layer_veto_turns_it_off(self) -> None:
        result = resolve(
            safety_denied=("approval_mode",),
            user_commands=(Intervention.APPROVE_ALL,),
        )
        assert result.approval.require_approval_for_all is False

    def test_rationale_is_never_blank(self) -> None:
        for overrides in (
            {},
            {"user_commands": (Intervention.APPROVE_ALL,)},
            {"safety_denied": ("approval_mode",)},
        ):
            assert resolve(**overrides).approval.rationale.strip(), overrides

    def test_rationale_retains_which_layer_decided(self) -> None:
        """**A6.3 的核心**：这条证据在 5B.1 曾被丢弃。

        原来 `resolve()` 只用 `approval.value`；现在必须保留那个 verdict 的 reason，
        否则"这个强制审批结论是怎么由 L11 优先级解析出来的"就无法审计。
        """
        by_user = resolve(user_commands=(Intervention.APPROVE_ALL,))
        assert "user" in by_user.approval.rationale
        assert "APPROVE_ALL" in by_user.approval.rationale

        by_layer = resolve(
            safety_denied=("approval_mode",),
            user_commands=(Intervention.APPROVE_ALL,),
        )
        assert "safety" in by_layer.approval.rationale

    def test_it_is_not_the_planning_rationale(self) -> None:
        """两条 seam 是**独立**投影，不得互相串用理由。"""
        result = resolve(system_denied=("auto_split", "approval_mode"))
        assert result.planning.rationale != result.approval.rationale
        assert "auto_split" in result.planning.rationale
        assert "approval_mode" in result.approval.rationale
        assert result.planning.auto_split_allowed is False
        assert result.approval.require_approval_for_all is False

    def test_the_user_command_path_names_the_command_not_the_key(self) -> None:
        """用户指令路径的 reason 记的是**命令**，deny-set 路径才记**键名**。

        锁住这个差异，避免将来有人"统一格式"时把审计信息改丢。
        """
        by_command = resolve(user_commands=(Intervention.APPROVE_ALL,))
        assert "APPROVE_ALL" in by_command.approval.rationale
        assert "approval_mode" not in by_command.approval.rationale

        by_layer = resolve(system_denied=("approval_mode",))
        assert "approval_mode" in by_layer.approval.rationale

    def test_projection_is_deterministic(self) -> None:
        one = resolve(user_commands=(Intervention.APPROVE_ALL,))
        two = resolve(user_commands=(Intervention.APPROVE_ALL,))
        assert one.approval == two.approval

    def test_the_false_branch_is_not_a_permissive_claim(self) -> None:
        """`False` **只**表示"不强制全部审批"，不是"不需要审批"（A6.4）。

        本测试锁的是**语义边界**：DTO 里没有、也不得有表示
        "免审批 / 跳过审批"的字段或值。
        """
        field_names = {
            f.name for f in dataclasses.fields(EffectiveApprovalConstraints)
        }
        for forbidden in ("skip_approval", "needs_approval", "approval_not_required"):
            assert forbidden not in field_names, forbidden
        assert resolve().approval.require_approval_for_all is False


class TestA6SeamModulePurity:
    """中性 seam 模块的依赖方向（与 `planning_constraints.py` 同构）。"""

    def test_does_not_import_policy(self) -> None:
        """否则 `policy → approval_constraints → policy` 会成环。"""
        assert "policy" not in _modules_of(_APPROVAL_TREE)

    def test_does_not_import_contracts(self) -> None:
        """A6.3：它是**实现级** DTO，不属于 `contracts/`，也不依赖它。"""
        assert "contracts" not in _modules_of(_APPROVAL_TREE)

    def test_does_not_import_any_runtime_node(self) -> None:
        modules = _modules_of(_APPROVAL_TREE)
        for forbidden in (
            "planner",
            "model_router",
            "risk",
            "cost",
            "session",
            "approval_judgment",
        ):
            assert forbidden not in modules, forbidden

    def test_policy_does_not_depend_on_the_judgment_consumer(self) -> None:
        """数据流是 `[02] → Approval Judgment`，**反向边不得存在**。

        ⚠️ 本测试原先是"consumer 尚不存在"的**越界守卫**（5B.4.a 期间，
        防止顺手把 Judgment 写了）。5B.4.b 正式实现消费者后，那条守卫已无意义，
        替换为这条**持久**不变量：producer 不得反过来 import consumer。
        """
        modules = _modules_of(_MODULE_TREE)
        assert "approval_judgment" not in modules
        assert "risk" not in modules
        names = _imported_names()
        assert "ApprovalJudge" not in names
        assert "requires_human_input" not in names
