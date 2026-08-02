"""Unit tests for `scripts/status.py`'s `check()` — spec vs. reality, both ways.

Drives `check()` against synthetic `FeatureFacts` and a synthetic `FEATURES`
tuple, never the live spec: a real feature flipping `partial` -> `done` (or
gaining a scenario) must not break this file. `scripts/status.py` has no
`__init__.py` sibling to import through package machinery — the module itself
resolves `spec` the same way, by putting its own directory on `sys.path`
before importing — so this file does the same for `status` and `spec`.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import spec as spec_module  # noqa: E402 - path insert must run first
import status  # noqa: E402 - path insert must run first

Feature = spec_module.Feature
Status = spec_module.Status
Layer = spec_module.Layer
FeatureFacts = status.FeatureFacts


@pytest.fixture(autouse=True)
def _isolated_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point `has_written_reason`'s two search paths at an empty tmp tree.

    Without this, a test asserting "undocumented partial is flagged" would
    pass or fail depending on what happens to exist under the real
    `.specs/features/` and `docs/contracts/` at the moment it runs.
    """
    monkeypatch.setattr(status, "SPECS", tmp_path / ".specs" / "features")
    monkeypatch.setattr(status, "CONTRACTS", tmp_path / "docs" / "contracts")
    yield


def _write_reason(feature_id: str, *, as_contract: bool = False) -> None:
    """Create the one document `has_written_reason(feature_id)` needs to see."""
    if as_contract:
        path = status.CONTRACTS / f"{feature_id}.md"
    else:
        path = status.SPECS / feature_id / "spec.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# reason\n")


def _feature(feature_id: str, status_: Status, **overrides: object) -> Feature:
    fields: dict[str, object] = {
        "id": feature_id,
        "area": "fun",
        "title": feature_id,
        "milestone": "M2",
        "status": status_,
        "layer": Layer.GATEWAY,
    }
    fields.update(overrides)
    return Feature(**fields)  # type: ignore[arg-type]


def test_overstatement_still_caught() -> None:
    """The original job: `done` with no ported scenario is still a finding."""
    features = (_feature("fun_x", Status.DONE),)
    facts: dict[str, FeatureFacts] = {}

    problems = _check_with(features, facts)

    assert any("marked done but has no scenario" in p for p in problems)


def test_overstatement_zero_passed_still_caught() -> None:
    features = (_feature("fun_x", Status.DONE),)
    facts = {"fun_x": FeatureFacts(v2_scenarios=["a scenario"], passed=0, bound=True)}

    problems = _check_with(features, facts)

    assert any("marked done but no scenario executed green" in p for p in problems)


def test_understatement_newly_caught() -> None:
    """A `planned` feature whose scenarios are all green looks complete."""
    features = (_feature("fun_x", Status.PLANNED),)
    facts = {
        "fun_x": FeatureFacts(v2_scenarios=["a", "b"], passed=2, failed=0, skipped=0, bound=True)
    }

    problems = _check_with(features, facts)

    assert any("looks complete" in p and "fun_x" in p for p in problems)


def test_documented_partial_is_exempt_from_looks_complete() -> None:
    """fun_battle's shape: partial, documented, and (hypothetically) all green."""
    _write_reason("fun_x")
    features = (_feature("fun_x", Status.PARTIAL, notes="see .specs/features/fun_x/spec.md"),)
    facts = {"fun_x": FeatureFacts(v2_scenarios=["a"], passed=1, failed=0, skipped=0, bound=True)}

    problems = _check_with(features, facts)

    assert not any("looks complete" in p for p in problems)
    assert not any("undocumented" in p for p in problems)


def test_undocumented_partial_is_flagged() -> None:
    """Same status, no spec.md and no contract - the omission is the finding."""
    features = (_feature("fun_x", Status.PARTIAL),)
    facts: dict[str, FeatureFacts] = {}  # no scenarios at all - doc gap alone must fire

    problems = _check_with(features, facts)

    assert any("undocumented" in p and "fun_x" in p for p in problems)
    assert not any("looks complete" in p for p in problems)


def test_undocumented_blocked_is_flagged_via_contract_too() -> None:
    """The other accepted document: docs/contracts/<id>.md, not just spec.md."""
    features = (_feature("fun_x", Status.BLOCKED),)
    facts: dict[str, FeatureFacts] = {}

    problems = _check_with(features, facts)
    assert any("undocumented" in p for p in problems)

    _write_reason("fun_x", as_contract=True)
    problems = _check_with(features, facts)
    assert not any("undocumented" in p for p in problems)


def test_skip_containing_feature_not_reported_complete() -> None:
    """Skipped != green: the already-gathered skipped count must gate this."""
    features = (_feature("fun_x", Status.PLANNED),)
    facts = {
        "fun_x": FeatureFacts(v2_scenarios=["a", "b"], passed=1, failed=0, skipped=1, bound=True)
    }

    problems = _check_with(features, facts)

    assert not any("looks complete" in p for p in problems)


def test_unbound_feature_file_is_flagged() -> None:
    """A .feature file with scenarios that no qa/test_*.py binds never runs."""
    features: tuple[Feature, ...] = ()
    facts = {"orphan_x": FeatureFacts(v2_scenarios=["a scenario"], bound=False)}

    problems = _check_with(features, facts)

    assert any("orphan_x" in p and "never runs" in p for p in problems)


def test_bound_feature_file_is_not_flagged() -> None:
    features: tuple[Feature, ...] = ()
    facts = {"fun_x": FeatureFacts(v2_scenarios=["a scenario"], bound=True)}

    problems = _check_with(features, facts)

    assert not any("never runs" in p for p in problems)


def test_strict_inventory_off_by_default() -> None:
    """The v1-inventory check must not fire unless explicitly requested."""
    features: tuple[Feature, ...] = ()
    problems = _check_with(features, {})

    assert not any("spec.py has no row for it" in p for p in problems)


def test_strict_inventory_flags_missing_ids_when_enabled() -> None:
    features: tuple[Feature, ...] = ()

    problems = _check_with(features, {}, strict_inventory=True)

    assert len(problems) >= len(status.MISSING_V1_INVENTORY)
    assert all(any(feature_id in p for p in problems) for feature_id in status.MISSING_V1_INVENTORY)


def test_strict_inventory_quiets_once_id_is_known() -> None:
    features = (_feature(status.MISSING_V1_INVENTORY[0], Status.PLANNED),)

    problems = _check_with(features, {}, strict_inventory=True)

    assert not any(status.MISSING_V1_INVENTORY[0] in p for p in problems)


def _check_with(
    features: tuple[Feature, ...],
    facts: dict[str, FeatureFacts],
    *,
    strict_inventory: bool = False,
) -> list[str]:
    """Run `status.check()` against a synthetic `FEATURES`, restored after."""
    original = status.FEATURES
    status.FEATURES = features  # type: ignore[assignment]
    try:
        return status.check(facts, strict_inventory=strict_inventory)
    finally:
        status.FEATURES = original
