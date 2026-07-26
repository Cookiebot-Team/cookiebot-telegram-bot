"""The pytest plugin: a sandbox, a client, and one scenario per test.

Registered as a `pytest11` entry point, so installing this package is the
whole integration — a suite gets these fixtures without importing anything.

    def test_rules_answers(sandbox, sandbox_bot_id):
        chat = sandbox.create_chat("rules test")
        user = sandbox.create_user("Ana", "ana")
        sandbox.join(chat["id"], user["id"])
        since = len(sandbox.state()["api_calls"])
        sandbox.send_message(chat["id"], user["id"], text="/rules")
        wait_for(
            lambda: next(iter(calls_to(sandbox.state(), "sendMessage", since)), None),
            timeout=10, description="answer /rules",
        )

Mark what a test is checking and the whole run becomes groupable:

    @pytest.mark.feature("rules")
    def test_rules_answers(sandbox): ...

The `sandbox_scenario` fixture is autouse: every test opens a scenario before
any other function-scoped fixture runs, so even the traffic a *fixture*
generates while building the world is tagged with the test that caused it, and
closes it with that test's real outcome — including a test that blew up in
setup, which from the sandbox's point of view is not a scenario that passed.

Two ways to get a server:

  --sandbox-url=http://…   point at one you started yourself
  (default)                the plugin starts one per session, on a free port

Opt out of the managed server in a suite that needs its own wiring (a bot
subprocess that must be started *after* the sandbox, say) by overriding the
`sandbox_base_url` fixture.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cb_sandbox.testkit.client import SandboxClient
from cb_sandbox.testkit.process import SandboxProcess

#: Where a managed server writes its world when the suite names no path. Not a
#: `tmp_path`: the run is the artefact — pointing a sandbox server at this file
#: afterwards and opening the web client shows every check the suite made,
#: filterable to one test and groupable by feature. A run whose file was
#: deleted on exit is exactly the evidence people want when something failed.
DEFAULT_RUN_DB = "sandbox-e2e.duckdb"

_REPORT_KEYS: dict[str, pytest.StashKey[pytest.TestReport]] = {
    "setup": pytest.StashKey[pytest.TestReport](),
    "call": pytest.StashKey[pytest.TestReport](),
    "teardown": pytest.StashKey[pytest.TestReport](),
}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("cb-sandbox")
    group.addoption(
        "--sandbox-url",
        default=os.environ.get("CB_SANDBOX_URL"),
        help="Base URL of an already-running sandbox. Omit to have one started per session.",
    )
    group.addoption(
        "--sandbox-config",
        default=os.environ.get("CB_SANDBOX_CONFIG"),
        help="Path to sandbox.config.json for a managed server.",
    )
    group.addoption(
        "--sandbox-db",
        default=os.environ.get("CB_SANDBOX_DB"),
        help=f"Where a managed server writes its DuckDB file (default: ./{DEFAULT_RUN_DB}).",
    )
    group.addoption(
        "--sandbox-keep-db",
        action="store_true",
        default=False,
        help="Keep the previous run's database instead of starting from empty.",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "feature(name): which feature this test checks — becomes the scenario's "
        "feature, so `GET /api/features` can group the whole run by it.",
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    """Stash each phase's report so the scenario fixture's teardown can tell
    the sandbox whether the test it was recording passed.

    A fixture cannot ask pytest for its own test's outcome any other way — by
    the time teardown runs, the result exists only in the report objects this
    hook sees.
    """
    outcome = yield
    report = outcome.get_result()
    item.stash[_REPORT_KEYS[report.when]] = report


def _outcome_of(item: pytest.Item) -> tuple[str, str | None]:
    """`(status, failure detail)` in the sandbox's vocabulary. A test that blew
    up in *setup* is `failed` too: a scenario whose fixtures never finished is
    not a scenario that passed."""
    for phase in ("setup", "call"):
        report = item.stash.get(_REPORT_KEYS[phase], None)
        if report is None:
            continue
        if report.skipped:
            return "skipped", None
        if report.failed:
            return "failed", report.longreprtext
    return "passed", None


def _feature_of(item: pytest.Item) -> str | None:
    marker = item.get_closest_marker("feature")
    if marker is None or not marker.args:
        return None
    return str(marker.args[0])


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="session")
def sandbox_run_db(request: pytest.FixtureRequest) -> Path:
    path = Path(request.config.getoption("--sandbox-db") or DEFAULT_RUN_DB)
    if not request.config.getoption("--sandbox-keep-db"):
        # Wiped at the *start* of the session, not the end: the file has to
        # outlive the run for anyone to read it. Both files — DuckDB keeps a
        # separate write-ahead log, and leaving a stale one next to a fresh
        # database is how a "cleaned" run comes back with the last run's
        # messages in it.
        for stale in (path, path.with_suffix(path.suffix + ".wal")):
            stale.unlink(missing_ok=True)
    return path


@pytest.fixture(scope="session")
def sandbox_server(
    request: pytest.FixtureRequest, sandbox_run_db: Path, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[SandboxProcess | None]:
    """A sandbox process for the session, unless one was named on the command
    line. Yields `None` in that case so `sandbox_base_url` can tell the
    difference without a second flag."""
    if request.config.getoption("--sandbox-url"):
        yield None
        return
    log_path = tmp_path_factory.mktemp("cb-sandbox-logs") / "sandbox.log"
    process = SandboxProcess(
        db_path=sandbox_run_db,
        config_path=request.config.getoption("--sandbox-config"),
        log_path=log_path,
    )
    with process:
        yield process


@pytest.fixture(scope="session")
def sandbox_base_url(request: pytest.FixtureRequest, sandbox_server: SandboxProcess | None) -> str:
    """Where the sandbox is. Override this in a suite that starts its own —
    everything else here goes through it, including the bot's `API_BASE`."""
    explicit = request.config.getoption("--sandbox-url")
    if explicit:
        return str(explicit).rstrip("/")
    assert sandbox_server is not None
    return sandbox_server.base_url


