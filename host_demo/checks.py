"""`HostDemo` 的**断言集** —— `HD-A1…A6`（Host 边界）+ `HD-B1…B6`（任务完成）。

判据一律结构化（AST / 只读投影），⛔ 不做子串扫描。见 `§278.4` / `§279.4`。
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass
from typing import Any

HOST_DEMO = pathlib.Path(__file__).resolve().parent

#: `§279.2`：Host 必须**自建**的 10 个 required injections。
REQUIRED_INJECTIONS = (
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
#: `HD-B6`：**允许**触碰的私有名 —— 每一项都要有登记过的理由。
ALLOWED_PRIVATE = {
    "_graph",      # γ′ 既定字段：`converge_run` 需要 manager，Core 无公开 accessor（`§281 HD-G9`）
    "_snapshot",   # B2 明确批准：预置能力快照缓存位（⛔ 不新增探测路径）
}
#: `HD-B6` 的显式 denylist：这些 Core 内部字段**任何情况下**都不许被 demo 读到。
FORBIDDEN_CORE_PRIVATE = {
    "_records", "_terminal", "_run_attempt", "_branch_order", "_total_attempt_limit",
    "_control_flow_converged", "_manager", "_router", "_composer", "_assembler",
    "_reusable_result_source", "_continuation_source", "_continuation_owner",
}


@dataclass
class Check:
    id: str
    ok: bool
    detail: str


def _tree(name: str) -> ast.Module:
    return ast.parse((HOST_DEMO / name).read_text(encoding="utf-8"))


def _source_files() -> list[pathlib.Path]:
    return sorted(p for p in HOST_DEMO.rglob("*.py") if "__pycache__" not in str(p))


def check_self_contained() -> Check:
    """`D4` / `HD-A1` 的一半：demo ⛔ 不 import `tests/*`，且 10 个 required 全部自建。"""
    offenders: list[str] = []
    for path in _source_files():
        for node in ast.walk(_tree(str(path.relative_to(HOST_DEMO)).replace("\\", "/"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("test_"):
                offenders.append(f"{path.name}: from {node.module}")
            if isinstance(node, ast.Import):
                offenders += [f"{path.name}: import {a.name}" for a in node.names if a.name.startswith("test_")]
    kwargs = {
        kw.arg
        for node in ast.walk(_tree("demo_host.py"))
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg
    }
    missing = [name for name in REQUIRED_INJECTIONS if name not in kwargs]
    ok = not offenders and not missing
    return Check("HD-A1", ok, f"tests 依赖 = {offenders or '无'} · 缺注入 = {missing or '无'}")


def check_no_private_poke() -> Check:
    """`HD-B6`：⛔ 不触碰 **Core** 私有字段。

    规则（⛔ 不是"任何下划线都违规"—— 那会误伤 demo 自己的状态，首版就是这样假红的）：
    每个 `X._y` 形态的**读取**必须属于以下之一：

      ① **dunder**（`__new__` 等语言机制，不是"私有状态"）
      ② 读的是 **demo 自己的**状态（`self._x`）
      ③ 已登记的允许项（见 `ALLOWED_PRIVATE`，每项都带理由）

    另对 `FORBIDDEN_CORE_PRIVATE` 做显式 denylist 二次确认，并**计数**允许项
    （让"唯一例外"可审计，而不是一句口号）。
    """
    illegal: set[str] = set()
    read_allowed: dict[str, int] = {}
    for path in _source_files():
        rel = str(path.relative_to(HOST_DEMO)).replace("\\", "/")
        for node in ast.walk(_tree(rel)):
            if not (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)):
                continue
            name = node.attr
            if name.startswith("__") and name.endswith("__"):
                continue  # ① dunder
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                continue  # ② demo 自己的状态
            if name in ALLOWED_PRIVATE:
                read_allowed[name] = read_allowed.get(name, 0) + 1  # ③ 已登记
                continue
            if name.startswith("_"):
                illegal.add(name)
    violated = sorted(illegal & FORBIDDEN_CORE_PRIVATE)
    ok = not illegal and not violated
    return Check(
        "HD-B6",
        ok,
        f"未登记私有读 = {sorted(illegal) or '无'} · denylist 命中 = {violated or '无'}"
        f" · 已登记依赖 = {read_allowed or '无'}",
    )


def demo_a(observation: Any, adapter: Any, expected_children: int, control_calls: int) -> list[Check]:
    run = observation.routing_run
    states = [record.state.value for record in run.branches]
    return [
        Check("HD-A2", observation.outcome is not None and observation.outcome.routing is not None
              and observation.outcome.plan is not None, "outcome.routing / outcome.plan 非 None"),
        # ⚠️ 计数 `on_execution` 调用**不足以**证明"恰好一个 pass"：回调只被装配层调一次，
        #    在其内部再驱动一个 pass 不会改变计数。⇒ 另加真检测器：`control_facts_source`
        #    每 pass **恰被调用一次**（既有不变量 `test_B17`）⇒ 两次驱动 → calls = 2。
        Check("HD-A3", observation.execution_calls == 1 and control_calls == 1,
              f"on_execution 调用 = {observation.execution_calls}（期望 1）· "
              f"control_facts_source 调用 = {control_calls}（期望 1 ⇒ 恰好一个 pass）"),
        Check("HD-A4", len(adapter.started) == expected_children and adapter.open_handle_count == expected_children,
              f"adapter.started = {len(adapter.started)} · open_handle = {adapter.open_handle_count}（期望 {expected_children}）"),
        Check("HD-A5", all(s == "running" for s in states), f"分支状态 = {states}"),
        Check("HD-A6", run.terminal is None and run.control_flow_converged is False,
              f"terminal = {run.terminal} · converged = {run.control_flow_converged}"
              "（⇒ Demo A 证「能启动」，不证「能完成」）"),
    ]


def demo_b(observation: Any, expected_children: int, terminal_state: str) -> list[Check]:
    run = observation.routing_run
    states = [record.state.value for record in run.branches]
    terminal = run.terminal
    convergence = observation.convergence or {}
    return [
        Check("HD-B1", len(observation.settlements) == expected_children
              and all(s["passed"] for s in observation.settlements),
              f"结算 = {observation.settlements}"),
        Check("HD-B2", terminal is not None and terminal.state.value == terminal_state,
              f"terminal.state = {terminal.state.value if terminal else None}（期望 {terminal_state}）"),
        Check("HD-B3", bool(run.is_converged) and bool(run.control_flow_converged),
              f"is_converged = {run.is_converged} · control_flow_converged = {run.control_flow_converged}"),
        Check("HD-B4", bool(convergence) and convergence.get("no_pending_approval") is True
              and convergence.get("no_pending_recovery") is True and all(s == "completed" for s in states),
              f"close_run 第二条件（4.5 条件②）= {convergence} · 分支终态 = {states}"),
    ]


def summarize(checks: list[Check]) -> dict[str, Any]:
    return {
        "passed": sum(1 for c in checks if c.ok),
        "failed": sum(1 for c in checks if not c.ok),
        "items": [{"id": c.id, "ok": c.ok, "detail": c.detail} for c in checks],
    }
