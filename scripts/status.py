"""Measure the spec against reality, and report the difference.

The spec (`scripts/spec.py`) says what should be true. This script measures what
*is* true — QA scenarios in the source repo, scenario files in v2, which step
definitions bind them, and the result of an actual test run — then reports the
difference. A feature claiming `done` with no passing scenario is a finding, not
a footnote.

    python scripts/status.py             # print the findings
    python scripts/status.py --check     # exit non-zero on any inconsistency
    python scripts/status.py --no-tests  # skip the test run (fast, less complete)
    python scripts/status.py --json      # machine-readable facts

Rendering lives elsewhere: `scripts/docs_sync.py` turns these same facts into
the documentation site's progress board (`docs/site`), which is the report a
human reads. This module is the measurement, and `gather()`/`check()` are what
that script imports.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from spec import FEATURES, Status

ROOT = Path(__file__).resolve().parent.parent
QA_REPO = ROOT.parent / "Cookiebot-QA" / "features"
V2_FEATURES = ROOT / "qa" / "features"
V2_STEPS = ROOT / "qa"

_SCENARIO = re.compile(r"^\s*Scenario(?: Outline)?:\s*(.+?)\s*$", re.M)
_BINDS = re.compile(r"""scenarios\(\s*["']([^"']+)["']""")
_RESULT = re.compile(
    r"^(?P<path>[^\s:]+)::(?P<test>\S+)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED)"
)


@dataclass
class FeatureFacts:
    spec_scenarios: list[str] = field(default_factory=list)  # from the QA repo
    v2_scenarios: list[str] = field(default_factory=list)  # from qa/features
    passed: int = 0
    failed: int = 0
    skipped: int = 0

    @property
    def executed(self) -> int:
        return self.passed + self.failed + self.skipped


def read_scenarios(directory: Path) -> dict[str, list[str]]:
    if not directory.is_dir():
        return {}
    return {f.stem: _SCENARIO.findall(f.read_text()) for f in sorted(directory.glob("*.feature"))}


def step_bindings() -> dict[str, str]:
    """qa/test_x.py -> the feature stem it binds, read from its `scenarios(...)` call."""
    out: dict[str, str] = {}
    for f in sorted(V2_STEPS.glob("test_*.py")):
        match = _BINDS.search(f.read_text())
        if match:
            out[f.name] = Path(match.group(1)).stem
    return out


def run_tests() -> tuple[dict[str, collections.Counter], str]:
    """Run the offline suite; return per-file outcome counters and the summary line."""
    proc = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            "-v",
            "--tb=no",
            # The outcome regex below matches on raw text. Terminals that export
            # FORCE_COLOR make pytest colourise even a piped run, and every line
            # then arrives wrapped in escape codes — which reads as "no scenario
            # executed green" for features that are in fact passing.
            "--color=no",
            "-p",
            "no:cacheprovider",
            "-m",
            "not integration",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    per_file: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for line in proc.stdout.splitlines():
        m = _RESULT.match(line.strip())
        if m:
            per_file[Path(m.group("path")).name][m.group("outcome")] += 1
    summary = ""
    for line in reversed(proc.stdout.splitlines()):
        if " passed" in line or " failed" in line or " error" in line:
            summary = line.strip().strip("= ")
            break
    return per_file, summary


def gather(with_tests: bool) -> tuple[dict[str, FeatureFacts], str]:
    spec_scen = read_scenarios(QA_REPO)
    v2_scen = read_scenarios(V2_FEATURES)
    bindings = step_bindings()
    per_file, summary = run_tests() if with_tests else ({}, "not run")

    facts: dict[str, FeatureFacts] = collections.defaultdict(FeatureFacts)
    for stem, scenarios in spec_scen.items():
        facts[stem].spec_scenarios = scenarios
    for stem, scenarios in v2_scen.items():
        facts[stem].v2_scenarios = scenarios
    for test_file, stem in bindings.items():
        counter = per_file.get(test_file, collections.Counter())
        facts[stem].passed += counter["PASSED"]
        facts[stem].failed += counter["FAILED"] + counter["ERROR"]
        facts[stem].skipped += counter["SKIPPED"]
    return facts, summary


def check(facts: dict[str, FeatureFacts]) -> list[str]:
    """Every way the spec and reality can disagree."""
    problems: list[str] = []
    known = {f.id for f in FEATURES}

    for stem in facts:
        if stem in known:
            continue
        if facts[stem].spec_scenarios:
            problems.append(f"{stem}: QA repo has a spec, but scripts/spec.py has no row for it")

    for feature in FEATURES:
        fact = facts.get(feature.id, FeatureFacts())
        if feature.status is Status.DONE and feature.area != "platform":
            if not fact.v2_scenarios:
                problems.append(f"{feature.id}: marked done but has no scenario in qa/features")
            elif fact.passed == 0:
                problems.append(f"{feature.id}: marked done but no scenario executed green")
        if fact.failed:
            problems.append(f"{feature.id}: {fact.failed} scenario(s) failing")
        if feature.status is Status.DONE and fact.spec_scenarios:
            missing = len(fact.spec_scenarios) - len(fact.v2_scenarios)
            if missing > 0:
                problems.append(
                    f"{feature.id}: marked done but {missing} of "
                    f"{len(fact.spec_scenarios)} QA scenarios are not ported"
                )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit non-zero on any inconsistency")
    parser.add_argument("--no-tests", action="store_true", help="skip the test run")
    parser.add_argument("--json", action="store_true", help="emit machine-readable facts")
    args = parser.parse_args()

    facts, summary = gather(with_tests=not args.no_tests)
    problems = check(facts)

    if args.json:
        print(
            json.dumps(
                {
                    "summary": summary,
                    "problems": problems,
                    "features": {
                        f.id: {"status": f.status.value, "milestone": f.milestone} for f in FEATURES
                    },
                    "scenarios": {
                        k: {
                            "spec": len(v.spec_scenarios),
                            "ported": len(v.v2_scenarios),
                            "passed": v.passed,
                            "failed": v.failed,
                        }
                        for k, v in sorted(facts.items())
                    },
                },
                indent=2,
            )
        )
        return 1 if (args.check and problems) else 0

    print(f"tests: {summary}")
    print(f"features: {len(FEATURES)} in the spec")
    if problems:
        print(f"\n{len(problems)} consistency finding(s):")
        for p in problems:
            print(f"  - {p}")
    else:
        print("no inconsistencies: every done feature has a ported, passing scenario")
    print("\nrender it: python scripts/cb.py docs-sync   (then cb.py docs)")
    return 1 if (args.check and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
