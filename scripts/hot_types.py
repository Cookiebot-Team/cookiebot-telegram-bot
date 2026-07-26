"""Type-coverage audit for the Cython-compiled modules.

`setup.py` compiles the hot modules with `annotation_typing = True`, so Cython
lowers PEP 484 hints to C types. That makes annotations load-bearing in exactly
these files and nowhere else: an unannotated local stays a generic `PyObject*`
and every operation on it goes back through the interpreter, which is the cost
the compilation exists to remove.

Ruff's ANN rules stop at signatures — no linter checks whether a *local* is
typed. This does, for the compiled modules only:

    python scripts/hot_types.py            report
    python scripts/hot_types.py --check    exit 1 if anything is untyped

Deliberately not applied to the rest of the tree: outside these files an
annotation is documentation, and demanding one for every local would be noise
with no runtime effect at all.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_SRC = ROOT / "packages" / "cb-core" / "src"
SETUP = ROOT / "packages" / "cb-core" / "setup.py"

# Names bound by a `with`/`for`/`except` clause cannot carry an inline annotation
# in Python syntax; Cython infers them from the iterable or context manager, so
# they are not reported.
_UNANNOTATABLE = frozenset({"with", "for", "except"})


@dataclass(slots=True)
class Finding:
    module: str
    line: int
    kind: str
    name: str


@dataclass(slots=True)
class ModuleReport:
    module: str
    functions: int = 0
    typed_functions: int = 0
    locals_total: int = 0
    locals_typed: int = 0
    findings: list[Finding] = field(default_factory=list)
    exemptions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings


def compiled_modules() -> list[Path]:
    """Read the list straight out of setup.py rather than duplicating it.

    The benchmark gate adds and removes modules from `HOT_MODULES` — textmatch
    and captcha were both dropped by it — and a second copy of that list here
    would quietly audit files that are no longer compiled.
    """
    tree = ast.parse(SETUP.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "HOT_MODULES" not in targets or not isinstance(node.value, ast.List):
            continue
        return [
            (SETUP.parent / element.value).resolve()
            for element in node.value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
    raise SystemExit(f"no HOT_MODULES list found in {SETUP}")


def _annotated_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Names the function already declares a type for: args and `x: int` locals."""
    named: set[str] = set()
    args = fn.args
    for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
        if arg.annotation is not None:
            named.add(arg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            named.add(node.target.id)
    return named


def _exempt_lines(source: str) -> dict[int, str]:
    """Lines carrying `# hot-types: ignore <reason>`, mapped to their reason.

    Some names cannot be lowered to a C type no matter what is written down — a
    Rust extension object, for instance. Forcing an annotation there would
    satisfy this script without removing a single interpreter round trip, so the
    escape hatch requires a reason and the reason is printed in the report. An
    exemption that stops being true is then visible rather than invisible.
    """
    exempt: dict[int, str] = {}
    for offset, line in enumerate(source.splitlines(), start=1):
        marker = "# hot-types: ignore"
        if marker in line:
            exempt[offset] = line.split(marker, 1)[1].strip() or "(no reason given)"
    return exempt


def audit(path: Path) -> ModuleReport:
    module = path.relative_to(CORE_SRC).as_posix()
    report = ModuleReport(module=module)
    source = path.read_text()
    exempt = _exempt_lines(source)
    tree = ast.parse(source)

    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        report.functions += 1

        args = fn.args
        every_arg = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        missing_args = [
            a.arg for a in every_arg if a.annotation is None and a.arg not in {"self", "cls"}
        ]
        signature_ok = not missing_args and fn.returns is not None
        report.typed_functions += int(signature_ok)
        for name in missing_args:
            report.findings.append(Finding(module, fn.lineno, "argument", f"{fn.name}({name})"))
        if fn.returns is None:
            report.findings.append(Finding(module, fn.lineno, "return", fn.name))

        declared = _annotated_names(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name) or target.id == "_":
                    continue
                report.locals_total += 1
                # The marker may sit on the assignment line or anywhere in the
                # comment block immediately above it, so a justification that
                # needs a few lines reads naturally.
                reason = next(
                    (
                        exempt[line]
                        for line in range(node.lineno, node.lineno - 5, -1)
                        if line in exempt
                    ),
                    None,
                )
                if target.id in declared or reason is not None:
                    report.locals_typed += 1
                    if reason is not None:
                        report.exemptions.append(f"{module}:{node.lineno}  {target.id}  {reason}")
                else:
                    report.findings.append(
                        Finding(module, node.lineno, "local", f"{fn.name}: {target.id}")
                    )
                    declared.add(target.id)  # report each name once per function

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 when anything is untyped")
    args = parser.parse_args()

    reports = [audit(path) for path in compiled_modules()]
    findings = [f for r in reports for f in r.findings]

    width = max((len(r.module) for r in reports), default=10)
    print(f"{'module':<{width}}  {'functions':>9}  {'locals':>12}")
    for report in reports:
        fns = f"{report.typed_functions}/{report.functions}"
        loc = f"{report.locals_typed}/{report.locals_total}"
        print(f"{report.module:<{width}}  {fns:>9}  {loc:>12}")

    exemptions = [e for r in reports for e in r.exemptions]
    if exemptions:
        print("\nexempted, with reason:")
        for entry in exemptions:
            print(f"  {entry}")

    if findings:
        print(f"\n{len(findings)} untyped in compiled modules:")
        for f in findings:
            print(f"  {f.module}:{f.line}  {f.kind}  {f.name}")
        if args.check:
            print(
                "\nThese are compiled with annotation_typing=True: an untyped name stays a "
                "PyObject* and its operations go back through the interpreter."
            )
            return 1
    else:
        print("\nevery function and local in the compiled modules is typed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
