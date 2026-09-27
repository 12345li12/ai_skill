"""model_router —— [06] Model Router：选择**抽象**模型档位。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 [06] Model Router
  - 4.2 Risk → Model → Cost → Approval 线性解环流

```text
[06] Model Router
   输入：Evaluation、RiskAssessment、ProjectContext 的模型偏好
   输出：ModelSelection（**计划档位 = abstract tier**）
```

⚠️ 四条边界：

   1. **只选抽象档位 / 强度。** 不得引入任何 provider model ID 或平台参数
      —— 平台模型由 [11] Adapter 翻译，Core 禁止出现平台细节。
   2. **风险是单向约束。** ``RiskAssessment`` 只能**抬升**档位，永不降级
      （架构 4.2：风险先于本节点产出，用于**约束**模型选择）。
   3. **项目偏好只是 preference。** 它可以抬升档位，**不能**压到复杂度下限以下，
      也不获得任何授权权力。
   4. **本模块不接线 [14] / [15] 的节点。** 升级触发（TEST-MODEL-002）与降级
      （TEST-MODEL-003）的**完整**语义分别需要 Recovery 与 Cost Summary。

      ⚠️ **实际接线状态（勿与上一版混淆）：**

           ``raise_tier()``  **已被 ``decide()`` 调用** ——
                            用于初始选择中的两类升级：
                            低置信度（``rework_probability == HIGH``）
                            与 ``RiskLevel.HIGH``
           ``lower_tier()``  **尚未被任何地方调用** —— 保留给 [15c]

      ⚠️ 因此本批**不主张**"``raise_tier`` 从不被隐式调用"；
         TEST-MODEL-002 的状态是 **PARTIAL**：
         [06] 侧的初始升级已实现，**执行期 / [14] Recovery 触发的升级未实现**。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import (
    BranchId,
    EffortLevel,
    Evaluation,
    ImpactScope,
    ModelSelection,
    ModelTier,
    Ordinal,
    Plan,
    ProjectContext,
    RiskAssessment,
    RiskLevel,
)

__all__ = [
    "PlatformDetailError",
    "TierDecision",
    "ModelRouter",
    "parse_tier_preference",
]

#: 档位由低到高的顺序，供单向升降与比较使用。
_TIER_ORDER: tuple[ModelTier, ...] = (
    ModelTier.LOW_COST,
    ModelTier.BALANCED,
    ModelTier.STRONG,
    ModelTier.MAXIMUM,
)

#: 各档位的**强度下限**。这是"刚好够用的最低配置"的落点。
_EFFORT_FLOOR: dict[ModelTier, EffortLevel] = {
    ModelTier.LOW_COST: EffortLevel.LOW,
    ModelTier.BALANCED: EffortLevel.MEDIUM,
    ModelTier.STRONG: EffortLevel.HIGH,
    ModelTier.MAXIMUM: EffortLevel.HIGH,
}

#: 项目偏好可接受的**抽象档位**字面量。任何 provider ID 都会被视为平台细节。
_PREFERENCE_ALIASES: dict[str, ModelTier] = {}
for _tier in _TIER_ORDER:
    _PREFERENCE_ALIASES[_tier.value] = _tier
    _PREFERENCE_ALIASES[_tier.name.lower()] = _tier


class PlatformDetailError(ValueError):
    """输入里出现了**平台细节**。

    架构要求 Core 只处理抽象档位；provider model ID 属 [11] Adapter 的职责，
    因此出现在偏好里会被明确拒绝，而不是静默忽略。
    """


def parse_tier_preference(value: str | None) -> ModelTier | None:
    """把项目声明的模型偏好解析成**抽象档位**。

    ``None`` / 空串 → ``None``（未声明）。
    任何非抽象档位的取值（例如 ``"gpt-5-codex"``）→ ``PlatformDetailError``。

    ⚠️ 这里刻意**不做模糊匹配** —— 猜错的代价是选错档位。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise PlatformDetailError(f"模型偏好必须是字符串，收到 {value!r}")
    candidate = value.strip().lower()
    if not candidate:
        return None
    try:
        return _PREFERENCE_ALIASES[candidate]
    except KeyError:
        raise PlatformDetailError(
            f"模型偏好必须是抽象档位之一 {[t.value for t in _TIER_ORDER]}，"
            f"收到 {value!r}（平台 model ID 属 [11] Adapter 的职责）"
        ) from None


@dataclass(frozen=True)
class TierDecision:
    """档位决策的**可审计**中间结果。

    把"为什么是这个档位"拆开记录，便于复盘与 [15c] 的成本对照。
    """

    base_tier: ModelTier
    final_tier: ModelTier
    effort: EffortLevel
    risk_raised: bool
    preference_applied: bool
    reason: str


