"""``redecision dispatch seam`` 结构锁 + 永久探针 + 行为（`LRD-1…9` / `MRD-*` / `B-1…B-4`）。

范围、体检与授权见 `tests/behavior/README_step5b.md` **§204** / **§205** / **§206** / **§207**
（含 **`CAPABILITY` 专用**、**其余四目标显式失败**、**新增异常类型**三条裁决）。

⚠️ **禁止子串扫描**：真实源码 docstring 里**故意提及** `PassDriver` / `drive_next_pass` /
`recheck` / `snapshot` 等词 ⇒ 判定一律 **AST 级 / 绑定集级**，或先剥 docstring 再看代码面。

⚠️ **两条核心证明（`§207.7` 的 DoD ⑥⑦）**
    · **唯一路径**：`CAPABILITY` ⇒ **恰一次** `recheck(ENVIRONMENT_SWITCHED)`，且返回的就是它的返回
    · **显式不可达**：其余**四个**目标逐一抛错，且**零** `recheck` 调用
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from adaptive_router.capabilities import (
    CapabilityProbe,
    CapabilityRegistry,
    EnvironmentReading,
    RecheckTrigger,
)
from adaptive_router.contracts import ContractError, RecoveryTarget
from adaptive_router.redecision_dispatch import (
    RedecisionDispatchError,
    dispatch_redecision,
)
from adaptive_router.recovery_route_wiring import (
    REDECISION_TARGETS,
    RedecisionRequest,
)

from _redecision_dispatch_locks import (  # noqa: F401  (复用既有检测器，不抄第二套)
    calls_to,
    clock_call_hits,
    forbidden_import_hits,
    function_return_annotation,
    imported_module_tokens,
    module_level_function_names,
    modules_importing,
    referenced_names,
)
from test_execution import capability  # noqa: F401  (复用既有假件)

SOURCE_PATH = pathlib.Path("src/adaptive_router/redecision_dispatch.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")

T0 = "2026-10-02T00:00:00Z"

#: `LRD-1` —— 模块级对象**恰 2**（异常 + 函数）。
EXPECTED_MODULE_OBJECTS = ["RedecisionDispatchError", "dispatch_redecision"]

#: `LRD-2` —— 签名：全 keyword-only · 恰 2 项。
EXPECTED_KWONLY = ["request", "capabilities"]

#: `LRD-6` —— 零编排：禁区调用必须全为 `[]`。
FORBIDDEN_CALLS = (
    "PassDriver",
    "drive_next_pass",
    "run_pass",
    "close_run",
    "converge_run",
    "record_outcome",
    "commit_started",
    "apply_runtime_control",
    "resume_from_waiting",
    "materialize",
    "RecheckTrigger",
)

#: `LRD-9` —— 不得构造任何决策载体。
FORBIDDEN_CONSTRUCTIONS = ("RecoveryDecision", "RecoveryOutcome", "RedecisionRequest")

#: `LRD-5` —— 不得自造 seam。
FORBIDDEN_SELF_MADE = ("CapabilityRegistry",)


# ======================================================================
# 测试侧局部 AST helper
# ======================================================================


def _code_only(source: str) -> str:
    """**剥掉 docstring 与注释**后的代码面（docstring 里故意提及禁用名 ⇒ 必须剥）。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
    return ast.unparse(tree)


def _func_node(source: str | None = None) -> ast.FunctionDef:
    for node in ast.parse(source or SOURCE).body:
        if isinstance(node, ast.FunctionDef) and node.name == "dispatch_redecision":
            return node
    raise AssertionError("未找到模块级函数 dispatch_redecision")


def _loop_nodes(source: str | None = None) -> list[str]:
    return [
        type(n).__name__
        for n in ast.walk(ast.parse(source or SOURCE))
        if isinstance(n, (ast.While, ast.For, ast.AsyncFor))
    ]


def _class_names(source: str | None = None) -> list[str]:
    return [n.name for n in ast.parse(source or SOURCE).body if isinstance(n, ast.ClassDef)]


# ======================================================================
# 测试夹具（真环境读数 + 真 probe）
# ======================================================================


