"""Measure the spec against reality, and report the difference.

The spec (`scripts/spec.py`) says what should be true. This script measures what
*is* true — QA scenarios in the source repo, scenario files in v2, which step
definitions bind them, and the result of an actual test run — then reports the
difference. A feature claiming `done` with no passing scenario is a finding, not
a footnote.

    python scripts/status.py                    # print the findings
    python scripts/status.py --check             # exit non-zero on any inconsistency
    python scripts/status.py --no-tests          # skip the test run (fast, less complete)
    python scripts/status.py --json              # machine-readable facts
    python scripts/status.py --strict-inventory  # also require every v1 feature to have a spec.py row

The check used to run one direction only: a feature marked `done` with no
ported, passing scenario. That catches overstatement but not the opposite —
a feature quietly finished and never promoted out of `partial`/`planned`. It
now also flags:

  * a feature whose scenarios are all green (no failures, no skips) and, where
    the QA repo defines scenarios for it, fully ported — reported as looking
    complete, naming the status that undersells it. A `partial`/`blocked`
    feature is exempt from *this* only if it carries a written reason
    (`.specs/features/<id>/spec.md` or `docs/contracts/<id>.md`) — `fun_battle`
    is the example: two of its three v1 shapes are genuinely blocked on a GCS
    bucket export, and its QA scenario is `pytest.skip()`-ed rather than
    ported, which the skipped-count guard below also catches independently.
  * any `partial`/`blocked` feature with no such document at all, pass or no
    pass — an undocumented partial reads as neglect, not as a recorded
    decision, regardless of whether its scenarios happen to be green.
  * a `.feature` file in `qa/features/` that no `qa/test_*.py` binds via
    `scenarios(...)` — it collects, and then never runs. pytest reports
    nothing wrong because nothing was asked to run.

`--strict-inventory` is a separate, opt-in flag: it checks a fixed list of v1
features (`MISSING_V1_INVENTORY`) that have real code and zero row in
`scripts/spec.py` today (feature-map.mdx §4) against the live spec. It is off
by default because the fix is a single pending edit
(`.specs/features/_pending/missing-spec-rows.md`) awaiting review, not a
per-feature decision — turning it on unconditionally would fail `--check`
for a gap that is already tracked. Once those rows land the check goes quiet
on its own: the ids it looks for stop being missing.

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
CONTRACTS = ROOT / "docs" / "contracts"
SPECS = ROOT / ".specs" / "features"

_SCENARIO = re.compile(r"^\s*Scenario(?: Outline)?:\s*(.+?)\s*$", re.M)
_BINDS = re.compile(r"""scenarios\(\s*["']([^"']+)["']""")
_RESULT = re.compile(
    r"^(?P<path>[^\s:]+)::(?P<test>\S+)\s+(?P<outcome>PASSED|FAILED|ERROR|SKIPPED)"
)

#: v1 features with shipped code (feature-map.mdx §4) and no row in FEATURES at
#: all — so no check, strict or otherwise, currently sees them. The exact ids
#: the pending rows in .specs/features/_pending/missing-spec-rows.md will use;
#: once those rows land, `check(..., strict_inventory=True)` goes quiet on its
#: own because every id below stops being missing from `known`.
MISSING_V1_INVENTORY: tuple[str, ...] = (
    "x_age_guess",
    "x_gender_guess",
    "x_unearth",
    "x_fortune_cookie",
    "x_image_search",
    "x_drawing_idea",
    "x_analysis",
    "x_sticker_autoreply",
)


@dataclass
class FeatureFacts:
    spec_scenarios: list[str] = field(default_factory=list)  # from the QA repo
    v2_scenarios: list[str] = field(default_factory=list)  # from qa/features
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    bound: bool = False  # some qa/test_*.py binds this stem via scenarios(...)

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
        facts[stem].bound = True
    return facts, summary


def has_written_reason(feature_id: str) -> bool:
    """A `partial`/`blocked` status is a decision, not neglect, when it points
    somewhere: `.specs/features/<id>/spec.md` for a slice still being designed,
    `docs/contracts/<id>.md` for one whose behaviour is already pinned down."""
    return (SPECS / feature_id / "spec.md").is_file() or (CONTRACTS / f"{feature_id}.md").is_file()


def check(facts: dict[str, FeatureFacts], *, strict_inventory: bool = False) -> list[str]:
    """Every way the spec and reality can disagree, in both directions.

    Overstatement — `done` without proof — was the original job and is still
    the first half below, unchanged. The rest catches the opposite: a feature
    that quietly finished and was never promoted, a `partial`/`blocked` status
    nobody wrote a reason for, and a `.feature` file nothing runs.
    """
    problems: list[str] = []
    known = {f.id for f in FEATURES}

    for stem in facts:
        if stem in known:
            continue
        if facts[stem].spec_scenarios:
            problems.append(f"{stem}: QA repo has a spec, but scripts/spec.py has no row for it")

    for stem, fact in facts.items():
        if fact.v2_scenarios and not fact.bound:
            problems.append(
                f"{stem}: qa/features/{stem}.feature has scenarios but no qa/test_*.py "
                f"binds it via scenarios(...) - it collects and never runs"
            )

    for feature in FEATURES:
        fact = facts.get(feature.id, FeatureFacts())

        # -------------------------------------------------------- overstatement
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

        # ------------------------------------------------------- understatement
        documented = feature.status in (Status.PARTIAL, Status.BLOCKED) and has_written_reason(
            feature.id
        )
        if feature.status in (Status.PARTIAL, Status.BLOCKED) and not documented:
            problems.append(
                f"{feature.id}: marked {feature.status.value} with no "
                f".specs/features/{feature.id}/spec.md or docs/contracts/{feature.id}.md - "
                f"the state is undocumented, which reads as neglect rather than a decision"
            )
        # A partial/blocked feature with a written reason may still be exactly
        # as far along as it should be (fun_battle: two of three v1 shapes are
        # blocked on a bucket export the passing scenarios don't cover) - that
        # reason is the guardrail against a false "should be done" report.
        # Everything else this green, with nothing failing or skipped, is one.
        if (
            feature.status is not Status.DONE
            and not documented
            and fact.v2_scenarios
            and fact.passed > 0
            and fact.failed == 0
            and fact.skipped == 0  # a skip is not a pass - see fun_battle's QA scenario
            and len(fact.v2_scenarios) >= len(fact.spec_scenarios)
        ):
            problems.append(
                f"{feature.id}: {fact.passed} scenario(s) passing, none failing or "
                f"skipped, but status is still {feature.status.value} - looks complete"
            )

    if strict_inventory:
        for feature_id in MISSING_V1_INVENTORY:
            if feature_id not in known:
                problems.append(
                    f"{feature_id}: shipped in v1 (feature-map.mdx §4) but scripts/spec.py "
                    f"has no row for it - see .specs/features/_pending/missing-spec-rows.md"
                )

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit non-zero on any inconsistency")
    parser.add_argument("--no-tests", action="store_true", help="skip the test run")
    parser.add_argument("--json", action="store_true", help="emit machine-readable facts")
    parser.add_argument(
        "--strict-inventory",
        action="store_true",
        help=(
            "also require every v1 feature in MISSING_V1_INVENTORY to have a scripts/spec.py "
            "row. Off by default: it fails today against the live tree until the pending rows "
            "in .specs/features/_pending/missing-spec-rows.md are applied."
        ),
    )
    args = parser.parse_args()

    facts, summary = gather(with_tests=not args.no_tests)
    problems = check(facts, strict_inventory=args.strict_inventory)

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
        print("no inconsistencies: status and reality agree in both directions")
    print("\nrender it: python scripts/cb.py docs-sync   (then cb.py docs)")
    return 1 if (args.check and problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
