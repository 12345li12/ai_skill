"""`RelPkg` **D9 / D10 / D11** —— 全链驱动：构建 → 内容 → 双解释器安装 → 不变量。

- `D9`  多版本矩阵：**3.14 + 3.12**（本机仅此两个 ⇒ `Validated 2/5`；
        3.10 / 3.11 / 3.13 = **NOT VALIDATED**，⛔ 不得写成 5/5）
- `D10` `tests/**.py == 100` · registry `65/65`
- `D11` `src/` **零 diff**（对实现前捕获的 manifest）· ⛔ 无 `.egg-info` / `build/` 落入仓库

用法：`python release/run_all.py [--with-suite]`
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import _common as C  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
BASELINE = C.REPO / ".pytest-tmp/retry_prep/relpkg_src_baseline.json"
VALIDATED = ("3.14", "3.12")
NOT_VALIDATED = ("3.10", "3.11", "3.13")


def step(script: str, args: list[str] | None = None) -> int:
    print()
    print("#" * 72)
    print(f"# release/{script}")
    print("#" * 72)
    return subprocess.run(  # noqa: S603
        [sys.executable, str(HERE / script), *(args or [])], cwd=str(C.REPO), check=False
    ).returncode


def find_interpreters() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = [("3.14", sys.executable)]
    for tag in VALIDATED:
        if tag == "3.14":
            continue
        try:
            out = subprocess.run(  # noqa: S603
                ["py", f"-{tag}", "-c", "import sys;print(sys.executable)"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            continue
        path = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
        if out.returncode == 0 and path and pathlib.Path(path).exists():
            found.append((tag, path))
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-suite", action="store_true", help="额外跑冻结全量（较慢）")
    args = parser.parse_args()

    codes: dict[str, int] = {}
    codes["build"] = step("build_check.py")
    if codes["build"] != 0:
        print("\n❌ 构建失败 ⇒ 后续步骤跳过")
        return 1
    codes["wheel"] = step("wheel_check.py")
    codes["sdist"] = step("sdist_check.py")

    print()
    print("#" * 72)
    print("# D9 多版本矩阵（install_check.py）")
    print("#" * 72)
    interpreters = find_interpreters()
    print(f"  可用解释器 = {[tag for tag, _ in interpreters]}")
    print(f"  Validated  = {list(VALIDATED)}")
    print(f"  NOT VALIDATED = {list(NOT_VALIDATED)}  ← 本机无此解释器，⛔ 不得声称已验证")
    for tag, exe in interpreters:
        for artifact in ("wheel", "sdist"):
            key = f"install_{tag}_{artifact}"
            # ⚠️ venv 名必须**按 artifact 区分**：否则 sdist 那一遍会复用已装好 wheel 的 venv，
            #    "干净环境" 的前提当场失效，且两遍日志互相覆盖（首版就是这个缺陷）。
            codes[key] = step(
                "install_check.py",
                [
                    "--base-python",
                    exe,
                    "--venv-name",
                    f"install{tag.replace('.', '')}{artifact}",
                    "--artifact",
                    artifact,
                ],
            )

    print()
    print("#" * 72)
    print("# D10 / D11 不变量")
    print("#" * 72)
    ok = True

    tests_py = len([p for p in (C.REPO / "tests").rglob("*.py") if "__pycache__" not in str(p)])
    ok &= C.say("D10 tests/**.py == 100", str(tests_py), tests_py == 100)

    rows = C.registry_rows()
    mismatched = [
        rel
        for _tag, rel, size, sha in rows
        if not (C.REPO / rel).exists() or C.sha256(C.REPO / rel) != sha or (C.REPO / rel).stat().st_size != size
    ]
    ok &= C.say("D10 registry 身份", f"{len(rows) - len(mismatched)}/{len(rows)}", not mismatched and len(rows) == 65)

    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    # ⚠️ baseline 的 key 形如 `src/adaptive_router/x.py`（实现前按**仓库根**相对路径记录），
    #    而 `src_manifest(REPO/"src")` 的 key 相对 `src/` ⇒ 必须补回前缀再比
    #    （首版漏了这一步，报成 +68/-68 的**假偏离**）。
    current = {f"src/{key}": value for key, value in C.src_manifest(C.REPO / "src").items()}
    added = sorted(set(current) - set(baseline))
    removed = sorted(set(baseline) - set(current))
    changed = sorted(k for k in set(current) & set(baseline) if current[k] != baseline[k])
    ok &= C.say(
        "D11 src/ 零 diff",
        f"+{len(added)} / -{len(removed)} / ~{len(changed)}（基线 {len(baseline)} 件）",
        not added and not removed and not changed,
    )
    for name in (added + removed + changed)[:8]:
        C.note(f"偏离：{name}")

    for stray in ("src/adaptive_router.egg-info", "build", "dist"):
        exists = (C.REPO / stray).exists()
        ok &= C.say(f"⛔ 仓库内无构建残留 · {stray}", "存在 ❌" if exists else "不存在", not exists)

    if args.with_suite:
        print()
        print("--- 冻结全量（--with-suite）---")
        code = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pytest", "--junit-xml=.pytest-tmp/relpkg_suite.xml", "-q"],
            cwd=str(C.REPO),
            check=False,
        ).returncode
        ok &= C.say("D10 冻结全量", f"pytest exit={code}", code == 0)

    print()
    print("=" * 72)
    print("=== 步骤汇总 ===")
    for key, code in codes.items():
        print(f"  {'✅' if code == 0 else '❌'}  {key:<24} exit={code}")
    failed = [k for k, v in codes.items() if v != 0]
    print()
    print(f"⇒ 矩阵 Validated {len(interpreters)}/5 · NOT VALIDATED {len(NOT_VALIDATED)}"
          f"（{', '.join(NOT_VALIDATED)}）")
    print(f"⇒ 总判定：{'全部通过 ✅' if ok and not failed else f'失败 ❌ {failed}'}")
    return 0 if ok and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
