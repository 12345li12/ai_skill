"""``execution_recovery_callback`` —— **既有 `on_execution` 运行期 callback seam 的生产实现**。

架构依据：`docs/architecture.md`

- 2 节 ``[14] Recovery Router`` 节点行：**"它只能产生决策，由对应节点重新做正式决策"**；
- **A17.2 性质 2**：**"只路由不执行"** —— `route_to` 只是**给对应节点的正式重决策请求**；
- **L228**：`[03]` 的重查策略（V1 冻结）= **事件驱动**，只重查可能变化的维度，**不定时轮询**。

范围、体检与授权见 `tests/behavior/README_step5b.md` **§210**（Production Reachability Preflight）·
**§211**（Scope Review）· **§212**（五项裁决登记）· **§213**（实现授权申请）。

**本模块是什么（一句话）：**

    它是冻结的 ``on_execution(composition_root, routing)`` seam 的**生产实现**：
    **驱动一个 pass** → 读该 pass 的 `failures` → **先全量验证** → 才允许 `CAPABILITY` 重查。

**本模块不是什么：**

  · ⛔ **不是**新架构节点 —— 它实现的是**既有**冻结 seam（`router_wrapper.py` 的 `on_execution`），
    不新增 `[N]` 节点、不新增决策域；
  · ⛔ **不是** `Coordinator` / `Dispatcher` / `Orchestrator`（⛔ 本模块**不定义**任何此类类型）；
  · ⛔ **不拥有新决策权**：不判 PASS/FAIL · 不选节点 · 不改计划 · 不算终态；
  · ⛔ **不持久化状态**：每次调用**自治**（零实例属性 · 零模块级可变容器 · 零跨调用缓存）；
  · ⛔ **不自触发**：触发者是既有 `RouterWrapper`；
  · ⛔ **不形成事件循环**：`drive_next_pass()` **恰一次**，**零第二 pass**；
    本模块**只**含**有限集合的有界遍历**（`report.started` / `report.failures` / 局部 `requests` 列表）
    —— ⛔ **零 `while`**（无轮询 · 无重试环 · 无自旋）· ⛔ 零 `poll` / `sleep` / 时钟
    ⚠️ "**零 loop**" 的准确含义是"**零循环驱动**"（不得 `while` 驱动 pass、不得轮询等待），
      **不是**"零 `for`" —— R3 要求"**逐条**处理 failures"，那本身就需要有界遍历。

**六条冻结规则（`§211.7` + `§212.1` 的对称锁）+ 两条红线：**

    R0 构造 `PassDriver` → `drive_next_pass()` **恰一次**
    R1 **两阶段**：**所有可导致拒绝的检查先于任何 `recheck` 副作用**
       ⇒ Phase 1 只**形成 + 检查**（零副作用）· Phase 2 整体检查 · Phase 3 **才**派发
    R2 `attempt` **来自 witness 三元组**（`P6 / D-P6C″` · A17.6「由上游显式传入」）：
       `report.failures` 的元素 = `(branch_id, attempt, failure)` ⇒ ⛔ **不再**建
       `started → attempt` 索引、⛔ **不再**反查 `report.started`、⛔ **不得猜 attempt**
       （旧 `started ↔ failures` 配对**已撤销**：对真实 start-failure 报告 `started == ()`）
    R3 基数：**逐条**处理（不合并、不去重）· `failures == ()` ⇒ 零请求零派发**且不抛错** ·
       全部请求为 `None`（已达 `total_attempt_limit`）⇒ **合法终态**：零派发零抛错 ·
       **同 `branch_id` 在 `failures` 重复** ⇒ **显式失败**
    R4 非 `CAPABILITY` ⇒ **上抛**（**不吞、不降级、不记录后继续**）
    R5 **每次调用自治**（见上"不持久化状态"）
    R6 **只驱动一个 pass**
    🔴 **红线 1**：⛔ **不得**因 `recheck()` 已更新 registry 就再跑一个 pass
       （重查结果的消费点是**下一 pass**，其触发者属**另一轮**）
    🔴 **红线 2**：⛔ **不包装** `RecoveryRouteWiringError` / `RedecisionDispatchError`
       —— 它们**原样上抛**，保持三种错误来源的 **provenance 分离**
       （callback-local 报告不变量失败才用 `ExecutionRecoveryCallbackError`）

**错误 provenance（三条边界，互不混用）：**

    `RecoveryRouteWiringError`        → **上游**：`C2a` 请求形成 / 接线失败   ⇒ **原样上抛**
    `RedecisionDispatchError`         → **下游**：已形成请求后的 consumer 派发失败 ⇒ **原样上抛**
    `ExecutionRecoveryCallbackError`  → **本层**：callback-local **报告不变量**失败（三种）

⚠️ **禁止子串扫描**：本文件 docstring 里**故意提及** `PassDriver` / `drive_next_pass` /
`recheck` / `RedecisionDispatchError` 等词 ⇒ 全部锁一律 **AST 级 / 绑定集级**。
"""

from __future__ import annotations

from .capabilities import CapabilityRegistry
from .composition_root import CompositionRoot
from .contracts import RecoveryTarget, RoutingPlan
from .execution import ExecutionPassReport
from .pass_driver import PassDriver
from .recovery_route_wiring import RedecisionRequest, build_redecision_request
from .recovery_wiring import route_adapter_failure
from .redecision_dispatch import RedecisionDispatchError, dispatch_redecision

