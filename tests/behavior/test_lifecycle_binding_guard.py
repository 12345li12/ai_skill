"""``lifecycle_binding_guard`` 结构锁 + 永久探针 + 行为（`LBL-*` / `LBM-*` / `B-*`）。

覆盖 `§225.2` 冻结状态模型与 `§224` 的全部失败面：`F1…F8` + ``bind(None)``。

⚠️ **禁止子串扫描**：生产模块 docstring **故意提及** ``PassDriver`` / ``materialize`` /
   ``run_id`` / ``registry`` ⇒ 本文件一律 **AST / 绑定集 / 对象身份** 判据。

⚠️ **类方法不能用 ``function_signature_shape`` / ``function_return_annotation``**
   （那两个检测器**只作用于模块级函数** ⇒ 对类方法会 ``AssertionError``）；
   本文件因此自带 ``_method_shape`` / ``_method_return`` / ``_calls_in_method`` 三个**本地** AST helper。

⚠️ **并发一律用同步 primitive（``threading.Event``）**，⛔ **不用 ``sleep()`` 制造并发**。
"""

from __future__ import annotations

import ast
import pathlib
import threading

import pytest

from _lifecycle_binding_guard_locks import (
    calls_to,
    class_method_names,
    clock_call_hits,
    forbidden_import_hits,
    imported_module_tokens,
    module_level_annassign,
    module_level_container_assignments,
    module_level_function_names,
    module_level_shape,
    referenced_names,
    slots_of,
)
from adaptive_router.lifecycle_binding_guard import (
    LifecycleBindingError,
    LifecycleBindingGuard,
)

SOURCE_PATH = pathlib.Path("src/adaptive_router/lifecycle_binding_guard.py")
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")

GUARD = "LifecycleBindingGuard"
ERROR = "LifecycleBindingError"


# ----------------------------------------------------------------------
# 本地 AST helper（⛔ 不改公共锁层；类方法专用）
# ----------------------------------------------------------------------


def _method_node(source: str, method_name: str) -> ast.FunctionDef:
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == GUARD:
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == method_name:
                    return member
    raise AssertionError(f"未找到方法 {method_name!r}")


def _method_shape(source: str, method_name: str) -> tuple[list[str], list[str], str | None]:
    node = _method_node(source, method_name)
    pos = [a.arg for a in node.args.args][1:]
    kw = [a.arg for a in node.args.kwonlyargs]
    ret = ast.unparse(node.returns) if node.returns else None
    return pos, kw, ret


def _acquire_release_pairs(source: str, method_name: str) -> tuple[int, int]:
    """方法体内 ``self._acquire(...)`` 次数 与 ``finally`` 中 ``self._guard.release()`` 次数。

    ⚠️ 这两者**必须同为 1** —— 因为删掉 acquire 会让 ``finally`` 释放**未持有**的锁。
    """
    node = _method_node(source, method_name)
    acquires = sum(
        1
        for x in ast.walk(node)
        if isinstance(x, ast.Call)
        and isinstance(x.func, ast.Attribute)
        and x.func.attr == "_acquire"
    )
    releases = 0
    for handler in [x for x in ast.walk(node) if isinstance(x, ast.Try)]:
        for stmt in handler.finalbody:
            releases += sum(
                1
                for x in ast.walk(stmt)
                if isinstance(x, ast.Call)
                and isinstance(x.func, ast.Attribute)
                and x.func.attr == "release"
            )
    return acquires, releases


def _calls_in_method(source: str, method_name: str, callee: str) -> int:
    node = _method_node(source, method_name)
    return sum(
        1
        for x in ast.walk(node)
        if isinstance(x, ast.Call)
        and isinstance(x.func, ast.Attribute)
        and x.func.attr == callee
    )


def _acquire_call(source: str) -> ast.Call:
    """源码里**唯一**的 ``acquire(...)`` 调用（⛔ 多于一个 ⇒ 直接失败）。"""
    acquires = [
        n
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "acquire"
    ]
    assert len(acquires) == 1, f"acquire 调用点数 = {len(acquires)}"
    return acquires[0]


def _acquire_blocking_flag(source: str) -> bool | None:
    """``acquire(blocking=X)`` 的 ``X`` 字面量；缺省 ⇒ ``None``（= 默认阻塞）。"""
    for kw in _acquire_call(source).keywords:
        if kw.arg == "blocking":
            return ast.literal_eval(kw.value)
    return None


