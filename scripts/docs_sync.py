"""Sync the docs site with the spec and the last measured test run.

Two artefacts, one source of truth:

    docs/site/content/progress.json          every number the site renders
    docs/site/content/docs/features/*.mdx    one page per feature

The rule that makes this safe to run at any time: **frontmatter is generated,
the body is yours.** A feature page's `status`, `milestone`, `triggers` and
scenario counts come from `scripts/spec.py` and `scripts/status.py` and are
rewritten on every run; everything below the closing `---` is hand-written
prose this script only ever creates (as a stub) and never edits.

    python scripts/cb.py docs-sync              # regenerate (runs the suite)
    python scripts/cb.py docs-sync --no-tests   # fast: skip the test run
    python scripts/cb.py docs-sync --check      # fail if anything is stale

`--check` is what CI runs. Without it, a status typed into an .mdx by hand
would sit there disagreeing with the spec, and the site's whole claim — that
its numbers are measured rather than asserted — would quietly stop being true.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import status as status_report
from spec import DEFECTS, FEATURES, MILESTONES, Feature, Status

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "docs" / "site"
CONTENT = SITE / "content" / "docs"
FEATURE_DIR = CONTENT / "features"
PROGRESS_JSON = SITE / "content" / "progress.json"
CONTRACTS = ROOT / "docs" / "contracts"

#: Areas in the order a reader should meet them: the survival core first, the
#: platform it stands on last.
AREA_ORDER = ("core", "util", "fun", "platform")
AREA_TITLES = {
    "core": "Core moderation",
    "util": "Utility",
    "fun": "Fun",
    "platform": "Platform",
}


# --------------------------------------------------------------------- facts


def commit() -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip() or "unknown"


def contract_for(feature: Feature) -> str | None:
    path = CONTRACTS / f"{feature.id}.md"
    return f"docs/contracts/{feature.id}.md" if path.is_file() else None


def build_progress(
    facts: dict[str, status_report.FeatureFacts], summary: str, problems: list[str]
) -> dict[str, object]:
    counts = {s.value: sum(1 for f in FEATURES if f.status is s) for s in Status}
    spec_total = sum(len(f.spec_scenarios) for f in facts.values())
    ported = sum(len(f.v2_scenarios) for f in facts.values() if f.spec_scenarios)
    new_specs = sum(len(f.v2_scenarios) for f in facts.values() if not f.spec_scenarios)
    # Coverage is counted in *spec files*, not scenarios. v2 routinely writes
    # more scenarios for a feature than v1 specified (edge cases v1 never wrote
    # down), so "95 of 63 ported" is both true and useless — the honest
    # headline is how many of v1's specs are executable at all.
    spec_files = [f for f in facts.values() if f.spec_scenarios]
    covered = [f for f in spec_files if f.v2_scenarios]

    features: list[dict[str, object]] = []
    for feature in FEATURES:
        fact = facts.get(feature.id, status_report.FeatureFacts())
        features.append(
            {
                "id": feature.id,
                "title": feature.title,
                "area": feature.area,
                "layer": feature.layer.value,
                "milestone": feature.milestone,
                "status": feature.status.value,
                "triggers": list(feature.triggers),
                "v1_source": feature.v1_source,
                "notes": feature.notes,
                "contract": contract_for(feature),
                "scenarios": {
                    "spec": len(fact.spec_scenarios),
                    "ported": len(fact.v2_scenarios),
                    "green": fact.passed,
                    "failing": fact.failed,
                },
                "url": f"/docs/features/{feature.id}",
            }
        )

    ledger = [
        {
            "stem": stem,
            "spec": len(fact.spec_scenarios),
            "ported": len(fact.v2_scenarios),
            "green": fact.passed,
            "failing": fact.failed,
            "state": _ledger_state(fact),
        }
        for stem, fact in sorted(facts.items())
        if fact.spec_scenarios or fact.v2_scenarios
    ]

    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d"),
        "commit": commit(),
        "test_summary": summary,
        "totals": {
            "features": len(FEATURES),
            "done": counts[Status.DONE.value],
            "partial": counts[Status.PARTIAL.value],
            "planned": counts[Status.PLANNED.value],
            "blocked": counts[Status.BLOCKED.value],
            "spec_scenarios": spec_total,
            "ported": ported,
            "spec_files": len(spec_files),
            "spec_files_covered": len(covered),
            "new_specs": new_specs,
            "green": sum(f.passed for f in facts.values()),
            "failing": sum(f.failed for f in facts.values()),
        },
        "milestones": [
            {
                "id": milestone,
                "title": title,
                "done": sum(
                    1 for f in FEATURES if f.milestone == milestone and f.status is Status.DONE
                ),
                "total": sum(1 for f in FEATURES if f.milestone == milestone),
            }
            for milestone, title in MILESTONES.items()
        ],
        "features": features,
        "scenarios": ledger,
        "defects": [
            {"id": key, "text": text, "addressed": addressed}
            for key, (text, addressed) in DEFECTS.items()
        ],
        "problems": problems,
    }


def _ledger_state(fact: status_report.FeatureFacts) -> str:
    if not fact.spec_scenarios:
        return "v2-only spec"
    if not fact.v2_scenarios:
        return "not ported"
    if fact.failed:
        return "failing"
    if len(fact.v2_scenarios) < len(fact.spec_scenarios):
        return "partial"
    if fact.passed:
        return "green"
    return "no step definitions"


# ----------------------------------------------------------------- mdx pages


def _yaml_scalar(value: str) -> str:
    """Quote anything YAML would misread. Deliberately conservative: a title
    starting with `/` or containing `:` is common here (commands, `v1 -> v2`),
    and an unquoted one silently becomes a different document."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def frontmatter(feature: Feature) -> str:
    """Only what the *spec* says — deliberately no scenario counts.

    Those move with every test run, so putting them here would make
    `docs-sync --check` fail on a branch that changed nothing, which is how a
    generated-artefact check ends up being deleted. They live in
    `progress.json` instead, which the check ignores, and the page renders them
    through `<FeatureHeader />` all the same.
    """
    lines = [
        "---",
        "# Generated by `python scripts/cb.py docs-sync` from scripts/spec.py.",
        "# Edit the prose below, not this block — the sync rewrites it and",
        "# `docs-sync --check` fails CI when it drifts.",
        f"title: {_yaml_scalar(feature.title)}",
        f"description: {_yaml_scalar(feature.notes or f'{feature.area} · {feature.milestone}')}",
        f"status: {feature.status.value}",
        f"milestone: {feature.milestone}",
        f"area: {feature.area}",
        f"layer: {feature.layer.value}",
    ]
    if feature.triggers:
        rendered = ", ".join(_yaml_scalar(t) for t in feature.triggers)
        lines.append(f"triggers: [{rendered}]")
    if feature.v1_source:
        lines.append(f"v1_source: {_yaml_scalar(feature.v1_source)}")
    contract = contract_for(feature)
    if contract:
        lines.append(f"contract: {_yaml_scalar(contract)}")
    lines.append("---")
    return "\n".join(lines)


