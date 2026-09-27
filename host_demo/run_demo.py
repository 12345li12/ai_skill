"""`HostDemo` —— 单命令跑完 **Demo A**（装配 + 单 pass 驱动）与 **Demo B**（完整终态收敛链）。

```console
python host_demo/run_demo.py
```

产物：
    `host_demo/output/demo_result.json`  —— 可复核报告（facts / 过程 / terminal / summary）
    `host_demo/output/demo_run.log`      —— 运行日志

⛔ 本 demo **不是**安装入口、**不是** CLI 产品：`pip` 安装与 console script 均不在其内
（`RelPkg` 已冻结「无 console entry」）。它只证明**被冻结的 Core 可以被外部 Host 驱动**。
"""

from __future__ import annotations

import json
import pathlib
import sys
import traceback

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
# ⚠️ 让 demo 能从**仓库检出**直接运行（⛔ 不要求先安装；本 demo 不验证安装 —— 那是 RelPkg 的事）
for extra in (str(HERE), str(REPO / "src")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from adaptive_router.contracts import TerminalState  # noqa: E402

import checks  # noqa: E402
from demo_host import DemoHost  # noqa: E402
from fixtures.facts import BRANCH_IDS, RUN_ID, T0  # noqa: E402

OUTPUT = HERE / "output"
TASK = "把这份跨模块的工程任务拆给多个子代理并行完成"


class Tee:
    """同时写 stdout 与日志文件。"""

    def __init__(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("w", encoding="utf-8")

    def __call__(self, line: str = "") -> None:
        print(line)
        self._handle.write(line + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def run_demo_a(log: Tee) -> tuple[dict, list]:
    log("=" * 72)
    log("Demo A —— 最小装配 + 单 pass 驱动链")
    log("=" * 72)
    host = DemoHost(task=TASK)
    host.dispatch_task()
    log(f"  on_execution 调用 = {host.observation.execution_calls}")
    log(f"  adapter.started  = {len(host.adapter.started)} · open_handle = {host.adapter.open_handle_count}")
    log(f"  分支状态          = {host.branch_states()}")
    run = host.observation.routing_run
    log(f"  terminal          = {run.terminal} · converged = {run.control_flow_converged}")
    results = checks.demo_a(host.observation, host.adapter, len(BRANCH_IDS), host.control.calls)
    for item in results:
        log(f"  {'✅' if item.ok else '❌'}  {item.id}  {item.detail}")
    facts = {
        "task": TASK,
        "run_id": RUN_ID,
        "started_at": T0,
        "branch_ids": list(BRANCH_IDS),
        "execution_calls": host.observation.execution_calls,
    }
    return {"facts": facts, "summary": checks.summarize(results), "process": {
        "adapter_started": len(host.adapter.started),
        "branch_states": host.branch_states(),
        "terminal": None,
        "converged": bool(run.control_flow_converged),
    }}, results


def run_demo_b(log: Tee) -> tuple[dict, list]:
    log("")
    log("=" * 72)
    log("Demo B —— 完整闭环：pass → 分支结算 → 收口 → terminal state")
    log("=" * 72)
    host = DemoHost(task=TASK)
    host.dispatch_task()
    log(f"  ① pass 已驱动：分支状态 = {host.branch_states()}")
    for branch_id in BRANCH_IDS:
        host.settle_branch(branch_id, attempt=1)
    log(f"  ② 逐分支结算：{host.observation.settlements}")
    host.converge(TerminalState.SUCCESS)
    run = host.observation.routing_run
    log(f"  ③ 收口：terminal = {run.terminal.state.value if run.terminal else None}"
        f" · is_converged = {run.is_converged} · control_flow_converged = {run.control_flow_converged}")
    log(f"  ④ 分支终态 = {host.branch_states()}")
    results = checks.demo_b(host.observation, len(BRANCH_IDS), TerminalState.SUCCESS.value)
    for item in results:
        log(f"  {'✅' if item.ok else '❌'}  {item.id}  {item.detail}")
    facts = {
        "task": TASK,
        "run_id": RUN_ID,
        "started_at": T0,
        "branch_ids": list(BRANCH_IDS),
        "execution_calls": host.observation.execution_calls,
    }
    return {"facts": facts, "summary": checks.summarize(results), "process": {
        "settlements": host.observation.settlements,
        "branch_states": host.branch_states(),
        "terminal": run.terminal.state.value if run.terminal else None,
        "converged": bool(run.is_converged) and bool(run.control_flow_converged),
    }}, results


def main() -> int:
    log = Tee(OUTPUT / "demo_run.log")
    report: dict = {"demo": "HostDemo", "task": TASK}
    all_checks: list = []
    try:
        log("HostDemo —— 证明被冻结的 Core 可以被**外部 Host** 驱动")
        log("⛔ 不是安装入口 · ⛔ 不是 CLI 产品 · ⛔ 不修改 Core")
        log("")
        all_checks.append(checks.check_self_contained())
        all_checks.append(checks.check_no_private_poke())
        for item in all_checks:
            log(f"  {'✅' if item.ok else '❌'}  {item.id}  {item.detail}")
        log("")

        report["demo_a"], a_checks = run_demo_a(log)
        report["demo_b"], b_checks = run_demo_b(log)
        all_checks += a_checks + b_checks

        report["summary"] = checks.summarize(all_checks)
        report["limits"] = [
            "只验证 Host 驱动能力，⛔ 不验证真实平台可达性（H3：仅 InMemoryAdapter）",
            "⛔ 不验证安装（那是 RelPkg 的职责）· ⛔ 不是可安装入口",
            "恰好一处私有触碰：PassDriver._graph（γ′ 既定字段 · 因 Core 无公开 manager accessor）",
        ]
        OUTPUT.mkdir(parents=True, exist_ok=True)
        (OUTPUT / "demo_result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        log("")
        log(f"⇒ 断言 {report['summary']['passed']} 通过 / {report['summary']['failed']} 失败")
        log(f"⇒ 报告 = {OUTPUT / 'demo_result.json'}")
        return 0 if report["summary"]["failed"] == 0 else 1
    except BaseException:  # noqa: BLE001
        log("❌ 运行异常：")
        log(traceback.format_exc())
        return 2
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
