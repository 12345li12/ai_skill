"""`RelPkg` **RP-3…RP-7 / D4–D8** —— 干净环境安装 + 身份 + **导入来源** + L1/L2 import。

★ `RP-4`（`D6`）是本单元**最重要的防假绿检查**：
  `pyproject.toml` 里有 `pythonpath = ["src"]`，若在仓库根跑 import，导到的会是**仓库源码**
  而不是安装产物 —— 那样"安装成功"就是假的。所以：
    ① 探针以 **中立 cwd**（scratch）执行；
    ② 断言 `adaptive_router.__file__` **落在 venv 的 site-packages 内**。

⚠️ `L1`/`L2` 的期望模块表来自**仓库**（⛔ 不是从安装产物反推）—— 否则装残了也能自证通过。

用法：`python release/install_check.py [--base-python <exe>] [--venv-name <n>] [--artifact wheel|sdist]`
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _common as C  # noqa: E402

PROBE = r'''"""安装产物探针（在**目标 venv 内**、以**中立 cwd**执行）。"""

import importlib
import importlib.metadata as md
import json
import pathlib
import sys

EXPECTED = json.loads(r"""__EXPECTED__""")

report = {"python": sys.version.split()[0], "checks": {}, "detail": {}, "failures": []}


def check(name, value, good, detail=""):
    report["checks"][name] = bool(good)
    report["detail"][name] = value
    if not good:
        report["failures"].append(f"{name}: {value} {detail}")


# RP-3 元数据身份
meta = md.metadata("adaptive-agent-router")
version = md.version("adaptive-agent-router")
check("version_matches_pyproject", version, version == EXPECTED["version"],
      f"期望 {EXPECTED['version']}")
check("requires_python", meta["Requires-Python"] or "",
      (meta["Requires-Python"] or "") == EXPECTED["requires_python"],
      f"期望 {EXPECTED['requires_python']}")
requires = meta.get_all("Requires-Dist") or []
check("requires_dist_empty", requires, requires == [], "期望 [] （zero runtime third-party dependency）")

# RP-4 导入来源
import adaptive_router  # noqa: E402

origin = pathlib.Path(adaptive_router.__file__).resolve()
report["detail"]["origin"] = str(origin)
report["detail"]["site_packages"] = EXPECTED["site_packages"]
inside = str(origin).lower().startswith(EXPECTED["site_packages"].lower())
check("import_origin_in_site_packages", str(origin), inside,
      f"必须在 {EXPECTED['site_packages']} 内（⛔ 不是仓库 src/）")
check("import_origin_not_repo_src", str(origin), "src" not in str(origin).replace("\\", "/").split("/")[:3])

# RP-5 / RP-6 L1 / L2 import
for label, key in (("L1", "modules_l1"), ("L2", "modules_l2")):
    bad = []
    for name in EXPECTED[key]:
        try:
            importlib.import_module(name)
        except BaseException as exc:  # noqa: BLE001
            bad.append(f"{name}: {type(exc).__name__}: {exc}")
    check(f"{label}_import_all", f"{len(EXPECTED[key]) - len(bad)}/{len(EXPECTED[key])}", not bad)
    if bad:
        report["detail"][f"{label}_bad"] = bad[:10]

print("PROBE:" + json.dumps(report))
'''


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-python", default=sys.executable)
    parser.add_argument("--venv-name", default="install314")
    parser.add_argument("--artifact", choices=("wheel", "sdist"), default="wheel")
    args = parser.parse_args()

    C.ensure_dirs()
    print(f"=== RP-3…RP-7 / D4–D8 干净安装（{args.artifact} · venv={args.venv_name}）===")

    cfg = C.pyproject()["project"]
    artifacts = sorted(C.DIST.glob("*.whl" if args.artifact == "wheel" else "*.tar.gz"))
    if not C.say("产物存在", str([a.name for a in artifacts]), len(artifacts) == 1):
        return C.summary("D4–D8 干净安装")
    artifact = artifacts[0]

    print("--- D4 干净 venv ---")
    venv = C.venv_dir(args.venv_name)
    # ⚠️ **fresh**：复用 venv 会让同版本 `pip install` 变成 no-op
    #    （"Requirement already satisfied"）⇒ 探针测到的是**上一次的安装** ⇒ 假绿。
    #    （MP-4 反证就是这样暴露出来的。）"干净环境" 的字面要求也在此。
    python = C.make_venv(venv, args.base_python, fresh=True)
    site_packages = venv / ("Lib/site-packages" if sys.platform == "win32" else "lib/python3.14/site-packages")
    for candidate in venv.rglob("site-packages"):
        site_packages = candidate
        break
    C.say("venv", str(venv.relative_to(C.REPO)))
    code, out = C.run([python, "--version"], cwd=C.SCRATCH, log=f"{args.venv_name}_ver")
    C.say("venv 解释器", out.strip().splitlines()[-1] if out.strip() else "?", code == 0)
    C.say("venv site-packages", str(site_packages.relative_to(C.REPO)), site_packages.exists())

    print()
    print("--- 构建依赖（**仅 sdist 路径需要** backend）---")
    if args.artifact == "sdist":
        wheels = [C.fetch_wheel(pkg) for pkg in C.BUILD_DEP_PKGS]
        code, out = C.run(
            [python, "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir", *[str(w) for w in wheels]],
            cwd=C.SCRATCH,
            log=f"{args.venv_name}_builddeps",
            env=C.pip_env(),
        )
        C.say("构建依赖装入 venv（临时）", f"exit={code}", code == 0)
    else:
        C.say("wheel 路径**不装**构建依赖", "install venv 保持最小", True)

    print()
    print("--- D4 pip install（⛔ --no-index · 离线自证）---")
    # ⚠️ `--no-cache-dir`：本机 pip 的 wheel 缓存目录
    #    （`%LOCALAPPDATA%\pip\cache\wheels`）不可访问 ⇒ 构建出的 wheel 写不进去就会失败。
    cmd = [python, "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir"]
    if args.artifact == "sdist":
        cmd.append("--no-build-isolation")
    cmd.append(str(artifact))
    code, out = C.run(cmd, cwd=C.SCRATCH, log=f"{args.venv_name}_install", env=C.pip_env())
    C.say("pip install", f"exit={code}", code == 0)
    if code != 0:
        C.note(out[-1200:])
        return C.summary("D4–D8 干净安装")

    if args.artifact == "sdist":
        # `--no-build-isolation` 会把 backend 留在 venv 里 —— 那**不是**用户的最终环境。
        # ⇒ 装完即卸，手工还原「build isolation 之后」的样子（否则 `pip check` 会被
        #   `wheel` 的 `packaging` 依赖噪声污染 —— 实测确实如此）。
        code, out = C.run(
            [python, "-m", "pip", "uninstall", "-y", *C.BUILD_DEP_PKGS],
            cwd=C.SCRATCH,
            log=f"{args.venv_name}_builddeps_uninstall",
            env=C.pip_env(),
        )
        C.say("构建依赖已卸除（还原 build isolation）", f"exit={code}", code == 0)

    print()
    print("--- 环境最小性（D4 强化）---")
    code, out = C.run(
        [python, "-m", "pip", "list", "--format=json", "--disable-pip-version-check"],
        cwd=C.SCRATCH,
        log=f"{args.venv_name}_piplist",
        env=C.pip_env(),
    )
    installed: list[str] = []
    if code == 0 and "[" in out and "]" in out:
        installed = sorted(entry["name"] for entry in json.loads(out[out.index("[") : out.rindex("]") + 1]))
    C.say("venv 内分发 == {adaptive-agent-router, pip}", str(installed),
          installed == ["adaptive-agent-router", "pip"])

    print()
    print("--- RP-7 pip check ---")
    code, out = C.run(
        [python, "-m", "pip", "check"], cwd=C.SCRATCH, log=f"{args.venv_name}_pipcheck", env=C.pip_env()
    )
    C.say("pip check", (out.strip().splitlines()[-1] if out.strip() else ""), code == 0)

    print()
    print("--- 探针（中立 cwd · ⛔ 不在仓库根）---")
    expected = {
        "version": cfg["version"],
        "requires_python": cfg["requires-python"],
        "site_packages": str(site_packages),
        "modules_l1": C.module_names(C.REPO / "src"),
        "modules_l2": C.protected_module_names(),
    }
    C.say("L1 期望模块数（仓库 src/ · 68）", str(len(expected["modules_l1"])), len(expected["modules_l1"]) == 68)
    C.say("L2 期望模块数（registry protected · 28）", str(len(expected["modules_l2"])), len(expected["modules_l2"]) == 28)
    subset = set(expected["modules_l2"]) <= set(expected["modules_l1"])
    C.say("L2 ⊆ L1", str(subset), subset)

    probe = C.SCRATCH / f"_probe_{args.venv_name}_{args.artifact}.py"
    probe.write_text(PROBE.replace("__EXPECTED__", json.dumps(expected)), encoding="utf-8")
    code, out = C.run([python, str(probe)], cwd=C.SCRATCH, log=f"{args.venv_name}_{args.artifact}_probe")

    payload = None
    for line in out.splitlines():
        if line.startswith("PROBE:"):
            payload = json.loads(line[len("PROBE:") :])
    if payload is None:
        C.say("探针输出", "未取到 PROBE 行", False)
        C.note(out[-1200:])
        return C.summary("D4–D8 干净安装")

    C.say("探针解释器", payload["python"])
    for name, good in payload["checks"].items():
        C.say(name, str(payload["detail"].get(name))[:70], good)
    C.note(f"导入来源 = {payload['detail'].get('origin')}")
    return C.summary("D4–D8 干净安装")


if __name__ == "__main__":
    sys.exit(main())