def stub_body(feature: Feature) -> str:
    """What a human is invited to fill in. Deliberately three questions rather
    than a blank page: the ones a reader of a ported feature actually has, and
    the ones the spec cannot answer on its own."""
    contract = contract_for(feature)
    lines = [
        "",
        f'<FeatureHeader id="{feature.id}" />',
        "",
        "## What it does",
        "",
        "{/* One paragraph, from the user's side. What happens in the chat? */}",
        "",
        "## Behaviour that must not change",
        "",
        "{/* v1 compatibility is not negotiable: the aliases, the wording, the",
        "    permissions. Anything a group would notice if it moved. */}",
        "",
    ]
    if contract:
        lines += [
            f"The full behaviour contract lives in [`{contract}`](https://github.com/"
            f"Cookiebot-Team/cookiebot-telegram-bot/blob/main/{contract}).",
            "",
        ]
    lines += [
        "## How to verify it",
        "",
        "{/* Which scenario file, and what to do by hand in the sandbox. */}",
        "",
    ]
    return "\n".join(lines)


def split_body(text: str) -> str:
    """Everything after the frontmatter block. A file with no frontmatter is
    treated as all body, so a page hand-created without one is not silently
    truncated on the next sync."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + len("\n---") :]


def feature_page(feature: Feature) -> tuple[Path, str]:
    path = FEATURE_DIR / f"{feature.id}.mdx"
    body = split_body(path.read_text(encoding="utf-8")) if path.is_file() else stub_body(feature)
    if not body.startswith("\n"):
        body = "\n" + body
    return path, frontmatter(feature) + body


def features_meta() -> tuple[Path, str]:
    """The sidebar for `features/`, grouped by area with the same section names
    the progress board uses. Generated because the page list is the spec's, and
    a hand-kept copy would go stale the first time a feature is added."""
    # `index` first and by hand: the overview page is written, not generated,
    # and a sidebar that lists it last reads as if the per-feature pages came
    # before the thing that explains them.
    pages: list[str] = ["index"]
    for area in AREA_ORDER:
        members = [f for f in FEATURES if f.area == area]
        if not members:
            continue
        pages.append(f"---{AREA_TITLES[area]}---")
        pages.extend(f.id for f in sorted(members, key=lambda f: (f.milestone, f.id)))
    meta = {"title": "Features", "description": "Every feature, one page each", "pages": pages}
    return FEATURE_DIR / "meta.json", json.dumps(meta, indent=2) + "\n"


# --------------------------------------------------------------------- write


def plan(with_tests: bool) -> dict[Path, str]:
    facts, summary = status_report.gather(with_tests=with_tests)
    problems = status_report.check(facts)

    out: dict[Path, str] = {
        PROGRESS_JSON: json.dumps(build_progress(facts, summary, problems), indent=2) + "\n"
    }
    for feature in FEATURES:
        path, text = feature_page(feature)
        out[path] = text
    path, text = features_meta()
    out[path] = text
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit non-zero if anything is stale")
    parser.add_argument("--no-tests", action="store_true", help="skip the test run (fast)")
    args = parser.parse_args()

    FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    files = plan(with_tests=not args.no_tests)

    # `--check` ignores `progress.json`: the test summary and scenario counts
    # move with every run of the suite, and a check that failed on them would
    # fail on a green branch that changed nothing — the classic
    # generated-artefact check that teams end up deleting. What must not drift
    # is what the *spec* says: status, milestone, triggers, the page set.
    volatile = {PROGRESS_JSON}
    stale = [
        path
        for path, text in files.items()
        if path not in volatile and (not path.is_file() or path.read_text(encoding="utf-8") != text)
    ]

    if args.check:
        if stale:
            print(f"{len(stale)} docs page(s) out of sync with scripts/spec.py:")
            for path in stale:
                print(f"  - {path.relative_to(ROOT)}")
            print("\nrun: python scripts/cb.py docs-sync")
            return 1
        print("docs site is in sync with the spec")
        return 0

    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")

    print(f"synced {len(files)} file(s) into {SITE.relative_to(ROOT)}")
    if stale:
        print(f"  {len(stale)} feature page(s) updated from the spec")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