__all__ = ["ExecutionRecoveryCallbackError", "execution_recovery_callback"]


class ExecutionRecoveryCallbackError(RuntimeError):
    """**callback-local 报告不变量**失败（三种，且**只有**这三种）。

    ⚠️ 它**不**用于上游/下游的错误 —— 那两者**原样上抛**（红线 2）：
      · `RecoveryRouteWiringError`（`C2a` 请求形成 / 接线）
      · `RedecisionDispatchError`（consumer 派发）

    ⚠️ `P6 / D-P6C″` 之后本类**只覆盖一种**情况（旧 ①`failure.branch_id ∉ started`
    与 ②`started` 重复 `branch_id` **随旧 R2 配对一起撤销**）：
      `failures` **重复** `branch_id`（同一分支被当成两个恢复事实 ⇒ 重复消费）
    """


def execution_recovery_callback(
    composition_root: CompositionRoot,
    routing: RoutingPlan,
) -> None:
    """**驱动一个 pass**，并对该 pass 的失败事实做恢复链处理。

    签名**逐字匹配**冻结的 `on_execution` seam（`router_wrapper.py`）：
        `Callable[[CompositionRoot, RoutingPlan], None]`
    ⚠️ 因此：入参是**位置参数**；返回**恒为 `None`** ——
      callback 是"**执行请求的转交端**"，**不建立第二条结果返回通道**；
      重查结果通过**同一个 `CapabilityRegistry`** 的状态变化被**下一 pass** 读取。

    **两阶段（R1 的核心不变式）**：

        Phase 1（**纯形成 + 纯检查**）：对 `report.failures` 逐条
            `route_adapter_failure` → `build_redecision_request`，**只收集**请求（零副作用）
        Phase 2（**先整体检查**）：若收集到的请求里**存在非 `CAPABILITY` 目标**
            ⇒ **立即抛错**（此时**零** `recheck` 发生过）
        Phase 3（**才派发**）：仅当请求**全部**为 `CAPABILITY` ⇒ 逐条 `dispatch_redecision`

    ⇒ 于是"**是否会发生 capability 重查**"与"**失败在 `failures` 里的排列顺序**"**完全无关**。

    ⚠️ **只驱动一个 pass**（R6 / 红线 1）：本函数内 `drive_next_pass()` **恰一次**；
      重查之后**不再**驱动第二个 pass —— 重查结果的消费点是下一 pass，
      而下一 pass 的触发者属**另一轮**（续跑事件源，仍 CLOSED）。
    """
    driver = PassDriver(composition_root=composition_root, routing=routing)
    report: ExecutionPassReport = driver.drive_next_pass()

    limit = composition_root.total_attempt_limit

    # ---- Phase 1 · **纯形成 + 纯检查**（⛔ 这一段零副作用）----
    # `P6 / D-P6C″`：`attempt` **直接**取自 witness 三元组（⛔ 无索引 · ⛔ 无反查 · ⛔ 无猜测）。
    requests: list[RedecisionRequest] = []
    seen_branches: set[str] = set()
    for branch_id, attempt, failure in report.failures:
        if branch_id in seen_branches:
            raise ExecutionRecoveryCallbackError(
                f"failures 中 branch_id={branch_id!r} 重复：同一分支不得被当成两个恢复事实"
                "（重复消费会制造虚假的第二次恢复）"
            )
        seen_branches.add(branch_id)

        outcome = route_adapter_failure(
            branch_id=branch_id,
            attempt=attempt,
            total_attempt_limit=limit,
            failure=failure,
        )
        request = build_redecision_request(outcome=outcome)
        if request is None:
            # `decision is None` = 已达 total_attempt_limit ⇒ **合法终态**，不是缺陷
            continue
        requests.append(request)

    # ---- Phase 2 · **先整体检查**：任何非 CAPABILITY ⇒ 此刻就失败（零 recheck）----
    # ⚠️ **本段的身份 = batch preflight mirror guard**（**不是**权威判定 —— `§216` 的措辞收口）：
    #    "callback 的 Phase 2 是对冻结 consumer 可达集的 **batch preflight mirror guard**，
    #     **不产生** target、**不改变** target；真正的**单请求** consumer 行为
    #     仍由 `dispatch_redecision` 承担。"（它自己也有同一个 CAPABILITY / 非 CAPABILITY 分界）
    #    ⇒ 本段存在的**唯一理由**是"**批次原子性**"：把拒绝**提前到任何 `recheck` 副作用之前**，
    #      否则会留下"已重查一次、然后才报错"的**顺序依赖性部分副作用**。
    #    ⚠️ **防静默漂移**：测试里有跨模块一致性锁 ——
    #      若 `dispatch_redecision` 今后改变其"能走完"的 target 集合，本守卫必须**同步变红**。
    for request in requests:
        if request.target is not RecoveryTarget.CAPABILITY:
            raise RedecisionDispatchError(
                f"本 pass 的恢复请求里存在非 CAPABILITY 目标（target={request.target.value!r}）："
                "在**任何** capability 重查发生**之前**整体失败"
                "（否则会留下'已重查一次、然后才报错'的**顺序依赖性部分副作用**）"
            )

    # ---- Phase 3 · **才派发**（此时已保证全部为 CAPABILITY）----
    capabilities: CapabilityRegistry = composition_root.capabilities
    for request in requests:
        dispatch_redecision(request=request, capabilities=capabilities)
