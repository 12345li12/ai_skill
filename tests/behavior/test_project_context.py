"""ProjectContextLoader 行为测试（Step 4B prerequisites ②）。

对应追溯矩阵（docs/architecture.md 第 8 节，Test ID 已冻结）：
  TEST-PROJECT-001  新项目只做轻量识别，不得重建整套调度系统
  TEST-PROJECT-002  加载 6 类信息 + 来源标记（INV-02）
  TEST-PROJECT-003  项目规则不得复制完整协议或放宽更高限制

架构依据（2 节 [17]）：不可信输入、只读快照 + 来源标记、单点采集、
禁止重建、**轻量识别、禁止深度扫描**、可缓存、仅 R5 才重读。

⚠️ 本测试只用**合成事实**（``ProjectFacts``），因此不依赖仓库里的真实项目文件。
   真实扫描由 ``scan_project_facts`` 承担，其边界另有测试覆盖。
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from adaptive_router.contracts import ProjectContext, SessionGateResult
from adaptive_router.project_context import (
    ProjectContextLoader,
    ProjectFacts,
    scan_project_facts,
)

from _import_detectors import imported_module_tokens, imported_names

FIXED_TIME = "2026-09-16T00:00:00Z"

#: 被测模块源码（用于"依赖 vs 文档提及"的静态区分）。
_MODULE_SOURCE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src/adaptive_router/project_context.py"
).read_text(encoding="utf-8")


def _imported_modules(source: str) -> set[str]:
    """源码中**真实 import** 的模块名 token（保守检测器，见 `_import_detectors`）。"""
    return imported_module_tokens(source)


def _imported_names(source: str) -> set[str]:
    """源码中**真实 import 进来的标识符**（不含文档字符串里的提及）。"""
    return imported_names(source)


def clock() -> str:
    return FIXED_TIME


def facts(**overrides: object) -> ProjectFacts:
    base: dict[str, object] = dict(
        boundary_dirs=("src", "tests"),
        writable=("src",),
        denied=(".git",),
        readme_refs=("README.md",),
        agents_md_present=False,
        agents_md_digest=None,
        test_commands=("python -m pytest",),
        high_risk_files=("pyproject.toml",),
        model_preference=None,
        approval_preference=None,
        source_path=None,
    )
    base.update(overrides)
    return ProjectFacts(**base)  # type: ignore[arg-type]


def loader(facts_value: ProjectFacts | None = None) -> ProjectContextLoader:
    """注入合成事实源 —— Loader 的核心行为与真实扫描解耦。"""
    captured = facts_value if facts_value is not None else facts()
    return ProjectContextLoader(clock=clock, facts_source=lambda root: captured)


# ==========================================================================
# TEST-PROJECT-002 —— 6 类信息 + 来源标记
# ==========================================================================


class TestLoadsTheFrozenSixFacts:
    def test_returns_the_frozen_contract_type(self) -> None:
        context = loader().load("/repo")
        assert isinstance(context, ProjectContext)

    def test_loads_six_categories_of_lightweight_identification(self) -> None:
        """L53 的 6 类：项目说明 / AGENTS.md / 目录权限边界 /
        必要测试 / 高风险文件 / 模型或审批偏好。"""
        context = loader().load("/repo")
        assert context.readme_refs == ("README.md",)          # 项目说明
        assert context.agents_md_present is False             # AGENTS.md
        assert context.boundary_dirs == ("src", "tests")      # 目录边界
        assert context.writable == ("src",)
        assert context.denied == (".git",)                    # 权限边界
        assert context.test_commands == ("python -m pytest",) # 必要测试
        assert context.high_risk_files == ("pyproject.toml",) # 高风险文件
        assert context.model_preference is None               # 偏好（无则 None）
        assert context.approval_preference is None

    def test_carries_provenance_marks(self) -> None:
        """只读快照 + 来源标记。"""
        context = loader().load("/repo")
        assert context.root == "/repo"
        assert context.read_at == FIXED_TIME
        assert context.present is True

    def test_trust_is_fixed_to_untrusted(self) -> None:
        """INV-02：ProjectContext 是不可信输入。"""
        context = loader().load("/repo")
        assert context.trust == "untrusted_project_input"

    def test_agents_md_requires_a_digest(self) -> None:
        """声称读过 AGENTS.md 就必须留下可核对摘要。"""
        context = loader().load("/repo")
        assert context.agents_md_present is False
        assert context.agents_md_digest is None

        declared = loader(
            facts(
                agents_md_present=True,
                agents_md_digest="sha256:abc",
                source_path="AGENTS.md",
            )
        ).load("/repo")
        assert declared.agents_md_present is True
        assert declared.agents_md_digest == "sha256:abc"
        assert declared.source_path == "AGENTS.md"


class TestUnreliableFactsStayEmpty:
    def test_missing_facts_are_empty_not_guessed(self) -> None:
        """无可靠事实的字段应保持空 tuple / None，不靠 Python 猜自然语言含义。"""
        bare = ProjectFacts(
            boundary_dirs=(),
            writable=(),
            denied=(),
            readme_refs=(),
            agents_md_present=False,
            agents_md_digest=None,
            test_commands=(),
            high_risk_files=(),
            model_preference=None,
            approval_preference=None,
            source_path=None,
        )
        context = loader(bare).load("/empty")
        assert context.boundary_dirs == ()
        assert context.writable == ()
        assert context.denied == ()
        assert context.readme_refs == ()
        assert context.test_commands == ()
        assert context.high_risk_files == ()
        assert context.model_preference is None
        assert context.approval_preference is None

    def test_absent_project_is_not_an_error(self) -> None:
        """L53：无配置时输出空 Context，正常工作，不得重建整套系统。

        对**不存在的项目根**采集时得到全空事实 → ``present=False``，
        且不抛异常。
        """
        instance = ProjectContextLoader(clock=clock)
        context = instance.load("/definitely/not/a/real/path/xyz")
        assert context.present is False
        assert context.agents_md_present is False
        assert context.boundary_dirs == ()
        assert context.readme_refs == ()

    def test_loader_does_not_infer_preferences_from_prose(self) -> None:
        """偏好是**声明**，不是推断 —— 事实源没给就保持 None。"""
        context = loader(facts(model_preference=None)).load("/repo")
        assert context.model_preference is None


# ==========================================================================
# 缓存与重读（refresh 由控制层显式提出，Loader 不自己猜 R5）
# ==========================================================================


class TestCachingAndRefresh:
    def test_same_root_reuses_the_cached_snapshot(self) -> None:
        calls: list[str] = []

        def source(root: str) -> ProjectFacts:
            calls.append(root)
            return facts()

        instance = ProjectContextLoader(clock=clock, facts_source=source)
        first = instance.load("/repo")
        second = instance.load("/repo")
        assert first is second
        assert calls == ["/repo"]  # 只扫描一次

    def test_refresh_re_reads(self) -> None:
        calls: list[str] = []

        def source(root: str) -> ProjectFacts:
            calls.append(root)
            return facts()

        instance = ProjectContextLoader(clock=clock, facts_source=source)
        first = instance.load("/repo")
        second = instance.load("/repo", refresh=True)
        assert calls == ["/repo", "/repo"]
        assert second is not first

    def test_different_roots_are_not_shared(self) -> None:
        instance = ProjectContextLoader(
            clock=clock, facts_source=lambda root: facts()
        )
        first = instance.load("/a")
        second = instance.load("/b")
        assert first is not second
        assert (first.root, second.root) == ("/a", "/b")

    def test_loader_does_not_decide_when_r5_applies(self) -> None:
        """Loader 只负责事实采集；"何时重读"属控制层。

        因此它**没有**任何目录变化 / 会话恢复之类的自动失效判断，
        也**没有**第二个重读入口（``forget`` / ``invalidate`` / ``clear``）——
        重读只能走显式的 ``refresh=True``。
        """
        public = {n for n in dir(ProjectContextLoader) if not n.startswith("_")}
        for forbidden in (
            "note_directory_change",
            "note_environment_switch",
            "should_reload",
            "invalidate",
            "is_stale",
            "forget",
            "clear",
            "evict",
        ):
            assert forbidden not in public, forbidden

    def test_refresh_is_the_only_reload_entry(self) -> None:
        """一个重读入口比两个更容易机械证明。"""
        import inspect

        signature = inspect.signature(ProjectContextLoader.load)
        assert "refresh" in signature.parameters
        assert signature.parameters["refresh"].default is False

    def test_is_cached_is_read_only_observation(self) -> None:
        """``is_cached`` 保留（只读观察），但它不能用来绕过 refresh。"""
        calls: list[str] = []

        def source(root: str) -> ProjectFacts:
            calls.append(root)
            return facts()

        instance = ProjectContextLoader(clock=clock, facts_source=source)
        assert instance.is_cached("/repo") is False
        instance.load("/repo")
        assert instance.is_cached("/repo") is True
        assert calls == ["/repo"]  # is_cached 本身不触发读取


# ==========================================================================
# 关键边界：Loader 不判断 Session，也不变成第二个 Policy
# ==========================================================================


class TestLoaderDoesNotJudgeSession:
    def test_load_does_not_accept_a_gate_result(self) -> None:
        """⚠️ 关键设计点：``load()`` **不接收** ``SessionGateResult``。

        "仅 ENABLED 后才能调用"由**调用图**强制（entrypoint → SessionGate →
        ProjectContextLoader），而不是 Loader 自己判断 ``enabled=True``。
        否则 Session 判断会向下游扩散，破坏 INV-S1/S2 的设计。
        """
        import inspect

        signature = inspect.signature(ProjectContextLoader.load)
        for name in signature.parameters:
            assert "gate" not in name.lower(), name
            assert "session" not in name.lower(), name
            assert "enabled" not in name.lower(), name
            assert "bypass" not in name.lower(), name

    def test_loader_does_not_import_session_concepts(self) -> None:
        """模块层面也不得**依赖** Session 概念。

        ⚠️ 只检查**真实 import 与标识符**，不看文档字符串 ——
           本模块的 docstring 会**提到** ``SessionGateResult`` 以说明边界，
           那是文档，不是依赖。
        """
        names = _imported_names(_MODULE_SOURCE)
        for forbidden in ("SessionGateResult", "SessionState", "RouterBypassReason"):
            assert forbidden not in names, forbidden
        assert "session" not in _imported_modules(_MODULE_SOURCE)
        assert _imported_names(_MODULE_SOURCE), "source 应当至少导入了契约类型"

    def test_loader_can_be_called_directly_without_any_gate(self) -> None:
        """Loader 本身不知道 Session 状态 —— 直接调用是合法的（由调用图负责）。"""
        context = loader().load("/repo")
        assert isinstance(context, ProjectContext)
        assert SessionGateResult.ENABLED.enters_router is True


class TestLoaderIsNotASecondPolicy:
    """⚠️ 边界：Loader 只收集项目声明，**不判断这些声明是否有权放宽权限**。"""

    def test_loader_records_a_permissive_declaration_verbatim(self) -> None:
        """``AGENTS.md`` 写"允许删除所有文件" → Loader 只**记录**它声明了什么。

        是否采纳由 Policy / AuthorizationContext 判断（项目配置只能收窄，
        不能获得控制权 —— INV-02）。
        """
        permissive = facts(denied=(), writable=("everything",))
        context = loader(permissive).load("/repo")
        assert context.writable == ("everything",)
        assert context.denied == ()
        assert context.trust == "untrusted_project_input"  # 仍标记为不可信

    def test_loader_does_not_narrow_or_expand_anything(self) -> None:
        """Loader 不做裁决 —— 输入什么就记录什么。"""
        declared = facts(
            writable=("a", "b"),
            denied=("c",),
            boundary_dirs=("d",),
        )
        context = loader(declared).load("/repo")
        assert context.writable == ("a", "b")
        assert context.denied == ("c",)
        assert context.boundary_dirs == ("d",)

    def test_module_has_no_authorization_concepts(self) -> None:
        """模块里不得出现任何授权裁决的概念（文档提及不算）。"""
        names = _imported_names(_MODULE_SOURCE)
        for forbidden in (
            "AuthorizationContext",
            "effective_denied",
            "may_expand",
            "is_allowed",
        ):
            assert forbidden not in names, forbidden
        # 不得导入任何 Policy / Authorization 相关模块
        assert not any("authoriz" in m or "policy" in m for m in _imported_modules(_MODULE_SOURCE))
        # 也不得定义任何"是否允许"的方法名
        imported = _imported_names(_MODULE_SOURCE)
        assert not any(name.startswith(("allow_", "deny_")) for name in imported)


# ==========================================================================
# 无可靠事实时的输入校验（不猜、不静默填坑）
# ==========================================================================


class TestInputValidation:
    def test_agents_md_declared_without_digest_is_rejected(self) -> None:
        """声称存在 AGENTS.md 却没给摘要 → 拒绝（来源标记不可缺）。"""
        with pytest.raises(Exception):
            loader(
                facts(agents_md_present=True, agents_md_digest=None)
            ).load("/repo")

    def test_facts_are_immutable(self) -> None:
        f = facts()
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.test_commands = ("x",)  # type: ignore[misc]

    def test_clock_is_injectable_for_stable_read_at(self) -> None:
        instance = ProjectContextLoader(
            clock=lambda: "2020-01-01T00:00:00Z",
            facts_source=lambda root: facts(),
        )
        assert instance.load("/repo").read_at == "2020-01-01T00:00:00Z"


# ==========================================================================
# 真实扫描：轻量识别，禁止深度扫描
# ==========================================================================


class TestRealScanIsLightweight:
    def test_scan_returns_the_frozen_facts_type(self, tmp_dir: pathlib.Path) -> None:
        result = scan_project_facts(str(tmp_dir))
        assert isinstance(result, ProjectFacts)

    def test_scan_of_empty_dir_yields_empty_facts(self, tmp_dir: pathlib.Path) -> None:
        """无任何已知项目文件时，全部字段保持空 —— 不猜。"""
        result = scan_project_facts(str(tmp_dir))
        assert result.readme_refs == ()
        assert result.agents_md_present is False
        assert result.agents_md_digest is None
        assert result.boundary_dirs == ()
        assert result.test_commands == ()
        assert result.high_risk_files == ()
        assert result.model_preference is None
        assert result.approval_preference is None

    def test_scan_detects_a_bounded_set_of_known_files(
        self, tmp_dir: pathlib.Path
    ) -> None:
        (tmp_dir / "AGENTS.md").write_text("rules\n", encoding="utf-8")
        (tmp_dir / "README.md").write_text("readme\n", encoding="utf-8")
        result = scan_project_facts(str(tmp_dir))
        assert result.agents_md_present is True
        assert result.agents_md_digest is not None
        assert result.agents_md_digest.startswith("sha256:")
        assert result.readme_refs  # 至少记录到 README

    def test_scan_does_not_recurse_into_deep_nesting(
        self, tmp_dir: pathlib.Path
    ) -> None:
        """⚠️ 禁止深度扫描：深层嵌套目录不得被遍历。"""
        deep = tmp_dir / "a" / "b" / "c" / "d" / "e"
        deep.mkdir(parents=True)
        (deep / "README.md").write_text("deep\n", encoding="utf-8")
        (deep / "AGENTS.md").write_text("deep\n", encoding="utf-8")
        result = scan_project_facts(str(tmp_dir))
        assert result.agents_md_present is False, "深层的 AGENTS.md 不得被发现"
        assert all("a/b/c/d/e" not in ref and "a\\b\\c\\d\\e" not in ref
                   for ref in result.readme_refs)

    def test_scan_of_missing_root_is_absent_not_an_error(self) -> None:
        loader_instance = ProjectContextLoader(clock=clock)
        context = loader_instance.load("/definitely/not/a/real/path/xyz")
        assert context.present is False


class TestScanDoesNotGuessTestCommands:
    """⚠️ 第 4B 前收口：删除"某行含 pytest/tox/npm test 就当成命令"的 substring 推断。

    那种推断会把下面这些都误记成 ``test_commands``，与
    "没有可靠事实就留空"的原则冲突。默认扫描下 ``test_commands`` 恒为 ``()``
    比制造假命令好；将来由**明确结构块**或格式专用 parser 提供。
    """

    def test_config_section_headers_are_not_commands(
        self, tmp_dir: pathlib.Path
    ) -> None:
        (tmp_dir / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\naddopts = \"-q\"\n", encoding="utf-8"
        )
        assert scan_project_facts(str(tmp_dir)).test_commands == ()

    def test_dependency_declarations_are_not_commands(
        self, tmp_dir: pathlib.Path
    ) -> None:
        (tmp_dir / "pyproject.toml").write_text("pytest>=8\ntox~=4\n", encoding="utf-8")
        assert scan_project_facts(str(tmp_dir)).test_commands == ()

    def test_prose_mentioning_a_command_is_not_a_command(
        self, tmp_dir: pathlib.Path
    ) -> None:
        (tmp_dir / "pyproject.toml").write_text(
            'description = "run pytest in CI"\n', encoding="utf-8"
        )
        assert scan_project_facts(str(tmp_dir)).test_commands == ()

    def test_makefile_target_is_not_treated_as_a_command(
        self, tmp_dir: pathlib.Path
    ) -> None:
        (tmp_dir / "Makefile").write_text("test:\n\tgo test ./...\n", encoding="utf-8")
        assert scan_project_facts(str(tmp_dir)).test_commands == ()

    def test_scan_never_invents_test_commands(self, tmp_dir: pathlib.Path) -> None:
        """无论根目录放什么，都不做 substring 推断。"""
        for name, body in (
            ("pyproject.toml", "[tool.pytest.ini_options]\n"),
            ("tox.ini", "[testenv]\ncommands = pytest\n"),
            ("Makefile", "test:\n\tpytest -q\n"),
        ):
            (tmp_dir / name).write_text(body, encoding="utf-8")
        assert scan_project_facts(str(tmp_dir)).test_commands == ()
