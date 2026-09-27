"""`RelPkg` 发布验证共享件 —— **只读取证**（⛔ 不改仓库任何文件）。

⚠️ 本模块**绝不 import** `adaptive_router`：既避免在仓库 `src/` 下产生 `__pycache__`
   （`src/** NO WRITE` 是硬边界），也避免被 `pythonpath = ["src"]` 遮蔽而**假绿**。
   所有真实导入都在**干净 venv** 里、以**中立 cwd** 执行（见 `install_check.py`）。

⚠️ 所有子进程输出一律**重定向到文件**（⛔ 不走管道）。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import sys
import urllib.request
import zipfile

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRATCH = REPO / ".pytest-tmp/retry_prep/relpkg"
BUILD_ROOT = SCRATCH / "build_root"
DIST = SCRATCH / "dist"
DEPS = SCRATCH / "build_deps"
VENVS = SCRATCH / "venvs"
LOGS = SCRATCH / "logs"

PYPI_JSON = "https://pypi.org/pypi/{pkg}/json"
#: `Q2` 绕道路线所需的构建依赖（⛔ 不走 `pypi.org/simple/`，那条路在本环境报 SSLError）
BUILD_DEP_PKGS = ("setuptools", "wheel")
#: 打进 wheel / sdist 的源文件（`MANIFEST.in` 与内容断言共用同一口径）
PACKAGE_ROOT = "src/adaptive_router"
REGISTRY = REPO / ".pytest-tmp/cb_frozen.txt"
REGISTRY_ROW = re.compile(r"^(SAME|NEW|CHANGED|REBASED|EXTRA)\s+(\S+)\s+(\d+)\s+([0-9A-F]{64})$")

_RESULTS: list[tuple[str, str, bool]] = []


# --------------------------------------------------------------------------
# 目录 / 报告
# --------------------------------------------------------------------------


def ensure_dirs() -> None:
    for path in (SCRATCH, BUILD_ROOT, DIST, DEPS, VENVS, LOGS):
        path.mkdir(parents=True, exist_ok=True)


def say(label: str, value: str, good: bool = True) -> bool:
    _RESULTS.append((label, value, good))
    print(f"  {'✅' if good else '❌'}  {label:<52} {value}")
    return good


def note(text: str) -> None:
    print(f"      {text}")


def summary(title: str) -> int:
    bad = [r for r in _RESULTS if not r[2]]
    print()
    print(f"=== {title} ===")
    print(f"  ✅ {len(_RESULTS) - len(bad)} 项通过 · ❌ {len(bad)} 项失败")
    for label, value, _ in bad:
        print(f"     !! {label} :: {value}")
    return 0 if not bad else 1


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def run(cmd: list[str], *, cwd: pathlib.Path, log: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    """执行子进程；stdout/stderr **重定向到日志文件**（⛔ 不走管道）。"""
    LOGS.mkdir(parents=True, exist_ok=True)
    target = LOGS / f"{log}.txt"
    full_env = dict(**__import__("os").environ)
    # pip 的临时目录必须在 workspace 内（系统 temp 受沙箱限制）
    tmp = SCRATCH / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    full_env.update({"TEMP": str(tmp), "TMP": str(tmp), "TMPDIR": str(tmp)})
    full_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    if env:
        full_env.update(env)
    with target.open("w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(str(c) for c in cmd) + f"\n(cwd={cwd})\n\n")
        handle.flush()
        code = subprocess.run(  # noqa: S603
            [str(c) for c in cmd], cwd=str(cwd), stdout=handle, stderr=subprocess.STDOUT, check=False, env=full_env
        ).returncode
    return code, target.read_text(encoding="utf-8", errors="replace")


def module_names(src_root: pathlib.Path) -> list[str]:
    """把 `src/adaptive_router/**/*.py` 映射成**点分模块名**（确定性排序）。

    口径：`__init__.py` ⇒ 包名本身；其余 ⇒ 去 `.py` 后缀。
    """
    root = src_root / "adaptive_router"
    names: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        rel = path.relative_to(src_root).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.append(".".join(parts))
    return sorted(set(names))


def src_manifest(root: pathlib.Path) -> dict[str, str]:
    """`root` 下（排除 `__pycache__`）逐文件 SHA256 —— 用于**零 diff** 自证。"""
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in str(path):
            out[str(path.relative_to(root)).replace("\\", "/")] = sha256(path)
    return out


def registry_rows() -> list[tuple[str, str, int, str]]:
    rows: list[tuple[str, str, int, str]] = []
    for line in REGISTRY.read_text(encoding="utf-8").splitlines():
        m = REGISTRY_ROW.match(line.strip())
        if m:
            rows.append((m.group(1), m.group(2), int(m.group(3)), m.group(4)))
    return rows


def protected_module_names() -> list[str]:
    """registry 中位于 `src/adaptive_router/` 的受保护模块 ⇒ 点分名（`L2` 口径）。"""
    out: list[str] = []
    for _tag, rel, _size, _sha in registry_rows():
        if not rel.startswith("src/adaptive_router/") or not rel.endswith(".py"):
            continue
        parts = list(pathlib.PurePosixPath(rel).relative_to("src").with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out.append(".".join(parts))
    return sorted(set(out))


def pyproject() -> dict:
    import tomllib  # noqa: PLC0415

    return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# `Q2` 绕道：JSON API → files.pythonhosted.org（⛔ 不走 /simple/）
# --------------------------------------------------------------------------


def _urlopen(url: str, *, attempts: int = 5, timeout: int = 60):
    """带**重试**的 urlopen。

    ⚠️ **环境绕道第 4 条**：本环境到 `pypi.org` 的 TLS 连接**不稳定** ——
    同一 URL 会时通时断（`SSLEOFError UNEXPECTED_EOF_WHILE_READING`）。
    ⇒ 一律重试；且**本地 wheel 缓存优先**（见 `fetch_wheel`），使命中缓存时**完全不触网**。
    """
    import time  # noqa: PLC0415

    last: Exception | None = None
    for index in range(attempts):
        try:
            return urllib.request.urlopen(url, timeout=timeout)  # noqa: S310
        except Exception as exc:  # noqa: BLE001
            last = exc
            if index + 1 < attempts:
                time.sleep(1.5 * (index + 1))
    raise RuntimeError(f"urlopen 重试 {attempts} 次仍失败：{url} :: {type(last).__name__}: {last}")


def fetch_wheel(pkg: str) -> pathlib.Path:
    """取得某个包的 wheel：**本地缓存优先**；未命中才走 `Q2` 绕道路线（JSON API → 直下）。

    ⛔ 不使用 `pypi.org/simple/`（本环境实测 `SSLError UNEXPECTED_EOF_WHILE_READING`）。
    """
    DEPS.mkdir(parents=True, exist_ok=True)
    cached = sorted(DEPS.glob(f"{pkg.replace('-', '_')}-*.whl")) + sorted(DEPS.glob(f"{pkg}-*.whl"))
    for candidate in cached:
        if _is_zip(candidate):
            print(f"  ✅ 构建依赖（本地缓存）{candidate.name:<40} {candidate.stat().st_size:>9,} B  {sha256(candidate)[:16]}…")
            return candidate

    with _urlopen(PYPI_JSON.format(pkg=pkg)) as response:
        info = json.load(response)
    pick = next((u for u in info["urls"] if u["filename"].endswith(".whl")), None)
    if pick is None:
        raise RuntimeError(f"{pkg}: JSON API 未提供 wheel")
    dest = DEPS / pick["filename"]
    if not dest.exists() or sha256(dest) != pick["digests"]["sha256"].upper():
        with _urlopen(pick["url"], attempts=5, timeout=180) as response:
            dest.write_bytes(response.read())
    actual, want = sha256(dest), pick["digests"]["sha256"].upper()
    if actual != want:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"{pkg}: sha256 不符 {actual} != {want}")
    print(f"  ✅ 取构建依赖 {pkg:<12} {info['info']['version']:<8} {dest.stat().st_size:>9,} B  sha256 MATCH")
    note("经 JSON API 解析 → files.pythonhosted.org（⛔ 未走 /simple/）")
    return dest


def _is_zip(path: pathlib.Path) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# 环境绕道第 3 条：修 `tempfile.mkdtemp`（**跨进程** · 经 `sitecustomize`）
# --------------------------------------------------------------------------

#: ⚠️ 实测根因：本机 `os.mkdir(path, mode)` 会建出**创建者自己都无法访问**的目录
#: （`PermissionError [WinError 5]`），而 `os.mkdir(path)`（默认 mode）完全正常。
#: `tempfile.mkdtemp` 恰好用 `os.mkdir(file, 0o700)` ⇒ 一切用它的代码全炸：
#: setuptools 的 staging（`_build_with_temp_dir`）与 pip 的 `pip-req-build-*` 都是。
#: ⇒ 用 `sitecustomize` 在**解释器启动时**替换 `tempfile.mkdtemp`（语义等价、⛔ 不传 mode）。
#: ⚠️ 只在 `PYTHONPATH` 显式指向本目录时生效；⛔ 不写进项目文件（属 scratch 绕道）。
SITECUSTOMIZE = '''"""环境绕道：`tempfile.mkdtemp` 的 `os.mkdir(path)` 版替身（语义等价）。