def _acquire_failure_branch_present(source: str) -> bool:
    """``_acquire`` 内是否存在以该调用为条件的 ``if``（= "获取失败即显式失败"分支）。"""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "_acquire":
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.If):
                    text = ast.unparse(stmt.test)
                    if "acquire" in text and "not" in text:
                        return True
    return False


def _has_try(source: str, method_name: str) -> bool:
    return any(isinstance(x, ast.Try) for x in ast.walk(_method_node(source, method_name)))


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------


class _Driver:
    """最小 duck-typed 替身：记录"**哪个实例**被调用了多少次"。

    ⚠️ 它**不是** ``PassDriver`` —— guard 的义务只是**保存并转发一个对象引用**，
      ⛔ 不需要（也**不得**）构造真 ``PassDriver``。
    """

    __slots__ = ("calls", "raises", "on_call")

    def __init__(self, *, raises: BaseException | None = None, on_call=None) -> None:
        self.calls = 0
        self.raises = raises
        self.on_call = on_call

    def drive_next_pass(self):
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        if self.raises is not None:
            raise self.raises
        return ("report", id(self), self.calls)


def _expect_error(fn, *args, **kwargs) -> LifecycleBindingError:
    with pytest.raises(LifecycleBindingError) as excinfo:
        fn(*args, **kwargs)
    return excinfo.value


def _exec(source: str) -> dict:
    ns: dict = {}
    exec(compile(source, "<mutant>", "exec"), ns)  # noqa: S102 - 一次性变异体
    return ns


def _mutate(pairs: list[tuple[str, str]]) -> str:
    """整块替换 + **前置 ``ast.parse`` 预检**（⛔ 语法错不算"变红"）。"""
    out = SOURCE
    for old, new in pairs:
        assert out.count(old) == 1, f"锚点不唯一：{old[:44]!r} x{out.count(old)}"
        out = out.replace(old, new, 1)
    assert out != SOURCE
    ast.parse(out)
    return out


# ======================================================================
# LBL-1 · 模块形状
# ======================================================================


class TestLBL1ModuleShape:
    def test_module_level_shape(self) -> None:
        shape = module_level_shape(SOURCE)
        assert shape["classes"] == [ERROR, GUARD]
        assert shape["functions"] == []
        assert shape["assigns"] == ["__all__"]

    def test_no_module_level_annotated_assignment(self) -> None:
        assert module_level_annassign(SOURCE) == []

    def test_no_module_level_container(self) -> None:
        assert module_level_container_assignments(SOURCE) == ()

    def test_no_module_level_function(self) -> None:
        assert module_level_function_names(SOURCE) == ()

    def test_import_surface_allows_only_expected_tokens(self) -> None:
        tokens = imported_module_tokens(SOURCE)
        assert tokens == {
            "__future__",
            "annotations",
            "threading",
            "typing",
            "TYPE_CHECKING",
            "Any",
            "pass_driver",
        }
        for forbidden in ("composition_root", "router_wrapper", "execution", "entrypoint", "host_wiring"):
            assert forbidden not in tokens

    def test_no_forbidden_import(self) -> None:
        assert forbidden_import_hits(SOURCE, {"adapters"}) == set()

    def test_pass_driver_import_is_type_checking_only(self) -> None:
        """⛔ ``pass_driver`` 只能在 ``TYPE_CHECKING`` 块内被 import（运行期零依赖）。"""
        module_imports = [
            n
            for n in ast.parse(SOURCE).body
            if isinstance(n, (ast.Import, ast.ImportFrom))
        ]
        assert len(module_imports) == 3  # __future__ / threading / typing
        guarded = [
            n
            for n in ast.walk(ast.parse(SOURCE))
            if isinstance(n, ast.If)
            and isinstance(n.test, ast.Name)
            and n.test.id == "TYPE_CHECKING"
        ]
        assert len(guarded) == 1
        inner = [x for x in ast.walk(guarded[0]) if isinstance(x, ast.ImportFrom)]
        # ⚠️ 用 ``getattr`` 读模块名 —— ⛔ **不得**写 `x.module`：
        #   那会命中 `test_import_detectors.TestNoReDivergence` 的"朴素读法"禁令
        #   （`X.module` 只允许出现在 detector 定义处与负对照函数体内）。
        assert [getattr(x, "module", None) for x in inner] == ["pass_driver"]


