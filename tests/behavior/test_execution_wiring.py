"""``P1 ResultHandoff wiring`` —— 回程组装的锁 **L-P1-RH-01 – L-P1-RH-04** + 行为测试。

范围与签署依据：`tests/behavior/README_step5b.md` §66（Delta Scope，7 项全部签署）/ §67。
架构依据：**Amendment A15**（`[11] → [12]` 的结果交接边界）+ **A14.2**（身份链）。

⚠️ 本轮是 **frozen artifact extension**：只**新增** P1 的回程组装面，
**不改** `run_pass` 的签名与行为（下方有专门的回归守卫）。

⚠️ 本文件**可以** import `execution_results`（`[12]` 本体）—— 那是**测试**，
用来证明 `P1 → ResultHandoff → [12]` 的输入端**真的闭合**；生产侧 P1 **不 import** 它（L-P1-RH-04）。
"""

from __future__ import annotations

import pathlib

import pytest

from adaptive_router.contracts import (
    BRANCH_RETURN_FIELDS,
    Confidence,
    ConfirmationSource,
    ContractError,
    EffortLevel,
    ModelTier,
    new_branch_id,
)
from adaptive_router.execution import ExecutionCompositor
from adaptive_router.execution_results import build_branch_return
from adaptive_router.result_handoff import (
    ActualFacts,
    PlannedFacts,
    ProviderResultPayload,
    RequestedFacts,
    ResultHandoff,
    RuntimeResultPayload,
)

from _execution_locks import forbidden_import_hits, signature_shape
from _results_locks import (
    calls_to,
    function_call_keyword_values,
    referenced_names,
)
from test_adapter_fake import routing_plan
from test_execution import EXPECTED_RUN_PASS_KWONLY, Harness

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
EXECUTION_SOURCE = (SRC / "execution.py").read_text(encoding="utf-8")

#: L-P1-RH-04 —— 回程组装**不得**引入的 downstream 依赖。
FORBIDDEN_DOWNSTREAM = (
    "validation",
    "recovery",
    "cost",
    "ledger",
    "summary",
    "execution_results",
)


def make_payload(
    *,
    requested_model: str = "gpt-5-codex",
    actual_model: str | None = "gpt-5-codex-2025-xx",
    observed: bool = True,
    provider_status: str = "ok",
) -> RuntimeResultPayload:
    if not observed:
        actual_model = None  # `ActualFacts` 不变量：未观测不得携带数值
    return RuntimeResultPayload(
        requested=RequestedFacts(
            model_id=requested_model,
            effort=EffortLevel.MEDIUM,
            source=ConfirmationSource.REQUEST,
        ),
        actual=ActualFacts(
            model_id=actual_model,
            effort=EffortLevel.MEDIUM if observed else None,
            source=(
                ConfirmationSource.PROVIDER_API
                if observed
                else ConfirmationSource.UNAVAILABLE
            ),
            confidence=Confidence.HIGH,
            observed=observed,
        ),
        payload=ProviderResultPayload(
            provider_status=provider_status,
            conclusion_summary="done",
            key_evidence=("e1",),
            residual_risks=(),
            main_agent_todo=("t1",),
        ),
    )


def wired_harness() -> tuple[Harness, str]:
    """跑一次 pass 让两个 child 都进入 `RUNNING`，返回 (harness, 目标 branch)。"""
    harness = Harness()
    harness.pass_()
    return harness, harness.first


# ==========================================================================
# 静态锁 L-P1-RH-01 – L-P1-RH-04
# ==========================================================================


class TestLP1RH01SingleAssemblySite:
    def test_exactly_one_construction_site(self) -> None:
        sites = calls_to(EXECUTION_SOURCE, ("ResultHandoff",))
        assert len(sites) == 1

    def test_probe_second_site_goes_red(self) -> None:
        mutated = (
            EXECUTION_SOURCE
            + "\n\n\ndef _sneak(**kw):\n    return ResultHandoff(**kw)\n"
        )
        assert len(calls_to(mutated, ("ResultHandoff",))) == 2


class TestLP1RH02NoBypassingTheAdapter:
    def test_three_fields_come_from_the_payload(self) -> None:
        assert function_call_keyword_values(
            EXECUTION_SOURCE, "ResultHandoff", "requested_fact"
        ) == ("payload.requested",)
        assert function_call_keyword_values(
            EXECUTION_SOURCE, "ResultHandoff", "actual_fact"
        ) == ("payload.actual",)
        assert function_call_keyword_values(
            EXECUTION_SOURCE, "ResultHandoff", "provider_payload"
        ) == ("payload.payload",)

    def test_no_raw_provider_response_symbol(self) -> None:
        names = referenced_names(EXECUTION_SOURCE)
        for forbidden in ("raw_response", "raw_provider_response", "provider_response"):
            assert forbidden not in names, forbidden

    def test_probe_non_payload_source_goes_red(self) -> None:
        """**变异靶子**（§66.11 ⑥）：`provider_payload=` 取自一个非 payload 的变量。"""
        mutated = EXECUTION_SOURCE.replace(
            "            provider_payload=payload.payload,",
            "            provider_payload=provider_response,",
        )
        assert mutated != EXECUTION_SOURCE, "探针失效：替换未命中"
        assert function_call_keyword_values(
            mutated, "ResultHandoff", "provider_payload"
        ) != ("payload.payload",)