class ModelRouter:
    """[06] Model Router —— 选择"刚好够用的最低配置"。

    确定性、无副作用、**不访问平台**、**不触发审批**、**不估算成本**。
    """

    #: 档位比较顺序（低 → 高）。
    ORDER: tuple[ModelTier, ...] = _TIER_ORDER

    # ------------------------------------------------------------------
    # 纯函数升降接口
    #   raise_tier —— 已被 decide() 用于初始升级；[14] Recovery 将来也会用
    #   lower_tier —— 尚未被调用，保留给 [15c] Cost Summary
    # ------------------------------------------------------------------

    @staticmethod
    def effort_floor(tier: ModelTier) -> EffortLevel:
        """某个档位对应的**强度下限**。"""
        if not isinstance(tier, ModelTier):
            raise TypeError(f"tier 必须是 ModelTier，收到 {tier!r}")
        return _EFFORT_FLOOR[tier]

    @staticmethod
    def raise_tier(tier: ModelTier, *, steps: int = 1) -> ModelTier:
        """升档（饱和于 ``MAXIMUM``）。

        ⚠️ **已被 ``decide()`` 调用** —— 用于初始选择中的两类升级：
           低置信度（``rework_probability == HIGH``）与 ``RiskLevel.HIGH``。
           将来 [14] Recovery 在执行期失败/冲突时也会调用它。
        """
        return _step(tier, steps)

    @staticmethod
    def lower_tier(tier: ModelTier, *, steps: int = 1) -> ModelTier:
        """降档（饱和于 ``LOW_COST``）。

        ⚠️ **尚未被任何地方调用** —— 保留给 [15c] Cost Summary，
           用于"工作转为机械明确后为**后续任务**降级"。
        """
        return _step(tier, -steps)

    # ------------------------------------------------------------------
    # 初始选择
    # ------------------------------------------------------------------

    def select(
        self,
        evaluation: Evaluation,
        risk_assessment: RiskAssessment,
        *,
        project: ProjectContext,
    ) -> ModelSelection:
        """按 L33 选择"刚好够用的最低配置"。"""
        decision = self.decide(evaluation, risk_assessment, project=project)
        return ModelSelection(
            tier=decision.final_tier,
            effort=decision.effort,
            reason=decision.reason,
        )

    def decide(
        self,
        evaluation: Evaluation,
        risk_assessment: RiskAssessment,
        *,
        project: ProjectContext,
    ) -> TierDecision:
        if not isinstance(evaluation, Evaluation):
            raise TypeError(f"evaluation 必须是 Evaluation，收到 {evaluation!r}")
        if not isinstance(risk_assessment, RiskAssessment):
            raise TypeError(
                f"risk_assessment 必须是 RiskAssessment，收到 {risk_assessment!r}"
            )
        if not isinstance(project, ProjectContext):
            raise TypeError(f"project 必须是 ProjectContext，收到 {project!r}")

        base = self._base_tier(evaluation)
        tier = base
        notes: list[str] = [f"base={base.value}"]

        # 证据弱 → 抬升一档（L33：低置信度是升级触发之一）
        if evaluation.rework_probability is Ordinal.HIGH:
            tier = self.raise_tier(tier)
            notes.append("weak-evidence+1")

        # 风险是**单向**约束：只升不降
        risk_raised = False
        if risk_assessment.level is RiskLevel.HIGH:
            raised = self.raise_tier(tier)
            risk_raised = raised is not tier
            tier = raised
            notes.append("high-risk+1")

        # 项目偏好：只作为 preference，**不能低于复杂度下限**
        preference = parse_tier_preference(project.model_preference)
        preference_applied = False
        if preference is not None and _index(preference) > _index(tier):
            tier = preference
            preference_applied = True
            notes.append(f"project-preference={preference.value}")

        effort = self.effort_floor(tier)
        notes.append(f"final={tier.value}")
        notes.append(f"effort={effort.value}")

        return TierDecision(
            base_tier=base,
            final_tier=tier,
            effort=effort,
            risk_raised=risk_raised,
            preference_applied=preference_applied,
            reason="; ".join(notes),
        )

    # ------------------------------------------------------------------
    # per-child 投影（Amendment A4）
    # ------------------------------------------------------------------

    def assign_branch_models(
        self,
        plan: Plan,
        *,
        selection: ModelSelection,
    ) -> tuple[tuple[BranchId, ModelSelection], ...]:
        """把**已形成**的任务级 ``ModelSelection`` **显式投影**给每个 child。

        架构依据：`docs/architecture.md` 2 节 ``[06]`` **Amendment A4**。

        ⚠️ **这是投影，不是决策。** 本方法：

        - **不调用** :meth:`select` / :meth:`decide` —— 不产生新的档位决策；
        - **不读取、不解释** child 的 ``goal`` / ``read_only`` / ``completion_criteria``
          —— 只读 ``branch_id`` 作为身份；
        - **不改** ``tier`` / ``effort``；
        - 只把**同一个**选择显式分配给每个 child，并逐条写明**继承理由**。

        V1 冻结（A4.4）：各 child 与任务级**同档位**。这不是装配器的默认猜测 ——
        是 ``[06]`` 的**决定**。推导链只有一条：

        ```text
        系统里没有 per-branch 的 Evaluation / RiskAssessment 证据
                ↓  不能合法差异化
        V1 保守继承 task-level ModelSelection
                ↓
        不欠配，也不发明规则
        ```

        可用的 per-branch 事实只有 ``BranchSpec`` 的
        ``branch_id`` / ``goal`` / ``read_only`` / ``completion_criteria``；
        ``Evaluation``（11 维）与 ``RiskAssessment`` 都是**任务级**。
        从 ``read_only`` 或 ``goal`` 文本反推档位属于**发明规则**（L33 无此规则）。

        > ⚠️ **``[15a]`` 不是本决定的证据。**
        > "``[15a]`` 每次调用只接收**一个** ``ModelSelection``" **不能**推出
        > "各分支必须同档位" —— 它本来就是每分支每 attempt 各调一次，
        > 完全支持 ``A→STRONG / B→BALANCED / C→LOW_COST``。
        > ``[15a]`` 最多只说明**这种 fallback 与它的接口兼容**（无需为差异化改造
        > 接口）。**兼容 ≠ 证据。**

        已知代价（A4.5）：任务级档位对某个机械型 child 是**保守偏强**的；
        但没有证据时偏强**不会欠配**，只有欠配才会触发 L33 的升级触发条件。

        ``DIRECT`` / ``STAGED_SINGLE_AGENT``（0 分支）→ 空映射；
        ``MULTI_AGENT`` → **恰好覆盖全部 child**（不多、不少）。
        """
        if not isinstance(plan, Plan):
            raise TypeError(f"plan 必须是 Plan，收到 {plan!r}")
        if not isinstance(selection, ModelSelection):
            raise TypeError(
                f"selection 必须是**已形成**的 ModelSelection（[06] 的决策结果），"
                f"收到 {selection!r}"
            )
        return tuple(
            (spec.branch_id, self._inherited(selection, spec.branch_id))
            for spec in plan.branches
        )

    @staticmethod
    def _inherited(selection: ModelSelection, branch_id: BranchId) -> ModelSelection:
        """逐 child 的继承副本 —— **只换理由，不动档位/强度**（A4.6）。

        理由里带上 ``branch_id``，使每条 assignment 都能**自证身份**：
        光看产物就知道"哪个分支、为什么是这个档"。
        """
        return ModelSelection(
            tier=selection.tier,
            effort=selection.effort,
            reason=(
                f"child {branch_id}: inherit task-level selection "
                f"（tier={selection.tier.value}, effort={selection.effort.value}）；"
                "no per-branch evidence（A4）"
            ),
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _base_tier(evaluation: Evaluation) -> ModelTier:
        """L33 的 4 档映射。**复杂度是主判据**，影响范围与复核价值次之。

        ⚠️ 这是"**刚好够用**"的落点，不是"越难越贵"：

            低复杂度 + LOCAL           → 最低档（"机械明确、低风险"）
            中复杂度 + LOCAL           → 平衡档（"常规编码分析"）
            跨模块 / 系统级            → **较强档**（L33："跨模块 → 较强"）
            高复杂度                   → 较强档（"复杂调试 / 重要审查"）
            系统级 + 高复杂度 + 需复核 → 最高档（只在必要时）

        ⚠️ **影响范围与复核价值有独立的档位下限，不依赖复杂度。**
           L33 明确"跨模块、复杂调试、重要审查 → 较强 / 较高"；
           因此跨模块任务**即便复杂度低或机械**，也不得落到较强档以下。
        """
        scope = evaluation.impact_scope
        cross_module = scope in (ImpactScope.CROSS_MODULE, ImpactScope.SYSTEM_WIDE)

        # 最高配置：只在系统级 + 高复杂度 + 有复核价值时
        if (
            scope is ImpactScope.SYSTEM_WIDE
            and evaluation.complexity is Ordinal.HIGH
            and evaluation.review_value is Ordinal.HIGH
        ):
            return ModelTier.MAXIMUM

        # 复核任务有**独立的档位下限**：重要审查属 L33 的第三类，
        # 即便代码本身简单也要求较强档。
        if evaluation.review_value is Ordinal.HIGH:
            return ModelTier.STRONG

        # 跨模块 / 系统级同样有**独立的档位下限**（L33："跨模块 → 较强"）。
        if cross_module:
            return ModelTier.STRONG

        if evaluation.complexity is Ordinal.HIGH:
            return ModelTier.STRONG
        if evaluation.complexity is Ordinal.MEDIUM:
            return ModelTier.BALANCED
        return ModelTier.LOW_COST


def _index(tier: ModelTier) -> int:
    return _TIER_ORDER.index(tier)


def _step(tier: ModelTier, steps: int) -> ModelTier:
    if not isinstance(tier, ModelTier):
        raise TypeError(f"tier 必须是 ModelTier，收到 {tier!r}")
    if not isinstance(steps, int) or isinstance(steps, bool):
        raise TypeError(f"steps 必须是 int，收到 {steps!r}")
    target = max(0, min(len(_TIER_ORDER) - 1, _index(tier) + steps))
    return _TIER_ORDER[target]