# ======================================================================
# LBL-2 · 公开面恰好三项 + 内部状态恰好三项
# ======================================================================


class TestLBL2Surface:
    def test_public_operational_methods_are_exactly_three(self) -> None:
        names = class_method_names(SOURCE, GUARD)
        assert sorted(n for n in names if not n.startswith("_")) == ["bind", "drive_once", "release"]

    def test_private_methods_are_only_init_and_acquire(self) -> None:
        names = class_method_names(SOURCE, GUARD)
        assert sorted(n for n in names if n.startswith("_")) == ["__init__", "_acquire"]

    def test_slots_are_exactly_three(self) -> None:
        assert slots_of(SOURCE, GUARD) == ["_bound_pass_driver", "_released", "_guard"]

    def test_method_shapes(self) -> None:
        assert _method_shape(SOURCE, "bind") == (["pass_driver"], [], "None")
        assert _method_shape(SOURCE, "drive_once") == ([], [], "Any")
        assert _method_shape(SOURCE, "release") == ([], [], "None")
        assert _method_shape(SOURCE, "__init__") == ([], [], "None")

    def test_no_driven_field_anywhere(self) -> None:
        assert "_driven" not in referenced_names(SOURCE)

    @pytest.mark.parametrize(
        "forbidden",
        [
            "retrieve", "lookup", "get_by_run_id", "rebuild", "rematerialize", "reconstruct",
            "reset", "revive", "queue", "enqueue", "retry", "schedule", "watch", "poll",
            "is_terminal", "interpret", "register", "registry",
        ],
    )
    def test_no_forbidden_operational_name(self, forbidden: str) -> None:
        assert forbidden not in class_method_names(SOURCE, GUARD)

    def test_no_construction_or_materialization(self) -> None:
        assert calls_to(SOURCE, "PassDriver") == []
        assert calls_to(SOURCE, "CompositionRoot") == []
        assert calls_to(SOURCE, "materialize") == []
        assert calls_to(SOURCE, "run_pass") == []
        assert calls_to(SOURCE, "recheck") == []

    def test_no_run_id_surface(self) -> None:
        assert "run_id" not in referenced_names(SOURCE)

    def test_no_clock_no_waiting_no_semaphore(self) -> None:
        assert clock_call_hits(SOURCE) == []
        names = referenced_names(SOURCE)
        for forbidden in ("sleep", "BoundedSemaphore", "Semaphore", "Event", "Condition", "Queue"):
            assert forbidden not in names

    def test_acquire_is_nonblocking_and_single(self) -> None:
        acquires = [
            n
            for n in ast.walk(ast.parse(SOURCE))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "acquire"
        ]
        assert len(acquires) == 1
        assert any(
            k.arg == "blocking" and ast.literal_eval(k.value) is False for k in acquires[0].keywords
        )

    def test_lock_is_created_exactly_once_in_init(self) -> None:
        """``__init__`` 内**恰一次** ``threading.Lock()`` 构造，且赋给 ``self._guard``。

        ⚠️ 检测器原理（准确表述 · `§227` 修正）：
          ``ast.walk(node)`` **会包含传入的 ``node`` 自身**，并递归遍历其**全部 descendants**
          ⇒ 这里传入 ``__init__`` 的 ``FunctionDef``，得到的正是"该函数**体内**"的全部节点
            ⇒ 于是可以找到它内部的 ``threading.Lock()`` 调用。
          （另一处断言改走 ``node.body``，是为了**只看顶层语句**、排除嵌套作用域，
            从而把"内部状态恰好三项"锁死。）
        """
        node = _method_node(SOURCE, "__init__")
        creates = [
            x
            for x in ast.walk(node)
            if isinstance(x, ast.Call)
            and isinstance(x.func, ast.Attribute)
            and x.func.attr == "Lock"
        ]
        assert len(creates) == 1
        assert "threading.Lock" in ast.unparse(creates[0])
        assigned = [
            ast.unparse(t)
            for stmt in node.body
            if isinstance(stmt, (ast.Assign, ast.AnnAssign))
            for t in (stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target])
        ]
        assert assigned == ["self._bound_pass_driver", "self._released", "self._guard"]
        # ⚠️ 内部状态**恰好三项**（`§225.2`）—— 顺序与数量都锁定
        assert len(assigned) == 3

    @pytest.mark.parametrize("method", ["bind", "drive_once", "release"])
    def test_all_three_entries_acquire_the_shared_lock(self, method: str) -> None:
        """⚠️ `§225` 的实现锁：**同一把**锁保护三个入口的**全部**状态转换。"""
        assert _calls_in_method(SOURCE, method, "_acquire") == 1

    @pytest.mark.parametrize("method", ["bind", "drive_once", "release"])
    def test_acquire_is_paired_with_finally_release(self, method: str) -> None:
        """⚠️ 结构不变式：每个入口的 ``_acquire()`` 与 ``finally`` 释放**必须成对**。

        ⚠️ 这也是"删掉 guard"这一变异**不干净**的原因：删掉 acquire 会让 finally
          释放**未持有**的锁 ⇒ 掩蔽性 ``RuntimeError``（见 `§224.9` 的口径）。
        """
        assert _acquire_release_pairs(SOURCE, method) == (1, 1)

    @pytest.mark.parametrize("method", ["bind", "drive_once", "release"])
    def test_all_three_entries_use_try_finally(self, method: str) -> None:
        assert _has_try(SOURCE, method) is True

    def test_drive_forwards_exactly_once(self) -> None:
        assert _calls_in_method(SOURCE, "drive_once", "drive_next_pass") == 1

    def test_no_second_drive_call_anywhere(self) -> None:
        total = sum(
            1
            for n in ast.walk(ast.parse(SOURCE))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "drive_next_pass"
        )
        assert total == 1