本机 `os.mkdir(path, mode)` 建出的目录**创建者自己都无法 scandir/rmtree**
（`PermissionError [WinError 5]`）；`os.mkdir(path)` 正常。
仅在 `PYTHONPATH` 显式指向本目录时被 `site` 自动导入。
"""

import os
import tempfile

_TMP_MAX = getattr(tempfile, "TMP_MAX", 10000)


def _mkdtemp(suffix=None, prefix=None, dir=None):
    if dir is None:
        dir = tempfile.gettempdir()
    suffix = suffix or ""
    prefix = prefix or "tmp"
    for name in tempfile._get_candidate_names():  # noqa: SLF001
        path = os.path.join(dir, prefix + name + suffix)
        try:
            os.mkdir(path)  # ← 关键：⛔ 不传 0o700
        except FileExistsError:
            continue
        return path
    raise FileExistsError("mkdtemp 无法创建唯一目录")


tempfile.mkdtemp = _mkdtemp
'''


def ensure_pysite() -> pathlib.Path:
    """把 `sitecustomize` 绕道写进 scratch，返回应加进 `PYTHONPATH` 的目录。"""
    target = SCRATCH / "pysite"
    target.mkdir(parents=True, exist_ok=True)
    path = target / "sitecustomize.py"
    if not path.exists() or path.read_text(encoding="utf-8") != SITECUSTOMIZE:
        path.write_text(SITECUSTOMIZE, encoding="utf-8")
    return target


def pip_env() -> dict[str, str]:
    """给 pip / 构建子进程的环境：把绕道目录前置进 `PYTHONPATH`。"""
    import os  # noqa: PLC0415

    existing = os.environ.get("PYTHONPATH", "")
    parts = [str(ensure_pysite())] + ([existing] if existing else [])
    return {"PYTHONPATH": os.pathsep.join(parts)}


# --------------------------------------------------------------------------
# venv
# --------------------------------------------------------------------------


def venv_dir(name: str) -> pathlib.Path:
    return VENVS / name


def venv_python(venv: pathlib.Path) -> pathlib.Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def make_venv(venv: pathlib.Path, base_python: str, *, fresh: bool = False) -> pathlib.Path:
    """建一个干净 venv（含 pip）并返回其 python 路径。

    ⚠️ `fresh=True` ⇒ **先删掉已存在的 venv**。这不是洁癖：复用 venv 时
    `pip install <同版本 wheel>` 会判 "Requirement already satisfied" 而**什么都不做**
    ⇒ 探针实际测的是**上一次的安装** ⇒ **假绿**（MP-4 反证就是这么暴露出来的）。
    安装检查一律 `fresh=True`（"干净环境"的字面要求）。

    ⚠️ **环境绕道第 2 条**：本沙箱下 `python -m venv`（默认走 `ensurepip`）会以
    `PermissionError: [Errno 13]` 失败（`ensurepip` 把内置 wheel 拷进临时目录时被拒，
    尽管同一目录的普通写入是允许的）。⇒ 改用 `--without-pip` 建 venv，
    再经 **JSON API 路线**取 `pip` wheel 并 `zipfile` 解包进 venv 的 `site-packages`。
    """
    import shutil  # noqa: PLC0415

    if fresh and venv.exists():
        shutil.rmtree(venv, ignore_errors=True)
    python = venv_python(venv)
    if not python.exists():
        code, out = run(
            [base_python, "-m", "venv", "--without-pip", str(venv)], cwd=SCRATCH, log=f"venv_{venv.name}"
        )
        if code != 0:
            raise RuntimeError(f"venv 创建失败：\n{out[-1500:]}")
    ensure_pip(venv, python)
    return python


def ensure_pip(venv: pathlib.Path, python: pathlib.Path) -> bool:
    """确保 venv 内有可用 pip；缺则用 **JSON API → 直下 wheel → 解包** 的方式补上。"""
    code, _ = run([python, "-m", "pip", "--version"], cwd=SCRATCH, log=f"{venv.name}_pipprobe")
    if code == 0:
        return True
    wheel = fetch_wheel("pip")
    site = next(venv.rglob("site-packages"), None)
    if site is None:
        raise RuntimeError(f"{venv} 内找不到 site-packages")
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    code, out = run([python, "-m", "pip", "--version"], cwd=SCRATCH, log=f"{venv.name}_pipprobe2")
    if code != 0:
        raise RuntimeError(f"pip bootstrap 失败：\n{out[-1200:]}")
    return True
