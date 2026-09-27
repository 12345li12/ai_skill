"""`RelPkg` **RP-1 / D2** —— wheel 内容**精确清单**断言（zipfile 只读遍历 · ⛔ 不解包）。

判据不是"数量对"，而是**成员集合逐个相等**（⟸ 数量断言会漏掉"少一个 A、多一个 B"）。
用法：`python release/wheel_check.py`
"""

from __future__ import annotations

import pathlib
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _common as C  # noqa: E402

EXPECTED_PACKAGES = {
    "adaptive_router",
    "adaptive_router/adapters",
    "adaptive_router/contracts",
    "adaptive_router/cost",
}
#: 出现在 wheel 里就是**越界**的顶层前缀
FORBIDDEN_PREFIXES = (
    "tests/",
    "docs/",
    "original-skill/",
    "adapters/",
    "config/",
    "release/",
    ".pytest-tmp/",
)
FORBIDDEN_MARKERS = ("__pycache__", ".egg-info", ".pyc", ".pyo")


def main() -> int:
    C.ensure_dirs()
    print("=== RP-1 / D2 wheel 内容精确清单 ===")
    wheels = sorted(C.DIST.glob("*.whl"))
    if not C.say("wheel 存在", str([w.name for w in wheels]), len(wheels) == 1):
        return C.summary("RP-1 wheel 内容")
    wheel = wheels[0]

    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
    C.say("wheel 成员总数", str(len(members)))

    # ---- 1) 包集合 -----------------------------------------------------------
    # ⚠️ `namelist()` 给的是 `pkg/__init__.py` ⇒ 包名要**去掉** `/__init__.py` 后缀
    #    （首版误用 `rstrip("/")`，把整条文件路径当成了包名）。
    package_dirs = {
        name[: -len("/__init__.py")]
        for name in members
        if name.endswith("/__init__.py") and ".dist-info/" not in name
    }
    C.say("包集合（含子包）", str(sorted(package_dirs)), package_dirs == EXPECTED_PACKAGES)
    C.say("包数 = 4", str(len(package_dirs)), len(package_dirs) == 4)

    # ---- 2) `adaptive_router/` 下的成员**逐个相等** ---------------------------
    # ⚠️ `src_manifest(REPO/"src")` 的 key 已经**相对 `src/`**（形如 `adaptive_router/x.py`），
    #    ⛔ 不可再切 `len("src/")`（首版就是这里切错了，导致 68 缺 / 68 多）。
    repo_py = {rel for rel in C.src_manifest(C.REPO / "src") if rel.endswith(".py")}
    packaged_py = {n for n in members if n.endswith(".py") and ".dist-info/" not in n}
    C.say("源模块数（仓库 src/）", str(len(repo_py)))
    C.say("打进 wheel 的 .py 数", str(len(packaged_py)), len(packaged_py) == 68)
    missing = sorted(repo_py - packaged_py)
    extra = sorted(packaged_py - repo_py)
    C.say("成员集合**逐个相等**", f"缺 {len(missing)} · 多 {len(extra)}", not missing and not extra)
    for name in missing[:6]:
        C.note(f"缺：{name}")
    for name in extra[:6]:
        C.note(f"多：{name}")

    # ---- 3) 包目录内**零非 .py** --------------------------------------------
    under_pkg = [
        n
        for n in members
        if n.startswith("adaptive_router/") and not n.endswith("/") and ".dist-info/" not in n
    ]
    non_py = [n for n in under_pkg if not n.endswith(".py")]
    C.say("包目录内非 .py 文件 = 0", str(len(non_py)), not non_py)
    for name in non_py[:6]:
        C.note(f"非 .py：{name}")

    # ---- 4) 越界前缀 / 标记 --------------------------------------------------
    offenders = [n for n in members if n.startswith(FORBIDDEN_PREFIXES)]
    C.say("⛔ 越界顶层前缀 = 0", str(offenders[:4]) if offenders else "无", not offenders)
    marked = [n for n in members if any(m in n for m in FORBIDDEN_MARKERS)]
    C.say("⛔ 缓存/egg-info/.pyc = 0", str(marked[:4]) if marked else "无", not marked)

    # ---- 5) dist-info 元数据存在 -------------------------------------------
    dist_info = sorted({n.split("/")[0] for n in members if ".dist-info/" in n})
    C.say("dist-info", str(dist_info), len(dist_info) == 1)
    names = {n.split("/", 1)[1] for n in members if n.startswith(dist_info[0] + "/")}
    for needed in ("METADATA", "WHEEL", "RECORD"):
        C.say(f"dist-info/{needed}", "存在" if needed in names else "缺失", needed in names)

    print()
    print("--- wheel 内包结构 ---")
    for name in sorted(members):
        C.note(name)
    return C.summary("RP-1 wheel 内容")


if __name__ == "__main__":
    sys.exit(main())
