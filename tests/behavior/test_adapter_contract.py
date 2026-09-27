"""``[11] Runtime Adapter`` 的接缝契约 + 边界锁 L11-1 – L11-7（Step 5B.6.5.2-impl 第一批）。

架构依据：`docs/architecture.md` 2 节 ``[11] Runtime Adapter``（**Amendment A14**，FROZEN）。
范围、锁清单与 probe 计划：`tests/behavior/README_step5b.md` §50。

⚠️ 敏感性 probe 一律在**源码字符串**上完成，**不覆写** ``src/`` 里的真实文件。

⚠️ **本条纪律来自 `[10]` 轮的真实教训**：Core 侧**不得**自行解析 ``ImportFrom.module`` ——
一律走 `_adapter_locks.modules_importing`（内部复用共用检测器）。
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from adaptive_router.adapters import (
    AdapterError,
    AdapterFailure,
    AdapterFailureKind,
    AdapterStartReceipt,
)

from _adapter_locks import (
    clock_call_hits,
    dataclass_field_names,
    enum_member_names,
    forbidden_import_hits,
    imported_module_tokens,
    mapping_get_calls,
    modules_importing,
    open_calls,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src/adaptive_router"
ADAPTERS = SRC / "adapters"
ADAPTER_FILES = {
    path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
    for path in sorted(ADAPTERS.rglob("*.py"))
    if "__pycache__" not in str(path)
}
CONTRACT_SOURCE = ADAPTER_FILES["adapters/contract.py"]

#: L11-2 —— **唯一**允许 import `adapters` 的 Core 模块（A14.6 的"P1 单一入口"）。
ALLOWED_ADAPTER_IMPORTERS = frozenset({"execution.py"})

#: L11-3 —— `adapters/*` 只允许这些导入 token（自身子模块 + 标准库 typing/dataclasses/enum）。
ALLOWED_ADAPTER_IMPORT_TOKENS = frozenset(
    {"__future__", "annotations", "dataclasses", "dataclass", "enum", "Enum", "contract"}
)

#: L11-3 —— 禁用清单：任何 Core 决策 / 执行 / 成本节点都不得被适配层依赖。
FORBIDDEN_CORE_IMPORTS = (
    "planner",
    "policy",
    "model_router",
    "risk",
    "capabilities",
    "scheduler",
    "agent_manager",
    "execution",
    "router",
    "entrypoint",
    "session",
    "project_context",
    "context_builder",
    "cost",
    "admission_facts",
    "reusable_results",
    "runtime_control_facts",
    "agent_start_requests",
)


def _all_src_sources() -> dict[str, str]:
    return {
        path.relative_to(SRC).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(SRC.rglob("*.py"))
        if "__pycache__" not in str(path)
    }


# ==========================================================================
# DTO 形状与构造期校验（A14.3 / A14.4）
# ==========================================================================


class TestAdapterStartReceipt:
    def test_fields_are_exactly_two(self) -> None:
        """A14.3：**最小两字段，不得预置其它字段**。"""
        assert dataclass_field_names(CONTRACT_SOURCE, "AdapterStartReceipt") == (
            "handle",
            "started_at",
        )

    def test_holds_platform_facts(self) -> None:
        receipt = AdapterStartReceipt(handle="platform-42", started_at="2026-09-18T00:00:00Z")
        assert receipt.handle == "platform-42"
        assert receipt.started_at == "2026-09-18T00:00:00Z"

    def test_is_frozen(self) -> None:
        receipt = AdapterStartReceipt(handle="h", started_at="t")
        with pytest.raises(dataclasses.FrozenInstanceError):
            receipt.handle = "other"  # type: ignore[misc]

    @pytest.mark.parametrize("bad", ["", "   ", None, 1, True])
    def test_rejects_bad_handle(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            AdapterStartReceipt(handle=bad, started_at="t")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", ["", "   ", None, 1, True])
    def test_rejects_bad_started_at(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            AdapterStartReceipt(handle="h", started_at=bad)  # type: ignore[arg-type]

    def test_does_not_carry_agent_id(self) -> None:
        """A14.2：``handle`` ≠ ``agent_id`` —— 回执里**不得**出现 ``agent_id``。"""
        names = {field.name for field in dataclasses.fields(AdapterStartReceipt)}
        assert "agent_id" not in names
        assert "branch_id" not in names
        assert "state" not in names
        assert "conclusion" not in names


class TestAdapterFailureKind:
    def test_closed_set_of_four(self) -> None:
        """A14.4：V1 **闭集**，四个成员，不得增删。"""
        assert [member.name for member in AdapterFailureKind] == [
            "CRASH",
            "TIMEOUT",
            "REFUSED",
            "NETWORK",
        ]
        assert [member.value for member in AdapterFailureKind] == [
            "crash",
            "timeout",
            "refused",
            "network",
        ]

    def test_source_declares_exactly_four(self) -> None:
        assert enum_member_names(CONTRACT_SOURCE, "AdapterFailureKind") == (
            "CRASH",
            "TIMEOUT",
            "REFUSED",
            "NETWORK",
        )


class TestAdapterFailure:
    def test_fields_are_exactly_three(self) -> None:
        assert dataclass_field_names(CONTRACT_SOURCE, "AdapterFailure") == (
            "failure_kind",
            "message",
            "timestamp",
        )

    def test_carries_no_business_conclusion(self) -> None:
        """A14.4：不得携带业务结论，也不得携带重试 / 降级建议。"""
        names = {field.name for field in dataclasses.fields(AdapterFailure)}
        for forbidden in (
            "conclusion_summary",
            "validation_state",
            "branch_return",
            "retry",
            "retry_after",
            "should_retry",
            "downgrade_to",
            "alternatives",
            "agent_id",
        ):
            assert forbidden not in names, forbidden

    def test_accepts_every_kind(self) -> None:
        for kind in AdapterFailureKind:
            failure = AdapterFailure(failure_kind=kind, message="m", timestamp="t")
            assert failure.failure_kind is kind

    @pytest.mark.parametrize("bad", ["crash", None, 1, True])
    def test_rejects_non_kind(self, bad: object) -> None:
        with pytest.raises(TypeError):
            AdapterFailure(failure_kind=bad, message="m", timestamp="t")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", ["", "   ", None, 1])
    def test_rejects_bad_message(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            AdapterFailure(
                failure_kind=AdapterFailureKind.CRASH,
                message=bad,  # type: ignore[arg-type]
                timestamp="t",
            )

    @pytest.mark.parametrize("bad", ["", "   ", None, 1])
    def test_rejects_bad_timestamp(self, bad: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            AdapterFailure(
                failure_kind=AdapterFailureKind.CRASH,
                message="m",
                timestamp=bad,  # type: ignore[arg-type]
            )


class TestAdapterError:
    def test_is_a_runtime_error(self) -> None:
        assert issubclass(AdapterError, RuntimeError)

    def test_may_carry_a_classified_failure(self) -> None:
        failure = AdapterFailure(
            failure_kind=AdapterFailureKind.NETWORK, message="m", timestamp="t"
        )
        error = AdapterError("append failed", failure=failure)
        assert error.failure is failure
        assert str(error) == "append failed"

    def test_failure_defaults_to_none(self) -> None:
        assert AdapterError("unclassifiable").failure is None


# ==========================================================================
# L11-1 / L11-2 / L11-7 —— import 边界
# ==========================================================================


class TestL11_1_no_core_imports_adapters:
    def test_real_repository(self) -> None:
        offenders = [
            path
            for path in modules_importing(_all_src_sources(), "adapters")
            if path not in ALLOWED_ADAPTER_IMPORTERS
        ]
        assert offenders == [], f"Core 侧不得 import adapters：{offenders}"

    def test_the_detector_is_not_vacuous(self) -> None:
        """非平凡性：检测器必须真的能看见这条边（否则上面的断言是空地的）。"""
        synthetic = {
            "router.py": "from . import adapters\n",
            "policy.py": "import adaptive_router.adapters.contract\n",
            "scheduler.py": "from .adapters.contract import AdapterFailure\n",
        }
        assert modules_importing(synthetic, "adapters") == (
            "policy.py",
            "router.py",
            "scheduler.py",
        )


class TestL11_2_only_p1_may_import_adapters:
    def test_real_repository(self) -> None:
        """A14.6：P1 是执行半边**唯一** import / 调用 `[11]` 的组件。

        ⚠️ **状态迁移记录（不是回归）**：`execution.py` 落盘前这条断言是
        ``importers == ()``；落盘后**按设计**变为 ``("execution.py",)``。
        迁移前后的两个读数都留在 README §57。
        """
        importers = modules_importing(_all_src_sources(), "adapters")
        assert set(importers) <= ALLOWED_ADAPTER_IMPORTERS
        assert importers == ("execution.py",)

    def test_probe_execution_py_would_be_allowed(self) -> None:
        synthetic = {"execution.py": "from .adapters.contract import AdapterStartReceipt\n"}
        assert modules_importing(synthetic, "adapters") == ("execution.py",)
        assert set(modules_importing(synthetic, "adapters")) <= ALLOWED_ADAPTER_IMPORTERS


class TestL11_7_agent_manager_stays_clean:
    """**本轮关键锁**：保护已冻结的 `[10]`（N2）。"""

    def test_agent_manager_does_not_import_adapters(self) -> None:
        source = (SRC / "agent_manager.py").read_text(encoding="utf-8")
        assert forbidden_import_hits(source, ("adapters",)) == set()

    def test_agent_manager_never_names_adapter_types(self) -> None:
        """`[10]` 只收**显式标量**（`handle` / `started_at`），不得出现适配层类型名。"""
        source = (SRC / "agent_manager.py").read_text(encoding="utf-8")
        for symbol in ("AdapterStartReceipt", "AdapterFailure", "AdapterError", "handleFact"):
            assert symbol not in source, symbol

    def test_probe_would_go_red(self) -> None:
        source = (SRC / "agent_manager.py").read_text(encoding="utf-8")
        mutated = source + "\n\nfrom .adapters.contract import AdapterStartReceipt  # noqa\n"
        assert forbidden_import_hits(mutated, ("adapters",)) == {"adapters"}

    def test_the_unpacking_path_is_the_only_route(self) -> None:
        """N2 的可执行形式：receipt → **两个标量** → ``commit_started``。"""
        receipt = AdapterStartReceipt(handle="platform-7", started_at="2026-09-18T00:00:00Z")
        kwargs = {"handle": receipt.handle, "started_at": receipt.started_at}
        assert kwargs == {"handle": "platform-7", "started_at": "2026-09-18T00:00:00Z"}


# ==========================================================================
# L11-3 / L11-4 / L11-5 / L11-6 —— 适配层自身的锁
# ==========================================================================


class TestL11_3_adapter_import_allowlist:
    def test_every_adapter_file(self) -> None:
        for path, source in ADAPTER_FILES.items():
            tokens = set(imported_module_tokens(source))
            assert tokens <= ALLOWED_ADAPTER_IMPORT_TOKENS, (
                f"{path} 出现白名单外的 import：{tokens - ALLOWED_ADAPTER_IMPORT_TOKENS}"
            )

    def test_no_core_dependency(self) -> None:
        for path, source in ADAPTER_FILES.items():
            assert forbidden_import_hits(source, FORBIDDEN_CORE_IMPORTS) == set(), path

    def test_contract_module_imports_nothing_from_the_project(self) -> None:
        """`contract.py` 的三个 DTO 只由 `str` 与自身枚举构成 —— 项目内导入为 **0**。"""
        contract_only = {"adapters/contract.py": CONTRACT_SOURCE}
        assert modules_importing(contract_only, "adaptive_router") == ()
        assert imported_module_tokens(CONTRACT_SOURCE) <= {
            "__future__",
            "annotations",
            "dataclasses",
            "dataclass",
            "enum",
            "Enum",
        }

    @pytest.mark.parametrize(
        "needle",
        [
            "\nfrom .. import planner  # noqa\n",
            "\nfrom ..policy import EffectivePolicy  # noqa\n",
            "\nfrom ..scheduler import Scheduler  # noqa\n",
            "\nfrom ..agent_manager import AgentManager  # noqa\n",
            "\nfrom .. import cost  # noqa\n",
        ],
    )
    def test_probe_core_import_goes_red(self, needle: str) -> None:
        mutated = CONTRACT_SOURCE + needle
        assert forbidden_import_hits(mutated, FORBIDDEN_CORE_IMPORTS) != set()
        assert set(imported_module_tokens(mutated)) != set(
            imported_module_tokens(CONTRACT_SOURCE)
        )


class TestL11_4_adapter_never_builds_a_plan:
    def test_no_routing_plan_construction(self) -> None:
        """INV-03 的机械形式：适配层内 ``RoutingPlan(`` 构造点 = **0**。"""
        for path, source in ADAPTER_FILES.items():
            assert "RoutingPlan(" not in source, path

    def test_no_plan_construction(self) -> None:
        for path, source in ADAPTER_FILES.items():
            assert "Plan(" not in source, path

    def test_probe_goes_red(self) -> None:
        mutated = CONTRACT_SOURCE + "\n\n\ndef _sneak(plan):\n    return RoutingPlan(plan=plan)\n"
        assert "RoutingPlan(" in mutated


class TestL11_5_adapter_reads_no_clock:
    def test_no_clock_calls(self) -> None:
        for path, source in ADAPTER_FILES.items():
            assert clock_call_hits(source) == [], path

    @pytest.mark.parametrize(
        "needle", ["x = datetime.now()\n", "x = time.time()\n", "x = clock.monotonic()\n"]
    )
    def test_probe_goes_red(self, needle: str) -> None:
        assert clock_call_hits(CONTRACT_SOURCE + "\n\n" + needle) != []


class TestL11_6_no_silent_defaults:
    def test_no_mapping_get(self) -> None:
        for path, source in ADAPTER_FILES.items():
            assert mapping_get_calls(source) == [], path

    def test_no_file_io(self) -> None:
        for path, source in ADAPTER_FILES.items():
            assert open_calls(source) == [], path

    def test_probe_mapping_get_goes_red(self) -> None:
        assert mapping_get_calls(CONTRACT_SOURCE + "\n\nx = mapping.get('k', 'fallback')\n") == [
            CONTRACT_SOURCE.count("\n") + 3
        ]

    def test_probe_open_goes_red(self) -> None:
        assert open_calls(CONTRACT_SOURCE + '\n\nx = open("f")\n') != []


class TestL11_8_no_abstract_interface_layer:
    """裁定 ④：V1 **不引入** ``typing.Protocol`` / ``ABC`` 接口抽象层。

    ⚠️ **AST 级判定**：`contract.py` 的 docstring 里就有 ``Protocol`` 这个词，
    子串扫描会**假阳性**（与 `[10]` 的 L5 同类陷阱）。
    """

    def test_adapters_declare_no_protocol_or_abc_bases(self) -> None:
        for path, source in ADAPTER_FILES.items():
            bases = {
                ast.unparse(base)
                for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.ClassDef)
                for base in node.bases
            }
            assert not any("Protocol" in base or "ABC" in base for base in bases), (path, bases)

    def test_probe_protocol_base_goes_red(self) -> None:
        mutated = CONTRACT_SOURCE + "\n\n\nclass _Sneak(Protocol):\n    pass\n"
        bases = {
            ast.unparse(base)
            for node in ast.walk(ast.parse(mutated))
            if isinstance(node, ast.ClassDef)
            for base in node.bases
        }
        assert any("Protocol" in base for base in bases)