class _LoaderSpy:
    """`CapabilityRegistry.loader` 的替身：记录调用并返回**真** `EnvironmentReading`。"""

    def __init__(self, limit: int = 3) -> None:
        self.limit = limit
        self.calls = 0

    def __call__(self, *args: object, **kwargs: object) -> EnvironmentReading:
        self.calls += 1
        return EnvironmentReading(
            subagents=True,
            async_background=True,
            concurrency_limit=self.limit,
            available_models=("provider-model-x",),
            available_effort_levels=(),
            state_wait_append_stop=True,
            hot_switch=False,
            reliable_cost_data=True,
            unavailable_reason=None,
        )


def _registry(*, limit: int = 3) -> tuple[CapabilityRegistry, _LoaderSpy]:
    loader = _LoaderSpy(limit=limit)
    return CapabilityRegistry(probe=CapabilityProbe(probed_at=T0), loader=loader), loader


def _request(target: RecoveryTarget) -> RedecisionRequest:
    return RedecisionRequest(
        branch_id="br_rd",
        attempt=1,
        target=target,
        minimal_correction="minimal",
        carry_forward=(),
    )


def _spy_recheck(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """计数 spy：记录**每一次** `recheck` 的 trigger（委派真实实现）。"""
    seen: list[object] = []
    original = CapabilityRegistry.recheck

    def spy(self: CapabilityRegistry, trigger: RecheckTrigger):
        seen.append(trigger)
        return original(self, trigger)

    monkeypatch.setattr(CapabilityRegistry, "recheck", spy)
    return seen


# ======================================================================
# `LRD-1 … LRD-9` —— 结构锁
# ======================================================================


class TestLRD1ModuleShape:
    """`LRD-1` —— 模块级对象**恰 2** · `__all__` 逐字 · **零 DTO**。"""

    def test_module_level_objects_are_exactly_two(self) -> None:
        import adaptive_router.redecision_dispatch as module

        declared = sorted(
            name
            for name, value in vars(module).items()
            if not name.startswith("__") and getattr(value, "__module__", None) == module.__name__
        )
        assert declared == EXPECTED_MODULE_OBJECTS

    def test_public_surface(self) -> None:
        import adaptive_router.redecision_dispatch as module

        assert module.__all__ == EXPECTED_MODULE_OBJECTS

    def test_only_one_class_and_it_is_the_error(self) -> None:
        assert _class_names() == ["RedecisionDispatchError"]

    def test_no_module_level_functions_besides_the_entry(self) -> None:
        assert module_level_function_names(SOURCE) == ("dispatch_redecision",)

    def test_error_is_runtime_error_not_contract_error(self) -> None:
        assert issubclass(RedecisionDispatchError, RuntimeError)
        assert not issubclass(RedecisionDispatchError, ContractError)

    def test_probe_third_module_object_goes_red(self) -> None:
        mutated = SOURCE + "\n\nclass _Sneak:\n    pass\n"
        assert _class_names(mutated) == ["RedecisionDispatchError", "_Sneak"]


class TestLRD2Signature:
    """`LRD-2` —— 全 keyword-only · 恰 2 项 · 返回既有 `CapabilitySnapshot`。"""

    def test_all_parameters_are_keyword_only(self) -> None:
        node = _func_node()
        assert node.args.posonlyargs == []
        assert node.args.args == []
        assert node.args.vararg is None
        assert node.args.kwarg is None

    def test_exactly_two_kwonly_names(self) -> None:
        assert [a.arg for a in _func_node().args.kwonlyargs] == EXPECTED_KWONLY
        assert _func_node().args.kw_defaults == [None, None]

    def test_return_annotation_is_the_existing_snapshot_type(self) -> None:
        assert function_return_annotation(SOURCE, "dispatch_redecision") == "CapabilitySnapshot"

    def test_probe_third_parameter_goes_red(self) -> None:
        """⚠️ 锚点取**整个签名块**（含 `) -> CapabilitySnapshot:`）以保证唯一 ——
        ⛔ 不能用短串 `replace(..., 1)`：本模块 docstring 里也提到了参数名（同一坑，见 `LRD-4`）。"""
        anchor = (
            "    request: RedecisionRequest,\n"
            "    capabilities: CapabilityRegistry,\n"
            ") -> CapabilitySnapshot:\n"
        )
        assert SOURCE.count(anchor) == 1, f"锚点命中 {SOURCE.count(anchor)} 次（应为 1）"
        mutated = SOURCE.replace(
            anchor,
            "    request: RedecisionRequest,\n"
            "    capabilities: CapabilityRegistry,\n"
            "    extra: int = 0,\n"
            ") -> CapabilitySnapshot:\n",
        )
        assert mutated != SOURCE
        assert [a.arg for a in _func_node(mutated).args.kwonlyargs] != EXPECTED_KWONLY


class TestLRD3ExplicitFailureIsComplete:
    """`LRD-3` —— `RecoveryTarget` **五值全覆盖**：`CAPABILITY` 走重查，其余四值抛错。"""

    def test_recovery_target_has_exactly_five_members(self) -> None:
        assert [m.name for m in RecoveryTarget] == [
            "CAPABILITY",
            "PLANNER",
            "MODEL_ROUTER",
            "CONTEXT",
            "SCHEDULER",
        ]

    def test_reachable_targets_are_exactly_three(self) -> None:
        assert [t.name for t in REDECISION_TARGETS] == ["PLANNER", "MODEL_ROUTER", "CAPABILITY"]

    @pytest.mark.parametrize(
        "target",
        [
            RecoveryTarget.PLANNER,
            RecoveryTarget.MODEL_ROUTER,
            RecoveryTarget.CONTEXT,
            RecoveryTarget.SCHEDULER,
        ],
    )
    def test_non_capability_targets_fail_explicitly(
        self, target: RecoveryTarget, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = _spy_recheck(monkeypatch)
        registry, _loader = _registry()
        with pytest.raises(RedecisionDispatchError):
            dispatch_redecision(request=_request(target), capabilities=registry)
        assert seen == []  # 零 recheck 调用

    def test_no_silent_success_branch_exists(self) -> None:
        """⛔ 不允许 `else: ignore` —— 代码面必须**没有**落回 `None` 的分支。"""
        code = _code_only(SOURCE)
        assert "return None" not in code
        assert "pass" not in code.split("def dispatch_redecision")[1]


class TestLRD4UniqueRecheckPath:
    """`LRD-4` —— ⚠️ **唯一路径**：`recheck` 恰 1 处，实参逐字 `ENVIRONMENT_SWITCHED`。"""

    def test_recheck_called_exactly_once_in_module(self) -> None:
        assert len(calls_to(SOURCE, {"recheck"})) == 1

    def test_recheck_argument_is_the_environment_switched_trigger(self) -> None:
        node = _func_node()
        call = next(
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("recheck")
        )
        assert [ast.unparse(a) for a in call.args] == ["RecheckTrigger.ENVIRONMENT_SWITCHED"]
        assert call.keywords == []

    def test_trigger_is_not_a_variable_or_a_parameter(self) -> None:
        """触发器是**冻结常量**，不是可变输入（否则就成了"自行判断 trigger"）。"""
        node = _func_node()
        names = {a.arg for a in node.args.kwonlyargs} | {
            n.id for n in ast.walk(node) if isinstance(n, ast.Name)
        }
        assert "trigger" not in names

    def test_probe_wrong_trigger_goes_red(self) -> None:
        """⚠️ 锚点必须**唯一且落在代码本体** —— ⛔ 不能用 `replace(..., 1)`：
        本模块 docstring 里**刻意提及**了同一个串，`replace(..., 1)` 会改到 docstring 那处、
        代码不变 ⇒ 读数**假绿**（本测试第一版就踩了这个坑）。
        """
        anchor = "        return capabilities.recheck(RecheckTrigger.ENVIRONMENT_SWITCHED)\n"
        assert SOURCE.count(anchor) == 1, f"锚点命中 {SOURCE.count(anchor)} 次（应为 1）"
        mutated = SOURCE.replace(anchor, anchor.replace("ENVIRONMENT_SWITCHED", "SESSION_RESUMED"))
        assert mutated != SOURCE
        node = _func_node(mutated)
        call = next(
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and ast.unparse(n.func).endswith("recheck")
        )
        assert [ast.unparse(a) for a in call.args] != ["RecheckTrigger.ENVIRONMENT_SWITCHED"]

    def test_probe_snapshot_instead_of_recheck_goes_red(self) -> None:
        anchor = "        return capabilities.recheck(RecheckTrigger.ENVIRONMENT_SWITCHED)\n"
        assert SOURCE.count(anchor) == 1
        mutated = SOURCE.replace(anchor, anchor.replace(".recheck(", ".snapshot("))
        assert mutated != SOURCE
        assert calls_to(mutated, {"recheck"}) == []
        assert calls_to(mutated, {"snapshot"}) != []


class TestLRD5NoSelfMadeSeam:
    """`LRD-5` —— **不自造 seam**：不构造 registry、不接受 `CompositionRoot`。"""

    def test_no_registry_construction(self) -> None:
        for name in FORBIDDEN_SELF_MADE:
            assert calls_to(SOURCE, {name}) == []

    def test_does_not_accept_composition_root(self) -> None:
        assert "CompositionRoot" not in imported_module_tokens(SOURCE)
        assert "CompositionRoot" not in _code_only(SOURCE)

    def test_import_surface_is_exactly_the_frozen_carriers(self) -> None:
        """导入面**精确相等**：三个既有载体 + 标准库（`TYPE_CHECKING` 块仅供静态检查）。"""
        assert imported_module_tokens(SOURCE) == {
            "TYPE_CHECKING",
            "__future__",
            "annotations",
            "capabilities",
            "contracts",
            "recovery_route_wiring",
            "typing",
        }

    def test_probe_self_made_registry_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    target = request.target\n",
            "    _r = CapabilityRegistry(probe=None, loader=None)\n    target = request.target\n",
            1,
        )
        assert mutated != SOURCE
        assert calls_to(mutated, {"CapabilityRegistry"}) != []


class TestLRD6ZeroOrchestration:
    """`LRD-6` —— **零编排**：不驱动 pass、不写运行态、不自造 trigger。"""

    def test_zero_forbidden_calls(self) -> None:
        hits = {name: calls_to(SOURCE, {name}) for name in FORBIDDEN_CALLS}
        assert {k: v for k, v in hits.items() if v} == {}

    def test_no_drive_symbol_in_the_code_surface(self) -> None:
        code = _code_only(SOURCE)
        assert "PassDriver" not in code
        assert "drive_next_pass" not in code
        assert "RecheckTrigger(" not in code  # trigger 只作常量引用，不得"构造"

    def test_probe_driving_a_pass_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    target = request.target\n",
            "    from .pass_driver import PassDriver\n"
            "    PassDriver(composition_root=None, routing=None).drive_next_pass()\n"
            "    target = request.target\n",
            1,
        )
        assert mutated != SOURCE
        assert calls_to(mutated, {"PassDriver"}) != []
        assert calls_to(mutated, {"drive_next_pass"}) != []