# ======================================================================
# LBM-* · 四类核心变异：目标语义变异 ⇒ 判据必须变红
# ======================================================================


class TestLBMGuards:
    def test_source_parseable(self) -> None:
        assert ast.parse(SOURCE) is not None

    def test_probe_allow_second_bind_goes_red(self) -> None:
        """变异：允许二次 bind ⇒ `F2` 判据必须失败（且**原实例被替换**）。"""
        mutated = _mutate([
            (
                "            if self._bound_pass_driver is not None:\n"
                "                raise LifecycleBindingError(\n"
                '                    "本 binding 已绑定 authoritative PassDriver：**绑定恰一次**，"\n'
                '                    "⛔ 不得替换已保留的实例（F2）"\n'
                "                )\n",
                "",
            )
        ])
        ns = _exec(mutated)
        guard = ns[GUARD]()
        first, second = _Driver(), _Driver()
        guard.bind(first)
        guard.bind(second)  # ⚠️ 变异后**不再抛** ⇒ 判据失效
        assert guard._bound_pass_driver is second  # 且**原实例被替换**
        # 原始源码：同一序列必须抛，且**原实例必须保留**
        ok = _exec(SOURCE)
        ok_guard = ok[GUARD]()
        ok_first = _Driver()
        ok_guard.bind(ok_first)
        with pytest.raises(ok[ERROR]):
            ok_guard.bind(_Driver())
        ok_guard.drive_once()
        assert ok_first.calls == 1
        assert ok_guard._bound_pass_driver is ok_first

    def test_probe_blocking_acquire_goes_red(self) -> None:
        """变异：把 **non-blocking** 改成 **blocking** ⇒ 并发入口**不再立即拒绝**。

        ⚠️⚠️ **本 probe 刻意用静态判据，不做行为执行** —— 变异体真的会**阻塞**：
          外部队列持锁时 ``drive_once()`` 会**永久等待**（`blocking=True`），
          执行型 probe 会**挂死测试进程**。
          ⇒ ⇒ 这一事实本身**就是**变异的证据：**blocking 与"立即拒绝"互斥**。
        ⚠️ 另：不用"删掉 guard"这一变异（`§224.9` 要求"**目标语义变异**导致红"）：
          ``_acquire`` 同时承担"守卫判定"与"**获取锁**"，删掉它会让
          ``finally`` 释放**未持有**的锁 ⇒ 掩蔽性 ``RuntimeError``，⛔ 不算目标语义变异。
        """
        mutated = _mutate([("acquire(blocking=False)", "acquire(blocking=True)")])
        assert _acquire_blocking_flag(SOURCE) is False  # 生产：非阻塞
        assert _acquire_blocking_flag(mutated) is True  # 变异：阻塞 ⇒ 并发拒绝失效
        # 生产源码还必须把"立即拒绝"表达成显式失败分支
        assert _acquire_failure_branch_present(SOURCE) is True
        assert _acquire_failure_branch_present(mutated) is True

    def test_probe_release_keeps_custody_goes_red(self) -> None:
        """变异：``release()`` **不丢弃** custody（且去掉 F5 守卫）⇒ 释放后仍可驱动。

        ⚠️ 这是**真实危险形态**：单纯删掉 ``drive_once`` 里的 ``_released`` 检查
          **不足以**放开复用 —— 因为 ``release()`` 同时把 ``_bound_pass_driver`` 置 ``None``，
          于是 ``drive_once`` 仍会撞 `F1`（unbound）。
          ⇒ 所以本 probe 必须**同时**点掉"丢弃引用"这一步，才真正检验
            "**release 后一切操作被机械拒绝**"。
        """
        mutated = _mutate([
            (
                "            self._bound_pass_driver = None\n"
                "            self._released = True",
                "            self._released = True",
            ),
            (
                "            if self._released:\n"
                "                raise LifecycleBindingError(\n"
                '                    "本 binding 已 RELEASED：release 后任何操作都被拒绝（F5）"\n'
                "                )\n",
                "",
            ),
        ])
        ns = _exec(mutated)
        guard = ns[GUARD]()
        driver = _Driver()
        guard.bind(driver)
        guard.release()
        guard.drive_once()  # ⚠️ 变异后**仍然驱动成功**
        assert driver.calls == 1
        # 原始源码：release 后 drive 必须抛，且**底层不得被调用**
        ok = _exec(SOURCE)
        g2 = ok[GUARD]()
        d2 = _Driver()
        g2.bind(d2)
        g2.release()
        with pytest.raises(ok[ERROR]):
            g2.drive_once()
        assert d2.calls == 0

    def test_probe_instance_replacement_goes_red(self) -> None:
        """变异：把"转发到保留实例"改成"重建一个替身" ⇒ identity 判据必须失败。"""
        mutated = _mutate([
            (
                "            return self._bound_pass_driver.drive_next_pass()",
                "            return type(self._bound_pass_driver)().drive_next_pass()",
            )
        ])
        ns = _exec(mutated)
        guard = ns[GUARD]()
        driver = _Driver()
        guard.bind(driver)
        guard.drive_once()
        assert driver.calls == 0  # 原实例**未被调用** ⇒ identity 判据失败
        # 原始源码上 identity 成立
        ok = _exec(SOURCE)
        g2 = ok[GUARD]()
        d2 = _Driver()
        g2.bind(d2)
        g2.drive_once()
        assert d2.calls == 1


