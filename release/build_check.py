"""`RelPkg` **D1** —— 构建 wheel + sdist（`RP-0` 前置）。

**构建路径 = `Q2` 的 `RelPkg environment workaround`**：JSON API → 直下 wheel → venv 本地安装 →
直接调用 PEP 517 后端（`setuptools.build_meta`）。⛔ **不是** official build policy。

⚠️ **为什么在 scratch 副本里构建（重要）**：`setuptools` 构建时会在包目录旁写
`src/adaptive_router.egg-info/`，并在仓库根写 `build/` 与 `dist/`。
而本单元的硬边界是 **`src/** NO WRITE`** ⇒ 一律在
`.pytest-tmp/retry_prep/relpkg/build_root/` 的**逐字节副本**里构建，
并**断言副本与仓库 `src/` 的 manifest 完全一致**（⇒ 构建产物具有代表性）。

用法：`python release/build_check.py`
"""

from __future__ import annotations

import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _common as C  # noqa: E402

REQUIRED_INPUTS = ("pyproject.toml", "README.md", "MANIFEST.in")

DRIVER = '''"""PEP 517 后端直调驱动（在 build venv 内运行 · cwd = build_root）。

⚠️ **环境绕道第 3 条（必须）**：本环境下 `tempfile.mkdtemp` 建出的目录**无法被
`os.scandir` / `shutil.rmtree` / `cmd rmdir` 访问或删除**（`PermissionError [WinError 5]`，
残留目录**永久不可删**），而 `os.mkdir` 建出的目录一切正常。setuptools 的
`build_meta._build_with_temp_dir` 恰好用 `tempfile.TemporaryDirectory` 做 staging
⇒ 构建会在**收尾清理**处炸掉（实测：已跑到写 `dist-info/WHEEL` 才失败）。
⇒ 用 `os.mkdir` 版**语义等价**替换（唯一目录名 + 退出时删除）。
"""

import itertools
import os
import pathlib
import shutil
import sys
import tempfile

_counter = itertools.count()


class _PlainTemporaryDirectory:
    """`tempfile.TemporaryDirectory` 的 `os.mkdir` 版替身（语义等价）。"""

    def __init__(self, suffix=None, prefix=None, dir=None, ignore_cleanup_errors=False):
        base = pathlib.Path(dir) if dir else pathlib.Path(tempfile.gettempdir())
        self.name = str(base / f"{prefix or ''}{os.getpid()}_{next(_counter)}{suffix or ''}")
        os.mkdir(self.name)

    def __enter__(self):
        return self.name

    def __exit__(self, *exc):
        self.cleanup()
        return False

    def cleanup(self):
        shutil.rmtree(self.name, ignore_errors=True)


tempfile.TemporaryDirectory = _PlainTemporaryDirectory

from setuptools import build_meta as backend  # noqa: E402

dist = pathlib.Path(sys.argv[1]).resolve()
dist.mkdir(parents=True, exist_ok=True)
print("WHEEL", backend.build_wheel(str(dist)))
print("SDIST", backend.build_sdist(str(dist)))
'''


def prepare_build_root() -> bool:
    """把构建输入**逐字节复制**到 scratch，并证明副本与仓库一致。"""
    if C.BUILD_ROOT.exists():
        shutil.rmtree(C.BUILD_ROOT)
    C.BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    ok = True
    for name in REQUIRED_INPUTS:
        source = C.REPO / name
        if not source.exists():
            ok = C.say(f"构建输入存在 · {name}", "缺失", False)
            continue
        shutil.copy2(source, C.BUILD_ROOT / name)
        ok = C.say(f"构建输入复制 · {name}", f"{source.stat().st_size} B", True)
    shutil.copytree(C.REPO / "src", C.BUILD_ROOT / "src", ignore=shutil.ignore_patterns("__pycache__"))

    repo_manifest = C.src_manifest(C.REPO / "src")
    copy_manifest = C.src_manifest(C.BUILD_ROOT / "src")
    same = repo_manifest == copy_manifest
    ok = C.say(
        "构建副本与仓库 src/ **逐字节一致**",
        f"{len(copy_manifest)} 件 · {'manifest 相同' if same else '不一致 ❌'}",
        same,
    )
    return ok


def main() -> int:
    C.ensure_dirs()
    print("=== D1 构建（wheel + sdist）===")

    prepare_build_root()

    print()
    print("--- 构建依赖（Q2 绕道路线）---")
    wheels = [C.fetch_wheel(pkg) for pkg in C.BUILD_DEP_PKGS]

    print()
    print("--- build venv ---")
    base = sys.executable
    venv = C.venv_dir("build")
    python = C.make_venv(venv, base)
    C.say("build venv", str(venv.relative_to(C.REPO)))
    code, out = C.run(
        [python, "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir", *[str(w) for w in wheels]],
        cwd=C.SCRATCH,
        log="build_deps_install",
        env=C.pip_env(),
    )
    C.say("构建依赖装入 venv", f"exit={code}", code == 0)
    if code != 0:
        C.note(out[-800:])
        return C.summary("D1 构建")

    print()
    print("--- 工具链版本（固化记录）---")
    for label, args in (
        ("python", ["--version"]),
        ("pip", ["-m", "pip", "--version"]),
        ("setuptools", ["-c", "import setuptools;print(setuptools.__version__)"]),
        ("wheel", ["-c", "import wheel;print(wheel.__version__)"]),
    ):
        code, out = C.run([python, *args], cwd=C.SCRATCH, log=f"ver_{label}")
        text = out.strip().splitlines()[-1] if out.strip() else "?"
        C.say(f"build venv · {label}", text, code == 0)

    print()
    print("--- PEP 517 后端直调（⛔ 无 `python -m build`，本机不可用）---")
    driver = C.SCRATCH / "_build_driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    # ⚠️ ⛔ 不用 `shutil.rmtree(DIST)`：本环境可能残留**不可删除**的 `mkdtemp` 目录
    #    （环境绕道第 3 条）⇒ 只清**产物**，逐件删除，残留空目录无害（不匹配 *.whl / *.tar.gz）。
    C.DIST.mkdir(parents=True, exist_ok=True)
    stale = [p for p in C.DIST.iterdir() if p.is_file() and (p.suffix == ".whl" or p.name.endswith(".tar.gz"))]
    for path in stale:
        path.unlink()
    C.say("dist 历史产物已清", f"删除 {len(stale)} 件 · 残留目录 {len(list(C.DIST.iterdir())) - 0} 项", True)
    code, out = C.run(
        [python, str(driver), str(C.DIST)], cwd=C.BUILD_ROOT, log="pep517_build", env=C.pip_env()
    )
    C.say("build_wheel + build_sdist", f"exit={code}", code == 0)
    if code != 0:
        C.note(out[-1200:])
        return C.summary("D1 构建")

    print()
    print("--- 产物 ---")
    artifacts = sorted(p for p in C.DIST.iterdir() if p.is_file())
    C.say("产物件数", str(len(artifacts)), len(artifacts) == 2)
    for path in artifacts:
        C.say(f"{path.name}", f"{path.stat().st_size:,} B · {C.sha256(path)[:16]}…", True)
    kinds = {".whl" if p.suffix == ".whl" else ".tar.gz" for p in artifacts}
    C.say("wheel + sdist 各一", str(sorted(kinds)), kinds == {".whl", ".tar.gz"})
    C.say("构建写入位置", f"{C.DIST.relative_to(C.REPO)}（⛔ 仓库 `src/` 未被写）", True)
    return C.summary("D1 构建")


if __name__ == "__main__":
    sys.exit(main())