class TestLRD7NoTerminalDecision:
    """`LRD-7` —— **零终态**。"""

    def test_terminal_names_are_not_referenced(self) -> None:
        names = referenced_names(SOURCE)
        for forbidden in ("TerminalState", "RunTerminal", "RoutingRun", "is_converged"):
            assert forbidden not in names

    def test_probe_terminal_state_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    target = request.target\n",
            "    _t = TerminalState.COMPLETED\n    target = request.target\n",
            1,
        )
        assert mutated != SOURCE
        assert "TerminalState" in referenced_names(mutated)


class TestLRD8NoLoopNoClock:
    """`LRD-8` —— 零循环 / 零时钟。"""

    def test_zero_loop_nodes(self) -> None:
        assert _loop_nodes() == []

    def test_zero_clock_calls(self) -> None:
        assert clock_call_hits(SOURCE) == []

    def test_probe_while_loop_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    target = request.target\n",
            "    while True:\n        break\n    target = request.target\n",
            1,
        )
        assert mutated != SOURCE
        assert _loop_nodes(mutated) != []


class TestLRD9NoDecisionCarrierConstruction:
    """`LRD-9` —— **不构造决策载体** · **不 import `recovery.py`**（`LRT-3` 同型纪律）。"""

    def test_zero_decision_carrier_constructions(self) -> None:
        for name in FORBIDDEN_CONSTRUCTIONS:
            assert calls_to(SOURCE, {name}) == []

    def test_recovery_module_is_not_imported(self) -> None:
        assert "recovery" not in imported_module_tokens(SOURCE)
        assert forbidden_import_hits(SOURCE, ("recovery",)) == set()

    def test_the_only_redecision_constructor_is_still_the_c2a_one(self) -> None:
        """跨代纪律：`LRT-1` 的"唯一构造点"**未被本轮破坏**。"""
        src_root = pathlib.Path("src/adaptive_router")
        sources = {
            p.relative_to(src_root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(src_root.rglob("*.py"))
            if "__pycache__" not in p.parts
        }
        sites = {
            rel: len(calls_to(text, {"RedecisionRequest"}))
            for rel, text in sources.items()
            if calls_to(text, {"RedecisionRequest"})
        }
        assert sites == {"recovery_route_wiring.py": 1}

    def test_probe_constructing_a_request_goes_red(self) -> None:
        mutated = SOURCE.replace(
            "    target = request.target\n",
            "    _r = RedecisionRequest(branch_id='b', attempt=1, target=None,\n"
            "                          minimal_correction='m', carry_forward=())\n"
            "    target = request.target\n",
            1,
        )
        assert mutated != SOURCE
        assert calls_to(mutated, {"RedecisionRequest"}) != []


# ======================================================================
# 行为 `B-1 … B-4`
# ======================================================================


class TestBehaviorRedecisionDispatch:
    def test_B1_capability_path_calls_recheck_once_with_the_right_trigger(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ **DoD ⑥ 唯一路径证明**：恰一次 · 正确 trigger · 返回即它的返回。"""
        seen = _spy_recheck(monkeypatch)
        registry, loader = _registry(limit=5)

        out = dispatch_redecision(
            request=_request(RecoveryTarget.CAPABILITY), capabilities=registry
        )

        assert seen == [RecheckTrigger.ENVIRONMENT_SWITCHED]  # 恰一次 · 逐字
        assert loader.calls >= 1  # 真重查发生了（不是只读缓存）
        assert registry.last_trigger is RecheckTrigger.ENVIRONMENT_SWITCHED
        assert out is registry._snapshot  # noqa: SLF001  (同一性：原样返回，未包装)
        assert out.concurrency_limit == 5  # 反映**新**读数

    def test_B2_every_other_target_fails_and_never_rechecks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ **DoD ⑦ 显式不可达证明**：四个目标逐一抛错且零 `recheck`。"""
        seen = _spy_recheck(monkeypatch)
        registry, loader = _registry()
        failed: list[str] = []

        for target in RecoveryTarget:
            if target is RecoveryTarget.CAPABILITY:
                continue
            with pytest.raises(RedecisionDispatchError):
                dispatch_redecision(request=_request(target), capabilities=registry)
            failed.append(target.name)

        assert failed == ["PLANNER", "MODEL_ROUTER", "CONTEXT", "SCHEDULER"]
        assert seen == []
        assert loader.calls == 0
        assert registry.last_trigger is None

    def test_B3_input_guards_fail_explicitly(self) -> None:
        registry, _loader = _registry()
        good = _request(RecoveryTarget.CAPABILITY)

        with pytest.raises(RedecisionDispatchError):
            dispatch_redecision(request=object(), capabilities=registry)  # type: ignore[arg-type]
        with pytest.raises(RedecisionDispatchError):
            dispatch_redecision(request=good, capabilities=None)  # type: ignore[arg-type]
        with pytest.raises(RedecisionDispatchError):
            dispatch_redecision(request=good, capabilities=object())  # type: ignore[arg-type]

    def test_B4_does_not_mutate_the_request(self) -> None:
        """只读消费：请求本身与它的字段**不被改写**。"""
        registry, _loader = _registry()
        request = _request(RecoveryTarget.CAPABILITY)
        before = (request.branch_id, request.attempt, request.target,
                  request.minimal_correction, request.carry_forward)

        dispatch_redecision(request=request, capabilities=registry)

        assert (request.branch_id, request.attempt, request.target,
                request.minimal_correction, request.carry_forward) == before

    def test_B5_the_same_registry_instance_is_the_one_rechecked(self) -> None:
        """⚠️ **注入一致性**：本模块重查的**就是**调用方注入的那个实例。

        这是 `§181.4.3` 冻结语义（驱动者每轮从**同一个 root** 读 `snapshot()`）在 seam 侧的对应物：
        若本模块自己造一个 registry，重查结果就**不会**被后续 pass 读到。
        """
        injected, _loader = _registry(limit=7)
        out = dispatch_redecision(
            request=_request(RecoveryTarget.CAPABILITY), capabilities=injected
        )
        assert out is injected._snapshot  # noqa: SLF001
        assert injected.last_trigger is RecheckTrigger.ENVIRONMENT_SWITCHED


# ======================================================================
# 探针卫生
# ======================================================================


class TestProbeHygiene:
    def test_every_declared_anchor_hits(self) -> None:
        anchors = [
            "class RedecisionDispatchError(RuntimeError):",
            "def dispatch_redecision(",
            "    request: RedecisionRequest,",
            "    capabilities: CapabilityRegistry,",
            "        return capabilities.recheck(RecheckTrigger.ENVIRONMENT_SWITCHED)",
            "    raise RedecisionDispatchError(",
            "from .recovery_route_wiring import RedecisionRequest",
        ]
        for anchor in anchors:
            assert anchor in SOURCE, f"锚点未命中：{anchor!r}"

    def test_source_is_parseable_without_bom(self) -> None:
        assert SOURCE_PATH.read_bytes()[:3] != b"\xef\xbb\xbf"
        ast.parse(SOURCE)

    def test_the_only_production_importer_is_the_recovery_callback(self) -> None:
        """⚠️ **过渡性可达性锁**（`§209.7` 的正式冻结注记）—— **本轮发生语义变更**。

        历史（`§208`/`§209` 冻结时）：断言 **`src/` 内没有任何模块 import 本 seam** ⇒
        证明"**尚无生产调用者**"。

        现在（`5B.6.44.1-impl`）：`execution_recovery_callback` **就是**它的生产调用者
        （已单独授权：`§213` 申请 → `§214` 实现）。
        按 `§209.7` 的冻结口径 —— "未来若单独授权运行期调用者，**这条测试本来就应该变红并随那一轮
        授权更新**，**不能被解释成'生产代码永远不得 import 此 seam'**" ——
        ⇒ 本轮**显式更新**该断言为"**唯一**生产导入者是那个 callback"（**非静默改测试**）：
          · 它**仍然是**有意义的约束：除该 callback 外**不得**再有别的生产调用者
          · 若将来出现**第二个**导入者 ⇒ 本测试**仍会变红**
        """
        src_root = pathlib.Path("src/adaptive_router")
        sources = {
            p.relative_to(src_root).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(src_root.rglob("*.py"))
            if "__pycache__" not in p.parts and p.name != "redecision_dispatch.py"
        }
        assert modules_importing(sources, "redecision_dispatch") == (
            "execution_recovery_callback.py",
        )

    def test_the_importer_detector_is_not_vacuous(self) -> None:
        """非平凡性：检测器仍能看见新出现的导入边（否则上面的断言是**空地**）。"""
        assert modules_importing(
            {"some_new_module.py": "from .redecision_dispatch import dispatch_redecision\n"},
            "redecision_dispatch",
        ) == ("some_new_module.py",)

    def test_no_return_none_anywhere(self) -> None:
        assert "return None" not in _code_only(SOURCE)