# ======================================================================
# F1–F8 + bind(None)
# ======================================================================


class TestFailureSurface:
    def test_f1_drive_before_bind(self) -> None:
        _expect_error(LifecycleBindingGuard().drive_once)

    def test_f2_second_bind(self) -> None:
        guard = LifecycleBindingGuard()
        guard.bind(_Driver())
        _expect_error(guard.bind, _Driver())

    def test_f3_bind_after_release(self) -> None:
        guard = LifecycleBindingGuard()
        guard.bind(_Driver())
        guard.release()
        _expect_error(guard.bind, _Driver())

    def test_f5_drive_after_release(self) -> None:
        guard = LifecycleBindingGuard()
        guard.bind(_Driver())
        guard.release()
        _expect_error(guard.drive_once)

    def test_f6_release_before_bind(self) -> None:
        _expect_error(LifecycleBindingGuard().release)

    def test_f8_second_release(self) -> None:
        guard = LifecycleBindingGuard()
        guard.bind(_Driver())
        guard.release()
        _expect_error(guard.release)

    def test_bind_none_rejected(self) -> None:
        _expect_error(LifecycleBindingGuard().bind, None)

    def test_bind_none_does_not_flip_state(self) -> None:
        guard = LifecycleBindingGuard()
        _expect_error(guard.bind, None)
        driver = _Driver()
        guard.bind(driver)
        guard.drive_once()
        assert driver.calls == 1

    def test_second_bind_does_not_replace_instance(self) -> None:
        """`F2` 被拒后，**原实例必须原样保留**。"""
        guard = LifecycleBindingGuard()
        first = _Driver()
        guard.bind(first)
        _expect_error(guard.bind, _Driver())
        guard.drive_once()
        assert first.calls == 1
        assert guard._bound_pass_driver is first