class TestLP1RH03IdentityIsConsistent:
    def test_branch_by_id_called_exactly_once(self) -> None:
        """静态面：身份只能来自 `[10]` 的既有记录（A14.2）。"""
        assert len(calls_to(EXECUTION_SOURCE, ("branch_by_id",))) == 1

    def test_plan_is_read_not_constructed(self) -> None:
        """`planned_fact` 是**投影**：读 `model_for_branch`，不构造任何计划对象。"""
        assert len(calls_to(EXECUTION_SOURCE, ("model_for_branch",))) == 1
        for symbol in ("RoutingPlan(", "Plan(", "ModelSelection("):
            assert symbol not in EXECUTION_SOURCE, symbol

    def test_probe_second_branch_by_id_goes_red(self) -> None:
        mutated = EXECUTION_SOURCE + (
            "\n\n\ndef _sneak(manager, branch_id):\n"
            "    return manager.routing_run.branch_by_id(branch_id)\n"
        )
        assert len(calls_to(mutated, ("branch_by_id",))) == 2


class TestLP1RH04NoDownstreamImports:
    def test_forbidden_downstream_imports_empty(self) -> None:
        assert forbidden_import_hits(EXECUTION_SOURCE, FORBIDDEN_DOWNSTREAM) == set()

    def test_it_does_not_import_the_results_transformer(self) -> None:
        """P1 只**产出** handoff，**不调用** `[12]`（§66.11 ⑤）。"""
        assert "execution_results" not in referenced_names(EXECUTION_SOURCE)
        assert "build_branch_return" not in referenced_names(EXECUTION_SOURCE)

    @pytest.mark.parametrize(
        "needle",
        [
            "\nfrom . import validation  # noqa\n",
            "\nfrom .cost import ledger  # noqa\n",
            "\nfrom .execution_results import build_branch_return  # noqa\n",
        ],
    )
    def test_probe_goes_red(self, needle: str) -> None:
        assert forbidden_import_hits(
            EXECUTION_SOURCE + needle, FORBIDDEN_DOWNSTREAM
        ) != set()


class TestRunPassIsUnchanged:
    """**回归守卫**：本轮是 extension，`run_pass` 的签名与行为必须逐字不变。"""

    def test_run_pass_signature_is_frozen(self) -> None:
        shape = signature_shape(EXECUTION_SOURCE, "ExecutionCompositor", "run_pass")
        assert shape == {
            "positional": ["self"],
            "kwonly": EXPECTED_RUN_PASS_KWONLY,
            "defaults": [False] * 6,
            "vararg": None,
            "kwarg": None,
        }

    def test_no_class_added(self) -> None:
        assert (
            len(calls_to(EXECUTION_SOURCE, ("ResultHandoff",))) == 1
        )  # 装配点仍在 ExecutionCompositor 内
        assert "class ExecutionCompositor" in EXECUTION_SOURCE
        assert "class ExecutionResultsBuilder" not in EXECUTION_SOURCE


# ==========================================================================
# 行为测试
# ==========================================================================


class TestAssembly:
    def test_success_path_fields(self) -> None:
        harness, branch_id = wired_harness()
        handoff = harness.compositor.assemble_result_handoff(
            routing=harness.routing,
            branch_id=branch_id,
            attempt=1,
            payload=make_payload(),
        )
        assert isinstance(handoff, ResultHandoff)
        assert handoff.branch_id == branch_id
        assert handoff.attempt == 1
        # agent_id 来自 [10] 的记录，**不是** Adapter 提供的
        record = harness.manager.routing_run.branch_by_id(branch_id)
        assert handoff.agent_id == record.agent_id
        assert handoff.agent_id.startswith("ag_")

    def test_planned_fact_is_the_plan_projection(self) -> None:
        harness, branch_id = wired_harness()
        handoff = harness.compositor.assemble_result_handoff(
            routing=harness.routing,
            branch_id=branch_id,
            attempt=1,
            payload=make_payload(),
        )
        selection = harness.routing.model_for_branch(branch_id)
        assert handoff.planned_fact.tier is selection.tier
        assert handoff.planned_fact.effort is selection.effort
        assert handoff.planned_fact.source is ConfirmationSource.PLANNER

    def test_requested_actual_payload_are_verbatim(self) -> None:
        harness, branch_id = wired_harness()
        payload = make_payload(provider_status="partial")
        handoff = harness.compositor.assemble_result_handoff(
            routing=harness.routing,
            branch_id=branch_id,
            attempt=1,
            payload=payload,
        )
        assert handoff.requested_fact is payload.requested
        assert handoff.actual_fact is payload.actual
        assert handoff.provider_payload is payload.payload

    def test_unobserved_result_is_carried_through(self) -> None:
        harness, branch_id = wired_harness()
        handoff = harness.compositor.assemble_result_handoff(
            routing=harness.routing,
            branch_id=branch_id,
            attempt=1,
            payload=make_payload(observed=False),
        )
        assert handoff.actual_fact.observed is False
        assert handoff.actual_fact.model_id is None
        assert handoff.actual_fact.source is ConfirmationSource.UNAVAILABLE


