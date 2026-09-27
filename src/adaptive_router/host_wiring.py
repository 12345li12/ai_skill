"""``host_wiring`` —— **Host 侧薄装配模块**（`composition only`；`C 路线` / `5B.6.39`）。

架构依据：`docs/architecture.md` v1.0 FROZEN **4.1 节**（Session BYPASS 流）·
`entrypoint.py` 的冻结定位（"**composition root / host-facing wiring boundary**"）。
范围、体检与授权见 `tests/behavior/README_step5b.md` **§198**（范围申请）/ **§199**（Preflight 结果）/
**§200**（实现授权申请）。

**本模块是什么（一句话）：**

    把外部依赖收进来 → 构造 ``CompositionRoot`` → 构造 ``RouterWrapper`` →
    **返回既有的** ``EntrypointHandlers``（其 ``router_core`` = 真 ``RouterWrapper``）。

⇒ 于是 ``entrypoint.dispatch`` **首次**获得一个可被 Host 直接使用的完整 handler 包。

**为什么需要它（`§199.2` / `§199.4` 的实测缺口）：**

    ⚠️ **该描述已被 `§201`（本模块）与 `§217`（recovery callback）两轮更新
    （`§220.1` / `§220.9` 的实测订正）** —— 按当前源码（AST 构造点普查 · `src/`）：
      · ``CompositionRoot(`` 在 ``src/`` 内构造点 = **1**（本模块 `L144`）；
      · ``RouterWrapper(``   在 ``src/`` 内构造点 = **1**（本模块 `L156`）；
      · ``PassDriver(``      在 ``src/`` 内构造点 = **1**
        （``execution_recovery_callback.py:115``）。
    ⇒ ⇒ 这条链（``dispatch → RouterWrapper → CompositionRoot → PassDriver``）
      **在生产面各恰有 1 个构造点**。
    ⚠️ **真正的缺口在别处**（本模块与 callback **都未改变**这一点）：
      上述对象**仍然只在局部调用作用域内存在** —— `router_wrapper` 的 ``root`` / ``core``、
      callback 的 ``driver`` **全是局部名称**；⇒ "**装配已存在**" 与
      "**跨调用连续性已存在**" 是**两件事**，后者当前**没有**承载者。
    本模块仍只补**最后那个装配点**，不新增任何架构概念。

**本模块不是什么（四条，逐条对应 §200.6 的禁止清单）：**

  · **不驱动任何 pass**（`方案 (i)` · 你 `§200.5` 的裁决）：本模块内 ``PassDriver(`` 与
    ``drive_next_pass`` 调用点**均为 0** ⇒ "驱动一个 pass"是**调用方**的责任
    （``on_execution`` 的签名已冻结为 ``(composition_root, routing) -> None``，调用方据此写一行即可）；
  · **不持有业务判断**：不判 PASS/FAIL · 不算终态 · 不选节点 · 不改计划；
  · **不设循环**：``while`` / ``for`` / ``poll`` / ``sleep`` **全不出现**；**零时钟**；
  · **不 import ``adapters``**：``adapter`` 是**入参**（duck-typed）⇒
    ``L11-2`` 的"``adapters`` 只有 ``execution.py`` 一个导入者"**保持不变**。

**为什么不提供"驱动一次"的便利入口（`§200.5` 的 (i) vs (ii)）：**

    装配层 = **对象图构造**；执行驱动 = **外部调用责任**；pass 生命周期 = ``PassDriver`` 的已冻结边界。
    把一次驱动提升到本模块，会让装配层开始承担 runtime orchestration，**容易侵入 `K4` 边界**
    （``K4``：``PassDriver`` 不自触发、零循环、零 polling，下一次由**外部状态变化**触发）。
    ⇒ 因此本模块**只装配**。

⚠️ **本模块不是新的架构节点**（与 ``entrypoint.py`` 同性质的 composition seam ——
   ``entrypoint.py`` 的冻结 docstring 逐字："本模块**不是第 19 个架构节点**"）。

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** ``PassDriver`` / ``drive_next_pass`` /
   ``adapters`` / ``terminal`` 等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Mapping

from .composition_root import CompositionRoot
from .entrypoint import EntrypointHandlers, ProjectLoader
from .router_wrapper import RouterWrapper

if TYPE_CHECKING:  # pragma: no cover - 仅供静态检查（运行期已 import 上面三个真实类型）
    from .contracts import BranchId, ProjectContext, RunId, RuntimeControlFacts
    from .admission_facts import AdmissionFacts
    from .capabilities import CapabilityRegistry
    from .execution_context_facts import ExecutionContextFacts
    from .reusable_results import ReusableResultRef
    from .session import EmittableReply, SessionGate

__all__ = ["build_entrypoint_handlers"]

#: **自证用的冻结形参表** —— 与函数签名**逐字**对应（`LHP-2` 的判定面）。
#: ⚠️ 它是本模块唯一一份"面清单"；两者不一致 ⇒ 锁变红。
_REQUIRED_KWONLY = (
    "facts_source",
    "capabilities",
    "adapter",
    "execution_context_facts_by_branch",
    "confirmed_conclusions_by_branch",
    "session_gate",
    "control_facts_source",
    "admission_facts_source",
    "reusable_result_source",
    "continuation_source",
)

_OPTIONAL_KWONLY = (
    "project_loader",
    "ask_enable",
    "host_direct",
    "on_execution",
    "total_attempt_limit",
    "run_id_factory",
    "router_overrides",
)


def build_entrypoint_handlers(
    *,
    facts_source: object,
    capabilities: object,
    adapter: object,
    execution_context_facts_by_branch: Mapping[object, object],
    confirmed_conclusions_by_branch: Mapping[object, tuple[str, ...]],
    session_gate: object,
    control_facts_source: Callable[[], tuple[object, str]],
    admission_facts_source: Callable[[object], object],
    reusable_result_source: Callable[[object], object | None],
    continuation_source: Callable[[], int | None],
    project_loader: object | None = None,
    ask_enable: Callable[[object], object] | None = None,
    host_direct: Callable[[object], object] | None = None,
    on_execution: Callable[[CompositionRoot, object], None] | None = None,
    total_attempt_limit: int = 2,
    run_id_factory: Callable[[], str] | None = None,
    router_overrides: Mapping[str, object] | None = None,
) -> EntrypointHandlers:
    """**只装配**：构造组合根与包装边，返回**既有**的 ``EntrypointHandlers``。

    **必填（10 项）** —— 其中 9 项是 ``CompositionRoot`` 的外部 seam（**原样转交**，本模块**不构造**），
    第 10 项 ``session_gate`` 由**部署侧**提供（本模块**不**替 Host 决定会话状态）：

        ``facts_source`` · ``capabilities`` · ``adapter`` ·
        ``execution_context_facts_by_branch`` · ``confirmed_conclusions_by_branch`` ·
        ``session_gate`` · ``control_facts_source`` · ``admission_facts_source`` ·
        ``reusable_result_source`` · ``continuation_source``

    **可选** —— 三个 Host handler **原样转交**（可为 ``None``；缺哪一项就在对应分支显式失败，
    那是 ``entrypoint.dispatch`` 的既有行为，**不由本模块**替它决定）：

        ``project_loader`` · ``ask_enable`` · ``host_direct`` · ``on_execution`` ·
        ``total_attempt_limit`` · ``run_id_factory`` · ``router_overrides``

    ⚠️ ``control_facts_source`` 的**归属**（`§199.6` 的体检结论）：它是**外部 seam**
    （``CompositionRoot`` 的注入面表逐字标"**外部**"），本模块**只转交** ——
    **不构造**它、**不新增** facts 类型、**不改变** ``K3-b`` 的语义（``K3-b`` 保持 **OPEN**）。

    ⚠️ ``adapter`` 由**外部提供**（本模块**不** import ``adapters``、**不**构造任何 adapter）
    ⇒ ``InMemoryAdapter(`` 在 ``src/`` 内仍是 **0** 处。

    ⚠️ 本函数**不**驱动 pass：``PassDriver(`` 与 ``drive_next_pass`` 在本模块内调用点**均为 0**（`方案 (i)`）。

    ⚠️ **`None` 的处理口径**（一处实现期修正，见 `§201`）：``run_id_factory`` / ``router_overrides``
    为 ``None`` 时**不转交** ``None`` —— 而是**省略该实参**，让 ``CompositionRoot`` 用它**自己的默认**
    （``new_run_id`` / 空映射）。理由：``router_overrides`` 的默认是 ``field(default_factory=dict)``，
    转交 ``None`` 会让 ``dict(None)`` 抛 ``TypeError``；而"缺省"与"显式传 None"在**本函数的语义**里是同一件事。
    ⛔ 这**不是**静默默认：本模块没有**发明**任何值 —— 缺省值仍由 ``CompositionRoot`` 提供。
    """
    optional: dict[str, object] = {}
    if run_id_factory is not None:
        optional["run_id_factory"] = run_id_factory
    if router_overrides is not None:
        optional["router_overrides"] = router_overrides

    root = CompositionRoot(
        facts_source=facts_source,
        capabilities=capabilities,
        adapter=adapter,
        execution_context_facts_by_branch=execution_context_facts_by_branch,
        confirmed_conclusions_by_branch=confirmed_conclusions_by_branch,
        control_facts_source=control_facts_source,
        admission_facts_source=admission_facts_source,
        reusable_result_source=reusable_result_source,
        continuation_source=continuation_source,
        total_attempt_limit=total_attempt_limit,
        **optional,  # type: ignore[arg-type]
    )
    core = RouterWrapper(composition_root=root, on_execution=on_execution)
    return EntrypointHandlers(
        session_gate=session_gate,
        project_loader=project_loader,
        ask_enable=ask_enable,
        host_direct=host_direct,
        router_core=core,
    )