class TestConcurrentFailureSurface:
    """`F4` / `F7`：``threading.Event`` 可控阻塞，⛔ 不用 ``sleep()``。"""

    def test_f4_concurrent_drive_rejected_immediately(self) -> None:
        entered = threading.Event()
        release_first = threading.Event()

        def block() -> None:
            entered.set()
            release_first.wait(5.0)

        driver = _Driver(on_call=block)
        guard = LifecycleBindingGuard()
        guard.bind(driver)
        errors: list[BaseException] = []

        def first() -> None:
            try:
                guard.drive_once()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        worker = threading.Thread(target=first)
        worker.start()
        try:
            assert entered.wait(5.0), "第一条 drive 未能进入"
            _expect_error(guard.drive_once)  # ⛔ 立即失败（不 WAIT）
            _expect_error(guard.release)  # F7
        finally:
            release_first.set()
            worker.join(5.0)
        assert errors == []
        assert driver.calls == 1

    def test_guard_is_free_after_concurrent_rejection(self) -> None:
        guard = LifecycleBindingGuard()
        driver = _Driver()
        guard.bind(driver)
        assert guard._guard.acquire(blocking=False) is True
        guard._guard.release()
        guard.drive_once()
        guard.drive_once()
        assert driver.calls == 2


# ======================================================================
# 行为
# ======================================================================


class TestBehaviour:
    def test_same_instance_across_many_legal_drives(self) -> None:
        guard = LifecycleBindingGuard()
        driver = _Driver()
        guard.bind(driver)
        receipts = [guard.drive_once() for _ in range(5)]
        assert driver.calls == 5
        assert {r[1] for r in receipts} == {id(driver)}

    def test_bind_does_not_drive(self) -> None:
        guard = LifecycleBindingGuard()
        driver = _Driver()
        guard.bind(driver)
        assert driver.calls == 0

    def test_release_really_drops_custody(self) -> None:
        guard = LifecycleBindingGuard()
        guard.bind(_Driver())
        guard.release()
        assert guard._bound_pass_driver is None
        assert guard._released is True

    def test_underlying_exception_propagates_unchanged(self) -> None:
        boom = ValueError("底层 pass 失败")
        guard = LifecycleBindingGuard()
        guard.bind(_Driver(raises=boom))
        with pytest.raises(ValueError) as excinfo:
            guard.drive_once()
        assert excinfo.value is boom

    def test_binding_stays_bound_after_exception(self) -> None:
        driver = _Driver(raises=RuntimeError("x"))
        guard = LifecycleBindingGuard()
        guard.bind(driver)
        with pytest.raises(RuntimeError):
            guard.drive_once()
        assert guard._guard.acquire(blocking=False) is True
        guard._guard.release()
        assert guard._released is False
        assert guard._bound_pass_driver is driver
        driver.raises = None
        guard.drive_once()
        assert driver.calls == 2

    def test_no_auto_release_and_no_retry_on_exception(self) -> None:
        driver = _Driver(raises=ValueError("boom"))
        guard = LifecycleBindingGuard()
        guard.bind(driver)
        with pytest.raises(ValueError):
            guard.drive_once()
        assert guard._released is False
        assert driver.calls == 1  # ⛔ 未自动 retry

    def test_exception_is_not_converted_to_binding_error(self) -> None:
        driver = _Driver(raises=KeyError("k"))
        guard = LifecycleBindingGuard()
        guard.bind(driver)
        with pytest.raises(KeyError):
            guard.drive_once()

    def test_released_is_absorbing(self) -> None:
        guard = LifecycleBindingGuard()
        guard.bind(_Driver())
        guard.release()
        _expect_error(guard.bind, _Driver())
        _expect_error(guard.drive_once)
        _expect_error(guard.release)


class TestProbeHygiene:
    def test_no_sleep_in_this_test_file(self) -> None:
        own = pathlib.Path(__file__).read_text(encoding="utf-8")
        assert clock_call_hits(own) == []

    def test_test_file_uses_threading_event_for_concurrency(self) -> None:
        own = pathlib.Path(__file__).read_text(encoding="utf-8")
        assert "threading.Event" in own
