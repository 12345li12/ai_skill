"""``[14] → [03]`` **redecision dispatch seam**（`K1-b` 最小接入 · 只接 `CAPABILITY`）。

架构依据：`docs/architecture.md`

- 2 节 ``[14] Recovery Router`` 节点行：**"它只能产生决策，由对应节点重新做正式决策"** ——
  所以"决策"与"正式重决策"之间必须有一个**显式请求**（`C2a` 已交付：`RedecisionRequest`）；
- **A17.2 性质 2**：**"只路由不执行"** —— `route_to` 只是**给对应节点的正式重决策请求**；
- **A17.2 / A17.3**：经 `FailureClass → RecoveryTarget` 落到**三个可达目标**，
  其中 **`CAPABILITY` = 重查**（L2977 逐字："`CAPABILITY` = 重查"）；
- **L2983 / L2988**：§4.6 **R3** 明文 ——「能力失效重查」的触发 = 「**工具失败** / **环境切换**」；
- **L228**：`[03]` 的**重查策略（V1 冻结）= 事件驱动**，只重查可能变化的维度，**不定时轮询**。

范围、体检、冻结与授权见 `tests/behavior/README_step5b.md` **§204**（seam 裁定 Preflight）·
**§205**（裁决登记）· **§206**（`K1-b` 最小接入 Scope Review）· **§207**（实现授权申请）。

**本模块是什么（一句话，实现前最后一条声明的原文）：**

    ``redecision_dispatch`` **只消费已存在的 `RedecisionRequest`**，
    **不负责生产请求、不负责决定 target、不负责装配 consumer**。

⇒ 它是 `[14] → [03]` 这条**既有边**缺失的**运行期连接角色**，
  **不是**一个新的 `[N]` 架构节点、**不是**新增职责域、
  **不是** `Dispatcher` / `Coordinator` / `Orchestrator` 之类的架构实体
  （⛔ 本模块**不定义**任何此类类型；下面那个函数名里的 `dispatch` 是**动作语义**
    "把请求送到对应节点"，不是实体名）。

**它做什么（恰好一件事）：**

    `RedecisionRequest.target` ⇒ `[03]` 的正式重决策入口
    · `RecoveryTarget.CAPABILITY` ⇒ ``capabilities.recheck(RecheckTrigger.ENVIRONMENT_SWITCHED)``
      并**原样返回**那次重查的 `CapabilitySnapshot`
    · 其余**四个**目标（`PLANNER` / `MODEL_ROUTER` / `CONTEXT` / `SCHEDULER`）⇒ **显式失败**

**它不做什么（越界即缺陷）：**

  · ⛔ **不生产** `RedecisionRequest`（那是 `[14]` 与 `C2a` 的活；`LRT-1` 的唯一构造点仍只有一处）
  · ⛔ **不决定** `target`（那是 `[14]` 的 `RecoveryDecision.route_to`）
  · ⛔ **不装配** consumer（`capabilities` 由调用方**注入**；本模块**不构造** registry）
  · ⛔ 不构造 `RecoveryDecision` / `RecoveryOutcome` · **不 import** `recovery.py`（`LRT-3` 同型纪律）
  · ⛔ 不写运行态 · 不碰 `[10]` · 不判终态 · 不重过授权门 · 不重估成本 · 不写 attempt
  · ⛔ 不驱动 pass（`PassDriver` / `drive_next_pass` 调用点 = **0**）· 零循环 · 零时钟
  · ⛔ **不给**任何 V1 不可达的 trigger 补 producer（尤其 `TOOL_FAILURE` ——
      架构 A17.3 逐字把它列为 **V1 不可达**，且"自行判断成 TOOL"是**发明事实**，A8.2）

⚠️ **为什么只接 `CAPABILITY`（不许"为了完整枚举而制造消费者"）**：
  当前证据只闭合了 `RecoveryTarget.CAPABILITY` ⇒ 其余目标**显式失败** ——
  "**未证明存在的目标，不生成运行路径**"（`§205.1` 的 ② 裁定）。

⚠️ **注入一致性（重要）**：`capabilities` **必须是**组合根 ``CompositionRoot.capabilities`` 的
   **同一个实例** —— 否则重查结果不会被 `PassDriver` 每轮的 ``snapshot()`` 读到
  （`§181.4.3` 已冻结"capability 由驱动者从**同一个 root** 每轮解析"）。
   本模块**不接受** `CompositionRoot` 本身（那会把装配职责拉进本 seam，与 `host_wiring` 重复）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `PassDriver` / `drive_next_pass` /
   `recheck` / `snapshot` 等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .capabilities import CapabilityRegistry, RecheckTrigger
from .contracts import RecoveryTarget
from .recovery_route_wiring import RedecisionRequest  # noqa: TID252  (C2a 冻结载体：只 import，零修改)

if TYPE_CHECKING:  # pragma: no cover - 仅供静态检查（**不做**运行期导入）
    from .capabilities import CapabilitySnapshot

__all__ = ["RedecisionDispatchError", "dispatch_redecision"]


class RedecisionDispatchError(RuntimeError):
    """``redecision dispatch seam`` 的显式失败。

    ⚠️ **不复用** `RecoveryRouteWiringError` —— 两者**错误边界不同**：
      · `RecoveryRouteWiringError` = **`C2a` 接线失败**（请求载体的构造/守卫阶段）
      · `RedecisionDispatchError` = **请求已经形成，但消费动作失败**（分发阶段）
    共用异常会破坏故障定位：无法区分"producer 成功？carrier 成功？consumer 调用失败？"
    """


def dispatch_redecision(
    *,
    request: RedecisionRequest,
    capabilities: CapabilityRegistry,
) -> CapabilitySnapshot:
    """把一个**已存在的** `RedecisionRequest` 送到它的目标节点（本批：只送 `CAPABILITY`）。

    **参数（全 keyword-only · 恰 2 项）**

        ``request`` —— `C2a` 的冻结载体（`branch_id` / `attempt` / `target` /
          `minimal_correction` / `carry_forward`）。本模块**只读** `target` 用于分派；
          ⚠️ 其余四项**不在这里消费**（`carry_forward` 的 producer 在 V1 无来源，已登记为上报项）。

        ``capabilities`` —— **调用方注入**的 `CapabilityRegistry`；必须是组合根持有的**同一个实例**
          （见模块 docstring 的"注入一致性"）。

    **行为（恰两条路径）**

        ① ``request.target is RecoveryTarget.CAPABILITY``
           ⇒ ``capabilities.recheck(RecheckTrigger.ENVIRONMENT_SWITCHED)``，**原样返回**其结果。
           ⚠️ `ENVIRONMENT_SWITCHED` **不是本模块的判断** —— 它来自架构 L2983/L2988 的 §4.6 **R3**
             明文（「能力失效重查」的触发 = 「工具失败 / **环境切换**」），
             而 `route_to == CAPABILITY` 的 V1 具名来源 = `AdapterFailureKind.{CRASH, NETWORK}`
             （A17.1 ⇒ `FailureClass.EXTERNAL_SERVICE`）= **环境/外部服务异常**。
             ⇒ 本模块**只做翻译**（既有 `route_to` ⇒ 既有 trigger 常量），
               ⛔ **不**自行判断"这是不是环境切换"（那会撞 A17.3 / A8.2）。
           ⚠️ `recheck` 是**昂贵且可重入**的（`capabilities.py` L168 逐字："有副作用、昂贵、**可重入**"）
             ⇒ 调用方应对"每次请求调用一次"负责；本模块**不做幂等缓存、不做去重**。

        ② 其余**四个**目标 ⇒ **显式失败**（`RedecisionDispatchError`），**零** `recheck` 调用。
           · `PLANNER` / `MODEL_ROUTER`：在 `REDECISION_TARGETS` 白名单内，但**尚无**对应的
             正式重决策入口 ⇒ "未证明存在的目标，不生成运行路径"
           · `CONTEXT` / `SCHEDULER`：**不在**白名单内（上游 `LRT-4` 会拦下）；本模块**仍**显式拒绝，
             以防有人绕过上游守卫直接构造 —— **禁静默默认**
    """
    if not isinstance(request, RedecisionRequest):
        raise RedecisionDispatchError(
            f"request 必须是 RedecisionRequest（C2a 的冻结载体），收到 {request!r}"
        )
    if not isinstance(capabilities, CapabilityRegistry):
        raise RedecisionDispatchError(
            f"capabilities 必须是 CapabilityRegistry（且应与组合根同一个实例），收到 {capabilities!r}"
        )

    target = request.target
    if target is RecoveryTarget.CAPABILITY:
        return capabilities.recheck(RecheckTrigger.ENVIRONMENT_SWITCHED)

    raise RedecisionDispatchError(
        f"target={target.value!r} 尚无正式重决策入口：本批只交付 `CAPABILITY` 的消费者"
        "（`PLANNER` / `MODEL_ROUTER` 未证明存在入口；`CONTEXT` / `SCHEDULER` 不在 V1 可达集）"
        " ⇒ 显式失败，**不得**静默成功或忽略"
    )
