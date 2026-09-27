"""`RelPkg` **RP-2 / D3** —— sdist 内容清单断言（tarfile 只读遍历）。

`Q3` 裁定：sdist **保持最小** —— `docs/` 与 `original-skill/` **不进**；
且 `tests/`、`.pytest-tmp/`（**135 MB** 风险面 · `R7`）、`release/` 同样不得进入。

用法：`python release/sdist_check.py`
"""

from __future__ import annotations

import pathlib
import sys
import tarfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _common as C  # noqa: E402

#: sdist 里出现即越界的**路径段**
FORBIDDEN_SEGMENTS = ("tests/", "docs/", "original-skill/", "adapters/", "config/", "release/", ".pytest-tmp/")
FORBIDDEN_MARKERS = ("__pycache__", ".pyc", ".pyo")
#: sdist **必须**携带的构建输入
REQUIRED = ("pyproject.toml", "README.md", "PKG-INFO")


def main() -> int:
    C.ensure_dirs()
    print("=== RP-2 / D3 sdist 内容清单 ===")
    sdists = sorted(C.DIST.glob("*.tar.gz"))
    if not C.say("sdist 存在", str([s.name for s in sdists]), len(sdists) == 1):
        return C.summary("RP-2 sdist 内容")
    sdist = sdists[0]

    with tarfile.open(sdist) as archive:
        members = archive.getnames()
    C.say("sdist 成员总数", str(len(members)))
    roots = sorted({n.split("/")[0] for n in members})
    C.say("顶层根目录名", str(roots), len(roots) == 1)
    root = roots[0]
    relative = [n[len(root) + 1 :] for n in members if n != root and n.startswith(root + "/")]

    # ---- 1) 越界段 -----------------------------------------------------------
    offenders = [n for n in relative if n.startswith(FORBIDDEN_SEGMENTS) or f"/{n}".find("/") >= 0 and any(
        seg in n for seg in ("__pycache__",)
    )]
    offenders = [n for n in relative if n.startswith(FORBIDDEN_SEGMENTS)]
    C.say("⛔ 越界路径段 = 0", str(offenders[:5]) if offenders else "无", not offenders)
    marked = [n for n in relative if any(m in n for m in FORBIDDEN_MARKERS)]
    C.say("⛔ 缓存/.pyc = 0", str(marked[:5]) if marked else "无", not marked)

    # ---- 2) 必需构建输入 -----------------------------------------------------
    for needed in REQUIRED:
        C.say(f"必需件 · {needed}", "存在" if needed in relative else "缺失", needed in relative)

    # ---- 3) 包源码完整 -------------------------------------------------------
    sdist_py = {n for n in relative if n.startswith("src/") and n.endswith(".py")}
    # ⚠️ `src_manifest(REPO/"src")` 的 key 相对 `src/` ⇒ sdist 侧要补回 `src/` 前缀。
    repo_py = {f"src/{rel}" for rel in C.src_manifest(C.REPO / "src") if rel.endswith(".py")}
    C.say("sdist 内 src/*.py 数", str(len(sdist_py)), len(sdist_py) == 68)
    missing = sorted(repo_py - sdist_py)
    extra = sorted(sdist_py - repo_py)
    C.say("源码集合**逐个相等**", f"缺 {len(missing)} · 多 {len(extra)}", not missing and not extra)
    for name in missing[:6]:
        C.note(f"缺：{name}")

    # ---- 4) 体量（R7：`.pytest-tmp` 135 MB 若误入会极其显眼）----------------
    size = sdist.stat().st_size
    C.say("sdist 体量", f"{size:,} B（远小于 .pytest-tmp 的 135 MB ⇒ R7 未发生）", size < 5_000_000)

    print()
    print("--- sdist 内容清单 ---")
    for name in sorted(relative):
        C.note(name)
    return C.summary("RP-2 sdist 内容")


if __name__ == "__main__":
    sys.exit(main())
