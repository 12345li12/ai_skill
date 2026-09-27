"""Approval Judgment —— DECISION：**是否需要审批？**

架构依据：`docs/architecture.md` 2 节 ``Approval Judgment``
（含 **Amendment A6** approval-policy seam / **Amendment A7** ordinary 谓词，
两者均 FROZEN；**Amendment A8** disclosure provenance）。

```text
输入：RiskAssessment、CostEstimate、AuthorizationContext、RoutingPlan 的副作用
      + EffectiveApprovalConstraints（A6 seam）
      + ApprovalDisclosure（A8 seam：拟执行动作 / 影响范围 / 恢复方式）
输出：ApprovalJudgment（needs_approval + 四项清单）
```

⚠️ `AuthorizationContext` **是**冻结输入面的一员（必须在签名里、必须类型校验），
但按 A7.5 **不参与** ordinary 的析取式 —— 它只作授权收窄／解释。

**本节点是"是否需要审批"的唯一决定者。** 它只组合已经存在的子判据：

```text
needs_approval = constraints.require_approval_for_all      # A6.4 策略强制
                 OR ordinary_needs_approval                # A7.2

ordinary_needs_approval =
        risk_hit            # L41 类别证据 ∪ Step 5A 的 HIGH helper
    OR  cost_hit            # L43「显著额外费用」（magnitude == HIGH）
    OR  side_effect_hit     # RoutingPlan 可证明的副作用
```

⚠️ **四条边界：**

1. **不复制风险规则。** ``risk_hit`` 直接复用 ``risk.py`` 已冻结的
   ``requires_human_input``（A7.2.1）—— "HIGH 意味着什么"由 Step 5A 拥有。
2. **授权维度不产生肯定项。** A7.5：``AuthorizationContext`` **是**本节冻结输入面
   的一员（因此 ``judge`` **必须**接收它），但**不参与**
   ``ordinary_needs_approval`` 的析取式；它只作**授权收窄／解释**，
   单独**不能**把 ``False`` 变成 ``True``；"已授权 + 只读"**不是**全局免审批。
3. **不得伪造风险。** 四项清单在任何情况下都必须非空（契约强制），
   但 ``main_risk`` 必须如实：普通判据命中就写**真实**理由；
   只有策略强制时写明"未识别到独立普通审批判据…"；两者都不命中时如实说未命中。
4. **不是 Gate。** Gate 只做"暂停 → 交还控制权 → 等待人类输入"；
   本节点**只产生 `needs_approval` 与清单**，不暂停、不向人类索取输入。
"""

from __future__ import annotations

from .approval_constraints import EffectiveApprovalConstraints
from .approval_disclosure import ApprovalDisclosure as _ApprovalDisclosure
from .contracts import (
    ApprovalJudgment,
    AuthorizationContext,
    CostEstimate,
    CostMagnitude,
    ExecutionMode,
    RiskAssessment,
    RoutingPlan,
)
from .risk import requires_human_input

__all__ = ["ApprovalDisclosure", "ApprovalJudge"]


#: 策略强制、且**没有**独立普通判据时的固定说明（A6.5）。
#: ⚠️ 不采用"非风险判据命中"那种有歧义的中文 —— ordinary 不只含 Risk。
POLICY_ONLY_EXPLANATION = (
    "未识别到独立普通审批判据；本次审批由策略要求全部先审批触发（L45）。"
)

#: 三个维度**全不命中**时的说明 —— 如实说"没命中"，不编造风险。
NO_CRITERION_EXPLANATION = (
    "未识别到需要审批的判据（风险类别 / 显著成本 / RoutingPlan 副作用均未命中，"
    "且未强制全部审批）。"
)

# ``ApprovalDisclosure`` 是 A8 的 disclosure 通路承载，
# 定义在 ``approval_disclosure.py``（含三条 provenance 规则），此处 re-export ——
# 既保持既有的 ``adaptive_router.approval_judgment.ApprovalDisclosure`` 可用，
# 又让 provenance 规则住在一个**不反向依赖本模块**的独立单元里。
ApprovalDisclosure = _ApprovalDisclosure