@pytest.fixture(scope="session")
def sandbox_kit(sandbox_base_url: str) -> dict[str, Any]:
    """`GET /api/kit`, fetched once — identity, seeds, features, commands.

    Read this instead of hardcoding: a suite that repeats the bot's id, or its
    seed names, has two places to update when the config changes and no way to
    notice it only updated one.
    """
    client = SandboxClient.connect(sandbox_base_url)
    try:
        return client.kit()
    finally:
        client.close()


@pytest.fixture(scope="session")
def sandbox_bot_id(sandbox_kit: dict[str, Any]) -> int:
    return int(sandbox_kit["bot"]["id"])


@pytest.fixture(scope="session")
def sandbox_bot_username(sandbox_kit: dict[str, Any]) -> str:
    return str(sandbox_kit["bot"]["username"])


@pytest.fixture
def sandbox(sandbox_base_url: str) -> Iterator[SandboxClient]:
    client = SandboxClient.connect(sandbox_base_url)
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
def sandbox_scenario_feature(request: pytest.FixtureRequest) -> str | None:
    """Which feature the current test is checking. Defaults to the `feature`
    marker; override it in a suite that derives the feature from something
    else (a directory layout, a parametrised fixture)."""
    return _feature_of(request.node)


@pytest.fixture
def sandbox_scenario_tags() -> list[str]:
    """Extra tags on this test's scenario, on top of the module-derived one.

    The override point for the dimensions a particular suite filters by that
    this plugin cannot know about — a language, a database backend, a
    protocol version. A suite parametrised over locales returns `[lang]` here
    and its scenarios become filterable by locale in the web client without
    touching anything else.
    """
    return []


@pytest.fixture(autouse=True)
def sandbox_scenario(request: pytest.FixtureRequest) -> Iterator[str | None]:
    """One scenario per test, opened before any other function-scoped fixture.

    Autouse so it is ordered first, which is the whole reason it can claim the
    traffic a *fixture* generates while building the world — a test whose
    setup creates a group and joins the bot into it would otherwise leave that
    conversation untagged and unattributable.

    Records what a person reading the run afterwards needs and cannot
    reconstruct: which test, from which file, checking which feature, with
    which docstring, and whether it passed. Everything else in the sandbox is
    *what happened*; this is *what it was for*.

    Skipped entirely for a test that never asks for a sandbox and carries no
    `feature` marker. That guard is why `sandbox_base_url` is resolved
    *inside* the body via `getfixturevalue` rather than declared as a
    parameter: this fixture is autouse across the whole session, and a
    parameter would make every unit test in the repository start a sandbox
    server before its own first line ran.
    """
    node = request.node
    wants_sandbox = "sandbox" in getattr(node, "fixturenames", ()) or _feature_of(node)
    if not wants_sandbox:
        yield None
        return

    client = SandboxClient.connect(request.getfixturevalue("sandbox_base_url"))
    # `module.test_name`, not the nodeid: an id becomes a URL path segment on
    # every scenario route, and a nodeid carries directory separators — a
    # slash is decoded before routing, so no amount of encoding rescues it.
    # Module plus function is unique within a package and still reads like the
    # test it names; the full nodeid goes in `metadata`, where nothing parses it.
    module = node.module.__name__.rsplit(".", 1)[-1] if node.module else "tests"
    scenario_id = f"{module}.{node.name}"
    description = (node.function.__doc__ or "").strip() or None if node.function else None
    feature = request.getfixturevalue("sandbox_scenario_feature")
    # The module name minus its `test_` prefix is a serviceable feature guess
    # for a suite that has not marked anything yet — the server treats a tag
    # matching a configured feature as that feature, so tagging costs nothing
    # and grandfathers in a suite that never adopts the marker.
    tags = [module.removeprefix("test_"), *request.getfixturevalue("sandbox_scenario_tags")]
    try:
        client.create_scenario(
            scenario_id=scenario_id,
            name=node.name,
            description=description,
            source="pytest",
            feature=feature,
            tags=tags,
            metadata={
                "nodeid": node.nodeid,
                "file": str(node.fspath),
                "markers": sorted(mark.name for mark in node.iter_markers()),
            },
        )
    except Exception:  # noqa: BLE001 - scenario bookkeeping must never fail a test
        client.close()
        yield None
        return

    try:
        yield scenario_id
    finally:
        status, detail = _outcome_of(node)
        try:
            if detail:
                # The traceback goes in as a note rather than into
                # `description`: notes are timestamped and carry a level, so
                # the UI can show a failure as a failure instead of as prose.
                client.add_note(scenario_id, detail[:2000], level="error")
            client.end_scenario(scenario_id, status=status)
        except Exception:  # noqa: BLE001 - a teardown that cannot reach the sandbox must not
            pass  # turn a passing test red on the way out
        finally:
            client.close()
