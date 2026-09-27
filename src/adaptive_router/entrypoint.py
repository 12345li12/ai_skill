"""entrypoint —— 外置 SessionGate 的 composition root / host-facing wiring boundary。

架构依据：`docs/architecture.md` v1.0 FROZEN 4.1 节（Session BYPASS 流）

落实已冻结的控制流：

```text
Host / orchestration entry
        ↓
   SessionGate
   ├─ UNASKED   → ask-enable handler → 立即结束
   │              （不读 Project Context、不调用 Router Core）
   ├─ DISABLED  → host_direct handler
   │              （不读 Project Context、不调用 Router Core）
   └─ ENABLED   → ProjectContextLoader.load(root) → Router Core
```

⚠️ **本模块不是第 19 个架构节点。** 它只是 **composition root** ——
   把已经冻结的分支关系接起来，不新增决策。

⚠️ **四条边界：**

   1. **直接透传 handler 返回值。** 本模块**不包装**结果 ——
      尤其 DISABLED BYPASS 时，用户看到的是正常的 Host / Main Agent 输出，
      **不应需要知道调度层存在**（架构 4.1："不污染结果通道"）。
      审计证据由 call count 与调用顺序提供，不由返回类型提供。

   2. **BYPASS 在 Router 外部。** 架构 4.1 明确：DISABLED 下
      "根本不经过任何后续节点"，**不是**"进入 Router 后提前 return"。
      因此用**不调用** ``router_core`` 表达 bypass，
      **不引入** ``RouterOutcome`` 之类的返回值类型
      （冻结架构中不存在该类型，全文搜索 0 命中）。

   3. **不判断 Session。** Session 判定唯一来源是 ``SessionGate.evaluate()``；
      状态迁移复用 ``session.py`` 的既有接口，**不造第二套 Session DTO**。

   4. **不绕过 Session 的时间语义。** L5 / ``TEST-SESSION-003`` 要求：
      问过之后，**必须等用户实际发来下一条消息**，才能把"没明确回答"
      解释成 DISABLED。因此：
      - 已问过但未收到真实下一条用户消息时，二次分派**继续 hard-stop**
        （不重复提问，也**绝不**执行 ``host_direct`` / loader / Router Core）；
      - Session 状态的迁移只能由 ``SessionGate.note_user_message()`` /
        ``record_decision()`` 完成 —— **entrypoint 不自行解释**。

⚠️ ``router_core`` 的签名里**没有** ``SessionGateResult`` ——
   Router Core 不需要知道 UNASKED / DISABLED / bypass 这些概念，
   它只接受"已经通过 Gate、并已拥有 ProjectContext 的任务"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .contracts import ProjectContext
from .session import EmittableReply, SessionGate

__all__ = [
    "ProjectLoader",
    "RouterCore",
    "EntrypointHandlers",
    "EntrypointError",
    "dispatch",
]


class EntrypointError(RuntimeError):
    """composition seam 无法安全继续。

    调用方装配错误（缺 handler），或同一 user turn 内异常地二次分派
    且 Session 尚未收到真实下一条用户消息。
    """


class ProjectLoader(Protocol):
    """``[17] Project Context Loader`` 的最小结构（结构化类型，不绑定实现）。"""

    def load(self, root: str, *, refresh: bool = False) -> ProjectContext:
        ...  # pragma: no cover - 协议声明


class RouterCore(Protocol):
    """Router Core 的最小结构。

    ⚠️ **签名里没有 ``SessionGateResult``** —— Core 不需要知道
       UNASKED / DISABLED / bypass。
    """

    def __call__(self, task: Any, context: ProjectContext) -> Any:
        ...  # pragma: no cover - 协议声明


@dataclass
class EntrypointHandlers:
    """composition seam 的全部注入点。

    ⚠️ ``session_gate`` 直接复用 ``session.py`` 的 ``SessionGate`` ——
       唯一判定来源，**不新增 DTO**。

    ``ask_enable`` 接收首次启用询问的回复形状（``EmittableReply``，L5 冻结：
    **只含问题并立即结束**），返回什么由 Host 决定。
    """

    session_gate: SessionGate
    project_loader: ProjectLoader | None = None
    ask_enable: Callable[[EmittableReply], Any] | None = None
    host_direct: Callable[[Any], Any] | None = None
    router_core: RouterCore | None = None


def dispatch(
    task: Any,
    *,
    project_root: str,
    handlers: EntrypointHandlers,
) -> Any:
    """按冻结的 4.1 控制流分派，并**直接返回**被选中 handler 的结果。

    顺序固定：

        1. ``SessionGate`` 判定（**唯一 gate**）
        2. UNASKED → 首次询问（成功后才记账），立即结束
        3. 非 ENABLED → ``host_direct(task)``（不读项目、不调用 Router Core）
        4. ENABLED → ``ProjectContextLoader.load(project_root)`` → ``router_core(task, ctx)``

    ⚠️ **不接收** ``refresh`` —— R5 失效判断属控制层，不在这里猜。
    """
    gate = handlers.session_gate
    result = gate.evaluate()

    # ---- UNASKED：只问启用问题，立即结束 ------------------------------
    if result.must_ask_first:
        if gate.may_ask_now():
            return _ask_enable(gate, handlers)
        # 已问过，但用户**尚未实际发来下一条消息**。
        # ⚠️ 不得在此解释成 DISABLED，也不得走 host_direct / loader / core。
        #    状态迁移只能由 SessionGate.note_user_message() 之后发生。
        raise EntrypointError(
            "已发出首次启用询问，正在等待用户的下一条消息；"
            "在 SessionGate 收到真实的下一条用户消息之前，分派必须继续 hard-stop"
            "（不重复提问，也不进入 Host direct / Project Context / Router Core）"
        )

    # ---- DISABLED：交还 Host，**不进入 Router** ------------------------
    if not result.enters_router:
        if handlers.host_direct is None:
            raise EntrypointError("host_direct 分支需要注入 host_direct handler")
        return handlers.host_direct(task)

    # ---- ENABLED：Project Context → Router Core -----------------------
    if handlers.project_loader is None:
        raise EntrypointError("ENABLED 分支需要注入 project_loader")
    if handlers.router_core is None:
        raise EntrypointError("ENABLED 分支需要注入 router_core")

    context = handlers.project_loader.load(project_root)
    return handlers.router_core(task, context)


def _ask_enable(gate: SessionGate, handlers: EntrypointHandlers) -> Any:
    """发出首次启用询问。

    ⚠️ 顺序有意为之（``mark_asked()`` **在最后**）：

       1. 缺 handler          → 抛错，``asked`` 保持 False
       2. 违规 compose        → 抛错，``asked`` 保持 False
       3. ``ask_enable()`` 抛错 → 抛错，``asked`` 保持 False
       4. **询问真正成功发出后**，才 ``mark_asked()``

    否则失败路径会留下 ``asked=True`` 但问题从未发出的假状态，
    从而永远不再询问（L5 只允许问一次）。
    """
    if handlers.ask_enable is None:
        raise EntrypointError("UNASKED 分支需要注入 ask_enable handler")

    reply = gate.compose_first_question()
    outcome = handlers.ask_enable(reply)
    gate.mark_asked()
    return outcome