class ApprovalJudge:
    """把已存在的子判据组合成 ``needs_approval`` + 四项清单。

    确定性、无副作用：同样输入永远得到同样结果。
    """

    def judge(
        self,
        risk_assessment: RiskAssessment,
        cost_estimate: CostEstimate,
        authorization: AuthorizationContext,
        routing: RoutingPlan,
        *,
        constraints: EffectiveApprovalConstraints,
        disclosure: ApprovalDisclosure,
    ) -> ApprovalJudgment:
        """产出 ``ApprovalJudgment``。

        参数顺序与本节的冻结**输入行**一致：
        ``RiskAssessment`` / ``CostEstimate`` / ``AuthorizationContext`` /
        ``RoutingPlan`` 的副作用，另加 A6 seam。

        ``constraints``（A6 seam）是**必需输入**：无默认值、不接受 ``None``。
        "没收到就当作不强制审批"是 fail-open，"没收到就当作强制审批"会掩盖
        Router 漏接 ``[02]`` 的 wiring bug —— 两者都被 A6.4 排除，
        唯一正确语义是**显式失败**。

        ⚠️ ``authorization`` **被接收但不参与判断**（A7.5）：
        它是本节冻结输入面的一员，因此必须在签名里、也必须做类型校验；
        但三个 ordinary 子判据**不得**读它，它**单独不能**把
        ``needs_approval`` 从 ``False`` 变成 ``True``。
        其职责仅为**授权收窄／解释**（由下游 Gate / 上报侧使用）。
        """
        self._validate(
            risk_assessment, cost_estimate, authorization, routing, constraints, disclosure
        )

        ordinary = self.ordinary_needs_approval(risk_assessment, cost_estimate, routing)
        needs_approval = constraints.require_approval_for_all or ordinary

        return ApprovalJudgment(
            needs_approval=needs_approval,
            proposed_action=disclosure.proposed_action,
            impact_scope=disclosure.impact_scope,
            main_risk=self._main_risk(
                risk_assessment,
                cost_estimate,
                routing,
                constraints=constraints,
                ordinary=ordinary,
            ),
            recovery_method=disclosure.recovery_method,
        )

    # ------------------------------------------------------------------
    # 三个子判据（A7）—— 公开，便于逐条审计与测试
    # ------------------------------------------------------------------

    @staticmethod
    def risk_hit(risk_assessment: RiskAssessment) -> bool:
        """L41 类别证据 ∪ Step 5A 已冻结的 HIGH helper（A7.2.1）。

        ⚠️ **不复制** ``level == HIGH`` 的规则 —— 它由 ``requires_human_input`` 拥有。
        """
        if not isinstance(risk_assessment, RiskAssessment):
            raise TypeError(
                f"risk_assessment 必须是 RiskAssessment，收到 {risk_assessment!r}"
            )
        return bool(risk_assessment.categories) or requires_human_input(risk_assessment)

    @staticmethod
    def cost_hit(cost_estimate: CostEstimate) -> bool:
        """L43「显著额外费用」：**只有** ``magnitude == HIGH``（A7.3）。

        ``MEDIUM`` 不命中；**不得**叠加 ``confidence`` ——
        ``CostEstimator._derive`` 目前恒返回 ``confidence=LOW``，
        叠加 confidence 会让该规则永不触发，等于静默禁用 L43 的第 11 类。
        """
        if not isinstance(cost_estimate, CostEstimate):
            raise TypeError(f"cost_estimate 必须是 CostEstimate，收到 {cost_estimate!r}")
        return cost_estimate.magnitude is CostMagnitude.HIGH

    @staticmethod
    def side_effect_hit(routing: RoutingPlan) -> bool:
        """``RoutingPlan`` 可证明的副作用（A7.4）。

        V1 只读 ``MULTI_AGENT`` 下 ``child.read_only is False`` 这一事实。

        ⚠️ **`0 child` / `DIRECT` / `STAGED_SINGLE_AGENT` 返回 ``False``，
        但那只表示"本维度没有额外证据"，绝不表示"无副作用"** ——
        真实操作风险由 ``RiskAssessment`` 负责（见 ``main_risk`` 的措辞）。

        ⚠️ **不得**用授权事实（``is_read_only`` / ``effective_denied``）
        替代副作用事实。
        """
        if not isinstance(routing, RoutingPlan):
            raise TypeError(f"routing 必须是 RoutingPlan，收到 {routing!r}")
        plan = routing.plan
        if plan.mode is not ExecutionMode.MULTI_AGENT:
            return False
        return any(spec.read_only is False for spec in plan.branches)

    @classmethod
    def ordinary_needs_approval(
        cls,
        risk_assessment: RiskAssessment,
        cost_estimate: CostEstimate,
        routing: RoutingPlan,
    ) -> bool:
        """A7.2：``risk_hit OR cost_hit OR side_effect_hit``。

        ⚠️ **授权维度不在此列**（A7.5）—— 它只作收窄／解释。
        """
        return (
            cls.risk_hit(risk_assessment)
            or cls.cost_hit(cost_estimate)
            or cls.side_effect_hit(routing)
        )

    @classmethod
    def policy_only_approval(
        cls,
        risk_assessment: RiskAssessment,
        cost_estimate: CostEstimate,
        routing: RoutingPlan,
        *,
        constraints: EffectiveApprovalConstraints,
    ) -> bool:
        """A6.5：**策略强制且没有独立普通判据**。

        ⚠️ 判据是 ``require_approval_for_all AND NOT ordinary_needs_approval``，
        **不是**"Risk 未命中"：ordinary 还含 Cost / ``RoutingPlan`` 副作用。
        反例：``Risk LOW`` + 显著 Cost ⇒ 存在**真实**的预算风险理由。
        """
        if not isinstance(constraints, EffectiveApprovalConstraints):
            raise TypeError(
                "constraints 必须是 EffectiveApprovalConstraints"
                f"（[02] Policy 的已解析约束），收到 {constraints!r}"
                " —— 缺参时**不得**静默取任何默认（A6.4）"
            )
        return constraints.require_approval_for_all and not cls.ordinary_needs_approval(
            risk_assessment, cost_estimate, routing
        )

    # ------------------------------------------------------------------

    @classmethod
    def _main_risk(
        cls,
        risk_assessment: RiskAssessment,
        cost_estimate: CostEstimate,
        routing: RoutingPlan,
        *,
        constraints: EffectiveApprovalConstraints,
        ordinary: bool,
    ) -> str:
        """``main_risk`` 的内容规则（A6.5 两种情形 + 全不命中）。

        | 情形 | 要求 |
        |---|---|
        | ordinary 命中 | 写**真实**理由；**不得**为展示 policy rationale 抹掉真实风险 |
        | policy-only | **不得伪造风险**；写明"未识别到独立普通审批判据…" |
        | 全不命中 | 如实说未命中（契约要求非空，但不能编造） |
        """
        if ordinary:
            return cls._ordinary_reasons(risk_assessment, cost_estimate, routing)
        if cls.policy_only_approval(
            risk_assessment, cost_estimate, routing, constraints=constraints
        ):
            return POLICY_ONLY_EXPLANATION
        return NO_CRITERION_EXPLANATION

    @classmethod
    def _ordinary_reasons(
        cls,
        risk_assessment: RiskAssessment,
        cost_estimate: CostEstimate,
        routing: RoutingPlan,
    ) -> str:
        """把**真实**命中的维度写成人类可读的理由（不掺 policy rationale）。"""
        parts: list[str] = []
        if cls.risk_hit(risk_assessment):
            if risk_assessment.categories:
                categories = ",".join(
                    category.value for category in risk_assessment.categories
                )
                parts.append(f"风险类别命中（{categories}）")
            else:
                # categories 为空但 requires_human_input 为真 ⇒ 高等级信号
                parts.append(f"风险等级 {risk_assessment.level.value}")
        if cls.cost_hit(cost_estimate):
            parts.append(
                f"显著额外费用（magnitude={cost_estimate.magnitude.value}）"
            )
        if cls.side_effect_hit(routing):
            writable = ",".join(
                spec.branch_id
                for spec in routing.plan.branches
                if spec.read_only is False
            )
            parts.append(f"存在可写分支（{writable}）")
        return "；".join(parts)

    # ------------------------------------------------------------------

    @staticmethod
    def _validate(
        risk_assessment: RiskAssessment,
        cost_estimate: CostEstimate,
        authorization: AuthorizationContext,
        routing: RoutingPlan,
        constraints: EffectiveApprovalConstraints,
        disclosure: ApprovalDisclosure,
    ) -> None:
        if not isinstance(risk_assessment, RiskAssessment):
            raise TypeError(
                f"risk_assessment 必须是 RiskAssessment，收到 {risk_assessment!r}"
            )
        if not isinstance(cost_estimate, CostEstimate):
            raise TypeError(f"cost_estimate 必须是 CostEstimate，收到 {cost_estimate!r}")
        if not isinstance(authorization, AuthorizationContext):
            raise TypeError(
                f"authorization 必须是 AuthorizationContext，收到 {authorization!r}"
            )
        if not isinstance(routing, RoutingPlan):
            raise TypeError(f"routing 必须是 RoutingPlan，收到 {routing!r}")
        if not isinstance(constraints, EffectiveApprovalConstraints):
            raise TypeError(
                "constraints 必须是 EffectiveApprovalConstraints"
                f"（[02] Policy 的已解析约束），收到 {constraints!r}"
                " —— 缺参时**不得**静默取任何默认（A6.4）"
            )
        if not isinstance(disclosure, ApprovalDisclosure):
            raise TypeError(f"disclosure 必须是 ApprovalDisclosure，收到 {disclosure!r}")
