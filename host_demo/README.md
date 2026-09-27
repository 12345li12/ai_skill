# `host_demo` —— 外部 Host 参考演示

> **本目录是「外围证明层」，不是产品入口。**
> 它证明的是：**被冻结的 Core 确实可以被外部 Host 驱动**。
> ⛔ 不是可安装入口 · ⛔ 不是 CLI 产品 · ⛔ 不修改 Core 任何文件。

## 它回答什么问题

`adaptive-agent-router` 是一个**库**：Core 交出的是**装配函数**（`build_entrypoint_handlers`）
与**驱动原语**（`PassDriver` / `LifecycleBindingGuard`），而**调用它们的那一层（Host）不在包内**。
本目录就是那一层的一个最小参考实现 —— 用来证明这个边界**是可闭合的**。

## 运行

```console
python host_demo/run_demo.py
```

⛔ 不需要先安装：demo 直接从仓库检出导入 `src/`（**这不代表安装已验证** —— 那是 `RelPkg` 的职责）。
产物：

```text
host_demo/output/demo_result.json   # 可复核报告：facts / 过程 / terminal / summary
host_demo/output/demo_run.log       # 运行日志
```

（`output/` 是**运行产物**，已在 `.gitignore` 中排除。）

## 两个演示

| | 证明什么 | 链路 |
|---|---|---|
| **Demo A** | **Host 边界正确** —— 架构能被启动 | `Host → build_entrypoint_handlers → dispatch → Router/Core → PassDriver` |
| **Demo B** | **系统能完成一次任务** | `dispatch → pass → 分支结算 → converge → close_run → terminal state` |

⚠️ 二者**必须分开**：Demo A 结束时 `terminal is None` 且 `control_flow_converged is False` ——
"跑了一个 pass" **不等于**"任务完成"。把装配问题与终态问题混在一起会难以定位。

## Host 的责任（实测确认的边界）

```text
host_wiring（Core 内）  只**装配**：不得调用 PassDriver / drive_next_pass / converge_run /
                        record_outcome / InMemoryAdapter（其 FORBIDDEN_CALLS 明令）
本目录（Host 侧）        **驱动**：构造 PassDriver、走 custody guard、结算、收口
```

Host 必须提供 **10 个 required injections** 并自己驱动：

```text
facts_source · capabilities · adapter · execution_context_facts_by_branch ·
confirmed_conclusions_by_branch · session_gate · control_facts_source ·
admission_facts_source · reusable_result_source · continuation_source   ← required（无默认值）
```

驱动顺序（`§279.3`）：

```text
guard = LifecycleBindingGuard()
driver = PassDriver(composition_root=…, routing=…)
guard.bind(driver) → guard.drive_once() → guard.release()
```

结算与收口（**只能经 Core 的冻结 seam**）：

```text
accept_completion(driver=…, branch_id=…, attempt=…, payload=…, ended_at=…, judgment_source=…)
    ⇒ Core 内部：build_branch_return → validate_branch → settle_pass/settle_failure
      ⇒ terminal_wiring::_settle ⇒ [10].record_outcome   （全仓唯一写点）
converge_run(manager=…, terminal_state=…, …)
    ⇒ [10].close_run                                      （全仓唯一收口点）
```

★ **一个真实的陷阱（`fixtures/judgment.py`）**：`ValidationJudgment.verdicts[].criterion`
**必须回填 Core 给出的权威完成标准**（即 `judgment_source(branch_return, criteria)` 的
`criteria`）。判定来源只能"承认标准并给证据"，⛔ 不能自造标准 —— 否则锚定校验不过。

## 已知边界（⛔ 不要读成能力声明）

1. **只用 `InMemoryAdapter`** —— ⛔ 不验证任何真实平台可达性。
2. **不是可安装入口** —— 无 console script；`RelPkg` 已冻结「无 console entry」。
3. **恰好一处私有触碰**：`PassDriver._graph`。因为 `converge_run(manager=…)` 需要一个
   `AgentManager`，而 `PassDriver` 的公开面只有 `__init__` / `drive_next_pass`
   （⛔ 无 manager accessor）。与 `completion_wiring` 同源使用 γ′ 字段。
   ⇒ 这说明 **Host 边界尚未完全闭合**，已登记为 `HD-G9`。
4. **单机验证** —— 仅在本机 Python 3.14 上跑过；未做多版本矩阵。

## 文件

```text
run_demo.py        单命令驱动两个 demo，写报告与日志
demo_host.py       Host 实现：装配 + 驱动 + 结算 + 收口（含 StableIdPlanner）
checks.py          断言集 HD-A1…A6 / HD-B1…B6（结构化判据，⛔ 无子串扫描）
fixtures/facts.py       场景事实（PolicyFacts / TaskFacts / RiskInputs / CapabilitySnapshot）
fixtures/conclusions.py 每 child 上下文事实 + 准入/控制/续跑源
fixtures/judgment.py    分支结果载荷 + judgment_source
output/            运行产物（已 gitignore）
```
