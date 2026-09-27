# Architecture Overview

> 本文是**对外架构总览**，不是内部架构宪法。内部治理记录不随本分发提供。

## 1. 系统目标

`adaptive-agent-router` 是一个**库形态**的自适应调度核心：把自然语言需求规格中的调度规则
落成代码，使部署侧能够按任务复杂度、风险与成本选择模型与推理强度。

三条设计约束支配全部实现：

1. **库而非应用** —— 不提供 console entry point，不拥有 deployment / session lifecycle。
2. **决策与执行分离** —— Core 只产出决定与操作请求；平台细节由 Adapter 承担。
3. **宿主提供环境** —— 事实、能力、适配器与门禁全部由 external Host 注入。

## 2. 模块关系

```text
Host（部署侧）
  │  注入 required runtime injections（当前签名含 10 个 required keyword-only inputs）
  ▼
entrypoint / host_wiring.build_entrypoint_handlers   ← 只做**组装**，⛔ 不驱动生命周期
  ▼
router · planner · policy · model_router · scheduler ← 决定：做什么、用哪个模型、按什么强度
  ▼
execution · pass_driver · lifecycle_binding_guard    ← 执行：把决定变成生命周期操作
  ▼
adapters（Protocol）· adapters/in_memory             ← 翻译：把操作交给具体平台
```

结算与收口是**独立的一条路径**（与执行路径不共享写点）：

```text
执行结果 → completion_wiring → validation_wiring → terminal_wiring（结算）
                                                    │
                                                    ▼
                                        converge_run → close_run（收口）
```

`agent_manager` 持有 run / branch / attempt 的权威记录；`recovery_*` 与
`resume_*` / `reentry_request` / `redecision_dispatch` 覆盖恢复、重入与重决策的边界。

## 3. 生命周期流程

```text
① 准入        START 只是**准入许可**（admission licence），⛔ 不立即写运行态
② 物化        Adapter 交出 handle 后，才由生命周期的**唯一写者**提交该 attempt 的运行态
③ 执行        宿主驱动的 pass 循环（bind → drive_once → release）
④ 结算        结果 → 判定 → 终态；失败与成功走同一结算入口
⑤ 收口        run 在无待决审批、无待恢复项时收敛为终态
⑥ 恢复/重入   受控的回边（recovery / resume / reentry / redecision），⛔ 不绕过 ④⑤
```

## 4. 运行时不变式

| # | 不变式 | 含义 |
|---|---|---|
| I1 | **结算写点唯一** | 全仓只有一个函数把分支结果写进 lifecycle 记录 |
| I2 | **收口点唯一** | 全仓只有一个函数把 run 收敛为终态 |
| I3 | **图读取点唯一** | 驱动图（`PassDriver._graph`）在全仓只被一个生产模块读取一次 |
| I4 | **attempt 单调且不可逆** | 同一分支的 attempt 只能递增；已终止分支不得再开新 attempt |
| I5 | **终态后无待决** | 收口后不得再出现待决审批 / 待恢复项 |
| I6 | **组装层不驱动** | 组装函数体内⛔ 不出现任何驱动 / 结算调用 |
| I7 | **零第三方运行时依赖** | `src/` 顶层 import 全部来自标准库 |

这些不变式由行为测试与结构锁共同固定；公开分发内可用测试套件直接验证。

## 5. 扩展点

| 扩展点 | 形态 | 说明 |
|---|---|---|
| Host injections | keyword-only 参数 | 事实源、能力、适配器、执行上下文、已确认结论、门禁、控制事实、准入事实、可复用结果、续接源 |
| Adapter | Protocol 实现 | 只需实现适配器契约；⛔ 不得做决策、⛔ 不得推断身份字段 |
| 事实来源 | 只读端口 | 成本、风险、上下文、可用能力等由宿主提供 |
| 判定来源 | 只读端口 | 验证判定由宿主提供；判定中的 criterion 必须回填权威 criteria |

## 6. 不在本分发内

规格资产（自然语言需求规格）与内部治理记录**单独维护**，不随本分发提供，
也不在 `LICENSE` 的覆盖范围内。