class TestIdentityFailures:
    def test_attempt_mismatch_fails_explicitly(self) -> None:
        """L-P1-RH-03 的行为面：不得静默绑定到另一个 agent。"""
        harness, branch_id = wired_harness()
        with pytest.raises(ContractError, match="身份必须一致"):
            harness.compositor.assemble_result_handoff(
                routing=harness.routing,
                branch_id=branch_id,
                attempt=2,
                payload=make_payload(),
            )

    def test_unknown_branch_fails(self) -> None:
        harness, _ = wired_harness()
        with pytest.raises(ContractError):
            harness.compositor.assemble_result_handoff(
                routing=harness.routing,
                branch_id=new_branch_id(),
                attempt=1,
                payload=make_payload(),
            )

    def test_branch_not_in_plan_fails(self) -> None:
        """`[10]` 有记录但 `RoutingPlan` 里没有该执行单位 ⇒ `model_for_branch` 显式失败。

        ⚠️ 不能用"删掉 `branch_models`"来构造：`RoutingPlan.__post_init__` 会先拒绝
        （每个 child 都必须有档位）。这里改用**合法的 `DIRECT` 计划**（0 child）。
        """
        harness, branch_id = wired_harness()
        direct_plan = routing_plan()  # 来自 test_adapter_fake：0 child 的 DIRECT
        with pytest.raises(ContractError, match="未知执行单位"):
            harness.compositor.assemble_result_handoff(
                routing=direct_plan,
                branch_id=branch_id,
                attempt=1,
                payload=make_payload(),
            )

    def test_wrong_payload_type_fails(self) -> None:
        harness, branch_id = wired_harness()
        with pytest.raises(ContractError, match="payload 必须是 RuntimeResultPayload"):
            harness.compositor.assemble_result_handoff(
                routing=harness.routing,
                branch_id=branch_id,
                attempt=1,
                payload="nope",  # type: ignore[arg-type]
            )


class TestInputEndIsClosed:
    """**端到端**：`P1 → ResultHandoff → [12]` 的输入端真的闭合了。"""

    def test_p1_handoff_feeds_the_results_transformer(self) -> None:
        harness, branch_id = wired_harness()
        handoff = harness.compositor.assemble_result_handoff(
            routing=harness.routing,
            branch_id=branch_id,
            attempt=1,
            payload=make_payload(),
        )
        branch_return = build_branch_return(handoff=handoff)
        assert tuple(branch_return.__dataclass_fields__) == BRANCH_RETURN_FIELDS
        assert branch_return.branch_id == branch_id
        assert branch_return.model.planned is ModelTier.BALANCED
        assert branch_return.model.requested == "gpt-5-codex"
        assert branch_return.model.actual == "gpt-5-codex-2025-xx"
        assert branch_return.validation_state == "ok"

    def test_two_branches_assemble_independently(self) -> None:
        harness, _ = wired_harness()
        first = harness.compositor.assemble_result_handoff(
            routing=harness.routing,
            branch_id=harness.first,
            attempt=1,
            payload=make_payload(requested_model="model-a"),
        )
        second = harness.compositor.assemble_result_handoff(
            routing=harness.routing,
            branch_id=harness.second,
            attempt=1,
            payload=make_payload(requested_model="model-b"),
        )
        assert first.branch_id != second.branch_id
        assert first.agent_id != second.agent_id
        assert first.requested_fact.model_id == "model-a"
        assert second.requested_fact.model_id == "model-b"

    def test_assembly_is_stateless(self) -> None:
        """同一入参调用两次 ⇒ 结果相等（不缓存任何跨调用信息）。"""
        harness, branch_id = wired_harness()
        payload = make_payload()
        first = harness.compositor.assemble_result_handoff(
            routing=harness.routing, branch_id=branch_id, attempt=1, payload=payload
        )
        second = harness.compositor.assemble_result_handoff(
            routing=harness.routing, branch_id=branch_id, attempt=1, payload=payload
        )
        assert first == second
