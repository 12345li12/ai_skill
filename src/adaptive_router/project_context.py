"""project_context —— [17] Project Context Loader。

架构依据：`docs/architecture.md` v1.0 FROZEN
  - 2 节 [17] Project Context Loader
  - 1.2 PERCEPTION 层
  - 7.2 INV-02

职责（**只做一件事**）：把项目声明的**事实**采集为冻结契约 ``ProjectContext``。

    ✅ 只读快照 + 来源标记 + ``trust="untrusted_project_input"``
    ✅ 轻量识别（L53 的 6 类），**禁止深度扫描**
    ✅ 单点采集（其他节点不得各自重读）
    ✅ 可缓存；**只有控制层显式提出 ``refresh=True`` 才重读**

⚠️ 三条**边界**（不得越界）：

   1. **不判断 Session。** ``load()`` 不接收 ``SessionGateResult``，
      也不自己判断 ENABLED。"仅 ENABLED 后才能调用"由**调用图**强制：

          entrypoint → SessionGate → (ENABLED only) → ProjectContextLoader.load()

      否则 Session 判断会向下游扩散，破坏 INV-S1 / INV-S2 的设计。

   2. **不决定 R5。** Loader 不猜"什么时候算失效"。R5 失效条件由控制层
      判断后**显式**传入 ``refresh=True``。因此本模块没有任何自动失效方法。

   3. **不变成第二个 Policy。** Loader **只收集项目声明**，
      不判断这些声明是否有权放宽权限。``ProjectContext`` 本身是不可信输入
      （INV-02），"只能收窄、不能获得控制权"由 Policy / AuthorizationContext 裁决：

          AGENTS.md 写"允许删除所有文件"
                  ↓
          ProjectContext 只记录它声明了什么
                  ↓
          Policy / AuthorizationContext 发现它比上层权限更宽 → 不采纳扩大部分
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass
from typing import Callable, Protocol

from .contracts import ProjectContext

__all__ = [
    "ProjectFacts",
    "FactsSource",
    "Clock",
    "ProjectContextLoader",
    "scan_project_facts",
    "AGENTS_MD_FILENAME",
    "README_CANDIDATES",
    "BOUNDARY_DIR_CANDIDATES",
    "HIGH_RISK_FILE_CANDIDATES",
]


#: AGENTS.md 的固定文件名（L53）。
AGENTS_MD_FILENAME = "AGENTS.md"

#: 项目说明的候选文件名 —— **只查根目录**，不递归。
README_CANDIDATES: tuple[str, ...] = ("README.md", "README.rst", "README.txt", "README")

#: 目录/权限边界的候选目录名 —— **只查根目录一层**。
BOUNDARY_DIR_CANDIDATES: tuple[str, ...] = (
    "src",
    "tests",
    "test",
    "docs",
    "scripts",
    "examples",
)

#: 高风险文件的候选 —— **只查根目录**。
HIGH_RISK_FILE_CANDIDATES: tuple[str, ...] = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    ".github",
)

#: 测试命令的候选来源（声明式，不做推断）。
# ⚠️ 当前**不使用** —— 见 ``scan_project_facts`` 中"测试命令：当前不采集"的说明。
#    保留常量仅为将来接"明确结构块 parser"时的参照，不参与任何 substring 推断。
_TEST_COMMAND_FILES: tuple[str, ...] = ()


class Clock(Protocol):
    """返回 RFC3339 时间戳字符串。可注入，便于稳定生成 ``read_at``。"""

    def __call__(self) -> str:  # pragma: no cover - 协议声明
        ...


class FactsSource(Protocol):
    """给定项目根目录，返回**合成/采集后的事实**。"""

    def __call__(self, root: str) -> "ProjectFacts":  # pragma: no cover - 协议声明
        ...


@dataclass(frozen=True)
class ProjectFacts:
    """项目事实的**原始集合**。

    这是 ``ProjectContext`` 的构造输入，与契约分离，使 Loader 的行为
    可对**合成事实**测试，而不依赖仓库里的真实项目文件。

    ⚠️ 无可靠事实的字段保持空 tuple / ``None`` —— **不靠猜测填坑**。
       偏好（``model_preference`` / ``approval_preference``）是**声明**，
       不是从自然语言里推断出来的。
    """

    boundary_dirs: tuple[str, ...]
    writable: tuple[str, ...]
    denied: tuple[str, ...]
    readme_refs: tuple[str, ...]
    agents_md_present: bool
    agents_md_digest: str | None
    test_commands: tuple[str, ...]
    high_risk_files: tuple[str, ...]
    model_preference: str | None
    approval_preference: str | None
    source_path: str | None


class ProjectContextLoader:
    """[17] 的唯一 Loader 边界。

    ⚠️ **不接收 ``SessionGateResult``，不判断 ENABLED，不决定 R5。**
       ``refresh`` 由控制层在 R5 触发时**显式**提出。
    """

    def __init__(
        self,
        *,
        clock: Clock,
        facts_source: FactsSource | None = None,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock 必须可调用")
        self._clock = clock
        self._facts_source: FactsSource = (
            facts_source if facts_source is not None else scan_project_facts
        )
        self._cache: dict[str, ProjectContext] = {}

    # ------------------------------------------------------------------

    def load(self, root: str, *, refresh: bool = False) -> ProjectContext:
        """采集（或复用缓存）某个项目根目录的只读快照。

        ``refresh=False`` → 同一 ``root`` 复用缓存快照；
        ``refresh=True``  → 重新读取（**由控制层在 R5 触发时显式提出**）。

        ⚠️ 本方法**不接收**任何 Session / gate 参数。
        """
        if not isinstance(root, str) or not root.strip():
            raise ValueError("root 必须是非空字符串")

        if not refresh and root in self._cache:
            return self._cache[root]

        facts = self._facts_source(root)
        if not isinstance(facts, ProjectFacts):
            raise TypeError(f"facts_source 必须返回 ProjectFacts，收到 {facts!r}")

        context = self._to_context(root, facts)
        self._cache[root] = context
        return context

    def is_cached(self, root: str) -> bool:
        return root in self._cache

    # ⚠️ 刻意**不提供**公开的 ``forget()`` / ``invalidate()``。
    #    否则调用方可以 ``forget(root)`` + ``load(root)`` 绕过显式边界，
    #    在**没有 R5** 的情况下再次读取项目。
    #    架构写的是"缓存允许，但**只有 R5 失效条件出现时才重读**"；
    #    既然已选定 ``refresh=True`` 作为唯一显式控制边界，
    #    **一个重读入口比两个更容易机械证明**。

    # ------------------------------------------------------------------

    def _to_context(self, root: str, facts: ProjectFacts) -> ProjectContext:
        """把事实装配成冻结契约（只装配，不裁决）。"""
        if facts.agents_md_present and facts.agents_md_digest is None:
            raise ValueError(
                "声称存在 AGENTS.md 时必须给出 digest（来源标记不可缺）"
            )

        return ProjectContext(
            root=root,
            present=_has_any_fact(facts),
            boundary_dirs=tuple(facts.boundary_dirs),
            writable=tuple(facts.writable),
            denied=tuple(facts.denied),
            readme_refs=tuple(facts.readme_refs),
            agents_md_present=facts.agents_md_present,
            agents_md_digest=facts.agents_md_digest,
            test_commands=tuple(facts.test_commands),
            high_risk_files=tuple(facts.high_risk_files),
            model_preference=facts.model_preference,
            approval_preference=facts.approval_preference,
            source_path=facts.source_path,
            read_at=self._clock(),
        )


def _has_any_fact(facts: ProjectFacts) -> bool:
    """是否采集到任何**可靠事实**。

    ⚠️ ``present`` 由此**派生**，不由调用方或事实源自由填写 ——
       避免"声称 present 但什么都没读到"的不一致状态。

       无可靠事实时 ``present=False`` **不是错误**（L53：无配置时正常工作）。
    """
    return bool(
        facts.readme_refs
        or facts.agents_md_present
        or facts.boundary_dirs
        or facts.test_commands
        or facts.high_risk_files
        or facts.writable
        or facts.denied
        or facts.model_preference is not None
        or facts.approval_preference is not None
    )


# ==========================================================================
# 真实扫描：轻量识别，**禁止深度扫描**
# ==========================================================================


def _digest(path: pathlib.Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _read_text(path: pathlib.Path, *, limit: int = 8192) -> str:
    """有界读取 —— 只取开头一段，避免把大文件整份读进来。"""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.read(limit)


def scan_project_facts(root: str) -> ProjectFacts:
    """轻量识别（L53）。

    ⚠️ **只在项目根目录一层查找**已知文件与已知目录名。
       **不递归、不深扫**、不遍历任意嵌套结构。

    无可靠事实的字段保持空 —— 不做自然语言推断。
    """
    base = pathlib.Path(root)
    if not base.is_dir():
        return ProjectFacts(
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

    # 项目说明：只查根目录的候选名
    readme_refs = tuple(name for name in README_CANDIDATES if (base / name).is_file())

    # AGENTS.md：只查根目录
    agents_path = base / AGENTS_MD_FILENAME
    agents_md_present = agents_path.is_file()
    agents_md_digest = _digest(agents_path) if agents_md_present else None

    # 目录边界：只查根目录一层
    boundary_dirs = tuple(
        name for name in BOUNDARY_DIR_CANDIDATES if (base / name).is_dir()
    )

    # 高风险文件：只查根目录
    high_risk_files = tuple(
        name for name in HIGH_RISK_FILE_CANDIDATES if (base / name).exists()
    )

    # 测试命令：**当前不采集**。
    #
    # ⚠️ 曾经的做法是"某行**包含** pytest / tox / npm test / go test 就把整行
    #    当成命令"，那是**过度积极的推断**，会把下面这些都误记成命令：
    #
    #        [tool.pytest.ini_options]        ← 配置小节标题
    #        pytest>=8                        ← 依赖声明
    #        description = "run pytest in CI" ← 纯描述文本
    #
    #    这与"没有可靠事实就留空"的原则冲突。因此直接删除该 substring 推断：
    #    **默认扫描下 test_commands 恒为 ()**，比制造假命令好。
    #    将来若需要，应由**明确结构块**或格式专用 parser 提供
    #    （属 4B 之后、接 Policy 之前的工作）。
    test_commands: tuple[str, ...] = ()

    # 权限边界 / 偏好：**不推断**。
    # "writable" / "denied" 与偏好必须由项目**显式声明** ——
    # 当前不解析自然语言，一律保持空 / None。
    return ProjectFacts(
        boundary_dirs=boundary_dirs,
        writable=(),
        denied=(),
        readme_refs=readme_refs,
        agents_md_present=agents_md_present,
        agents_md_digest=agents_md_digest,
        test_commands=test_commands,
        high_risk_files=high_risk_files,
        model_preference=None,
        approval_preference=None,
        source_path=AGENTS_MD_FILENAME if agents_md_present else None,
    )
