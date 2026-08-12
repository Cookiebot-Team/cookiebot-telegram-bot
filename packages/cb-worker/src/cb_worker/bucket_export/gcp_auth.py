"""Every decision about *who* the bucket export runs as, in one module.

`source.open_source` needs a Google credential scoped to read v1's private
bucket and nothing else; getting from "the operator has a personal Google
account with access" to that credential is a bigger problem than a scope
constant, because the honest answer is "don't use the operator's own token for
the actual reads at all." Three things live here, in order of how an operator
actually uses them:

1. `user_credentials` — Application Default Credentials for the *human*. This
   is what proves an operator is who they say they are, and it is also the
   seam every other path below eventually calls: `export_credentials`'s ADC
   fallback calls it directly, and its impersonation path calls it for the
   *source* credential behind the impersonation (a service account can only be
   impersonated by a principal already authenticated some other way).
2. `provision_export_account` / `revoke_export_account` — turn that human
   identity into a short-lived, narrowly-scoped service account: read-only on
   exactly one bucket, impersonable by exactly the operator who created it,
   nothing else. `create_export_key` is the escape hatch for an environment
   that cannot impersonate at all, deliberately not the default path either
   function reaches for.
3. `export_credentials` — what `source.open_source` actually calls. It never
   asks "does a service account exist"; it asks "what is the best credential
   available right now", in the preference order documented on the function
   itself, and every branch of that order ends up scoped to exactly
   `READ_ONLY_SCOPE` before it is handed back — the read-only guarantee
   `source.py`'s own module docstring describes is a property of *this*
   module's output, not of `GcsReadOnlySource` alone.

**Every call this module makes to Google is a separate, injectable function**
(`user_credentials`'s own `google.auth.default` call; `default_http_client` and
`default_bucket_iam`, which build the two live handles `provision_export_account`
and `revoke_export_account` operate through; and the handful of
`_create_service_account`/`_delete_service_account`/`_get_service_account_policy`/
`_set_service_account_policy` helpers that are the only things in this module
that speak the IAM REST API). None of them are reachable from a unit test by
accident — every public provisioning function takes its `http`/`bucket` handle
as a parameter, so a test hands in an in-memory fake and nothing here ever
opens a socket.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import stat
import time
import warnings
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import google.auth
from google.api_core.iam import Policy
from google.auth import impersonated_credentials
from google.auth.credentials import Credentials
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import AuthorizedSession, Request
from google.cloud import storage
from google.oauth2 import service_account

from cb_core.logging import get_logger

log = get_logger("cb.bucket_export.gcp_auth")

#: The one scope every credential path in this module is allowed to end up
#: with — see the module docstring. `source.py` re-exports this rather than
#: defining its own copy, so there is exactly one literal string to keep in
#: sync with `TestReadOnlyEnforcement`.
READ_ONLY_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"

#: What the *human* operator's own credential needs in order to create a
#: service account, grant IAM bindings, or mint an impersonated token for one
#: — never the scope handed to the export credential itself, which stays at
#: `READ_ONLY_SCOPE` no matter what the operator's own token can do.
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

#: Added alongside `CLOUD_PLATFORM_SCOPE` whenever this module needs to answer
#: "who is the operator" (`resolve_operator_context`) — this is the scope the
#: standard OAuth2 userinfo endpoint checks for before it will hand back an
#: `email` claim. Not requested on its own anywhere: every caller that needs
#: identity also needs `CLOUD_PLATFORM_SCOPE` to act on that identity.
USERINFO_EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"

#: Read by `export_credentials`: the email of a service account to impersonate
#: for the read, set by `provision`'s final "export this" line.
SERVICE_ACCOUNT_ENV = "CB_GCS_EXPORT_SERVICE_ACCOUNT"

#: The same variable `google.auth.default()` itself reads for a standing key
#: file — named here, not imported from `google.auth.environment_vars`,
#: because the two other things this module does with the value (locate the
#: ADC file for `describe_adc`, and treat `key_file=` as an override in
#: `export_credentials`) are this module's own concerns, not `google-auth`'s.
KEY_FILE_ENV = "GOOGLE_APPLICATION_CREDENTIALS"

#: The exact two grants `provision_export_account` makes, exported so the CLI's
#: plan table and `revoke_export_account` both name them from one place.
BUCKET_ROLE = "roles/storage.objectViewer"
TOKEN_CREATOR_ROLE = "roles/iam.serviceAccountTokenCreator"

_IAM_BASE = "https://iam.googleapis.com/v1"

#: The standard OAuth2 "who am I" endpoint — see `resolve_operator_email`.
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


class GcsAuthError(RuntimeError):
    """An auth or provisioning failure with a message safe to show an operator.

    Never a bare `DefaultCredentialsError` or `requests.HTTPError` — every
    place this module can fail names, in the message itself, the exact command
    that fixes it (`gcloud auth application-default login`, `gcs-auth
    provision`), the same "actionable message, not a traceback" contract
    `GcsSourceError` already keeps in `source.py`.
    """


class PartialProvisionError(GcsAuthError):
    """`provision_export_account` created the service account but failed
    before both grants landed — real, not hypothetical: verified against a
    production project where a bucket-policy write failed after the account
    existed.

    The account is left behind on purpose rather than auto-deleted here:
    a delete call made moments after a failed grant is racing the exact same
    IAM propagation lag that likely caused the failure in the first place,
    and a failed *cleanup* on top of a failed *provision* is a worse outcome
    than a leftover account with no bindings, which is inert (it can read
    nothing until granted) and is a one-line `gcs-auth revoke` away from
    being gone. `service_account_email` is carried on the exception
    specifically so the CLI can print that exact line instead of leaving the
    operator to reconstruct it from a stack trace.
    """

    def __init__(self, message: str, *, service_account_email: str) -> None:
        super().__init__(message)
        self.service_account_email = service_account_email


# --------------------------------------------------------------- identity


@dataclass(frozen=True, slots=True)
class AdcIdentity:
    """What `describe_adc` found on disk, read-only — no network call, so
    `gcs-auth status` (which never writes) can print it."""

    path: Path
    credential_type: str
    email: str | None
    #: Present only when the ADC file itself carries a `quota_project_id` —
    #: `gcloud auth application-default set-quota-project` writes one in.
    #: Worth its own status row (see `gcp_auth_cli.collect_status`): once set,
    #: every request from this credential carries `x-goog-user-project` for
    #: that project, which then requires `roles/serviceusage.serviceUsageConsumer`
    #: there — a mismatch here is a common, easy-to-misdiagnose cause of a
    #: 403 that looks like a permissions problem on the *resource* being
    #: called instead (see `diagnose_google_error`).
    quota_project_id: str | None


def default_adc_path() -> Path:
    """Where `google.auth.default()` will read from, absent a project-level
    override elsewhere in this process.

    `GOOGLE_APPLICATION_CREDENTIALS` wins if set — that is what `google.auth`
    itself checks first. Otherwise this is gcloud's own per-user ADC file:
    `$CLOUDSDK_CONFIG/application_default_credentials.json`, or
    `~/.config/gcloud/...` (`%APPDATA%\\gcloud\\...` on Windows) when
    `CLOUDSDK_CONFIG` is unset, which is gcloud's own documented config
    directory resolution — not `google-auth`'s private `_cloud_sdk` helper,
    because that module's leading underscore is Google's own signal that it is
    not a surface to depend on.
    """
    override = os.environ.get(KEY_FILE_ENV, "").strip()
    if override:
        return Path(override)
    config_dir = os.environ.get("CLOUDSDK_CONFIG", "").strip()
    if config_dir:
        return Path(config_dir) / "application_default_credentials.json"
    if os.name == "nt":
        return (
            Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
        )
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def describe_adc(path: Path | None = None) -> AdcIdentity | None:
    """Best-effort identity straight from the credential file on disk.

    A service-account key names itself (`client_email`). The file
    `gcloud auth application-default login` writes does not carry an email at
    all — it is a refresh token plus an OAuth client id — so `email` is `None`
    for that case, and callers say so rather than guessing one. `None` overall
    means no file exists at the resolved path, which is this machine's state
    with no credentials configured at all.
    """
    candidate = path or default_adc_path()
    if not candidate.is_file():
        return None
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    credential_type = str(data.get("type", "unknown"))
    email = data.get("client_email") or data.get("account") or None
    quota_project_id = data.get("quota_project_id") or None
    return AdcIdentity(
        path=candidate,
        credential_type=credential_type,
        email=str(email) if email else None,
        quota_project_id=str(quota_project_id) if quota_project_id else None,
    )


def _find_google_error_body(exc: Exception) -> Mapping[str, object] | None:
    """Best-effort extraction of a Google JSON error body from `exc`,
    wherever it actually lives.

    Most of the client libraries this module touches put it straight in
    `str(exc)`, which a plain substring search finds. `google.auth.exceptions
    .RefreshError` (raised by `impersonated_credentials...refresh()`, see
    `default_impersonation_probe`) does not: it is constructed as
    `RefreshError(message, response_body)`, a two-element `args` tuple, and
    the base `Exception.__str__` for a multi-arg exception renders as the
    *Python tuple repr* of `args` — `('message', '{\\n  "error": ...}')` —
    which is not valid JSON at all (its quotes and newlines are escaped as
    text, not real characters) and defeats a search over `str(exc)`
    specifically. `exc.args[1]` is not that repr, though — it is the real
    string the library received, parseable as-is — so this checks `args`
    before falling back to `str(exc)`.
    """
    candidates: list[object] = [*getattr(exc, "args", ()), str(exc)]
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return candidate
        if not isinstance(candidate, str):
            continue
        brace = candidate.find("{")
        if brace == -1:
            continue
        try:
            body = json.loads(candidate[brace:])
        except json.JSONDecodeError:
            continue
        if isinstance(body, dict):
            return body
    return None


def _summarize_google_error(exc: Exception) -> str:
    """Collapse a raw Google API error to one line.

    A Google API error's `str()` can embed its *entire* JSON error body —
    `"@type"`, an `error_info_id`, a `troubleshooter_url` link — which reads
    as a dozen wrapped lines dumped into whatever is displaying it (a table
    cell, a one-line CLI message). The permission denied and the resource are
    the whole of what an operator needs at a glance; the rest is exactly what
    a caller should send to the log instead (`log.warning(...,
    error=str(exc))`), not to the table — this function is what makes that
    split possible; it never discards anything, it just doesn't return it.
    """
    body = _find_google_error_body(exc)
    if body is not None:
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            status = error.get("status") or error.get("code")
            if message:
                return f"{status}: {message}" if status else str(message)
    # No JSON body found anywhere — a plain-text error (a test fake, a
    # network failure) — the first line is still shorter than the whole thing.
    return str(exc).splitlines()[0][:200]


def diagnose_google_error(exc: Exception) -> str:
    """Turn a raw Google API error into the one line an operator can act on
    — see `_summarize_google_error` for the "one line" half of that, and the
    rest of this docstring for the one translation on top of it this module
    knows how to make.

    A `403` whose body mentions `serviceusage` almost always means the ADC's
    own *quota project* is the problem, not the resource the call was
    actually about: once an ADC has a quota project set (`describe_adc`'s own
    `quota_project_id`), every request from it carries `x-goog-user-project`
    for that project, and the caller then needs
    `roles/serviceusage.serviceUsageConsumer` *there* — a completely
    different grant from whatever the call was trying to do (list a bucket,
    resolve an email, create a service account). Those two diagnoses are
    opposites ("you can't use this quota project" vs "you can't touch this
    resource"), and the raw error text does not tell them apart, so this
    names the quota project when the ADC file has one and states the fix:
    clear it, or point it at a project the operator does have that role on.
    Verified against a real 403 of exactly this shape, not theorised.
    """
    # Joins every candidate `_find_google_error_body` also checks (`args` plus
    # `str(exc)`) rather than `str(exc)` alone, for the same `RefreshError`
    # reason that function's own docstring explains: the "403"/"serviceusage"
    # text can live in `exc.args[1]` and never reach `str(exc)` verbatim.
    text = " ".join(str(part) for part in (*getattr(exc, "args", ()), exc))
    summary = _summarize_google_error(exc)
    if "403" in text and "serviceusage" in text.lower():
        adc = describe_adc()
        quota_project = adc.quota_project_id if adc is not None else None
        naming = f" ({quota_project!r})" if quota_project else ""
        return (
            f"{summary} — this looks like the ADC's quota project{naming}, not the resource itself. "
            "Once an ADC has a quota project, every request carries `x-goog-user-project`, which "
            "needs `roles/serviceusage.serviceUsageConsumer` on that project specifically. The fix "
            "is usually to clear it: remove `quota_project_id` from the ADC file, or run `gcloud "
            "auth application-default set-quota-project <PROJECT_ID>` with a project you do have "
            "that role on."
        )
    return summary


#: A freshly created service account is not immediately visible to every
#: other Google service — Cloud Storage's bucket `setIamPolicy` can 400 on a
#: binding that names one seconds after `_create_service_account` returns,
#: purely from replication lag, not because anything is wrong (GCP's own
#: documented behaviour, and the normal case on a fresh account, not an edge
#: one — verified against a real 400 of exactly this shape, on the very
#: first production run).
#:
#: A *different*, longer-lived-looking 404 was also observed on
#: `iam.googleapis.com`'s own `getIamPolicy`/`setIamPolicy` for the same
#: account, which cost real debugging time chasing as if it were the same
#: propagation phenomenon — a wider retry, a per-account 404 scope, a
#: five-minute budget were all tried against it in turn. It was not lag at
#: all: `_get_service_account_policy` was calling `getIamPolicy` with `GET`,
#: and that method is POST-only on `iam.googleapis.com` (see that function's
#: own docstring) — a `GET` 404s *permanently*, not intermittently, which is
#: why no budget was ever going to fix it and why it is not handled here.
#: `_PROPAGATION_RETRY_ATTEMPTS` tries, `_PROPAGATION_RETRY_DELAY_S` apart,
#: covers the one real, measured phenomenon (a few seconds up to ~30s)
#: without making a genuine failure — which is never retried, see
#: `_looks_like_propagation_lag` — hang anywhere near that long.
_PROPAGATION_RETRY_ATTEMPTS = 6
_PROPAGATION_RETRY_DELAY_S = 5.0


def _looks_like_propagation_lag(exc: Exception) -> bool:
    """Whether `exc` is the specific "this service account does not exist
    yet" shape Cloud Storage's bucket `setIamPolicy` produces for a service
    account created moments earlier, as opposed to a real permission or
    configuration error — or a bug in this module.

    Matched on that literal phrase and nothing broader, on purpose: a status
    code alone (a bare `404`, say) is cheap to produce for an unrelated
    reason — a URL bug, in this module's own history (see the module-level
    note above `_PROPAGATION_RETRY_ATTEMPTS`) — and retrying it just turns a
    deterministic failure into a slow deterministic failure. This specific,
    verified phrase is not cheap to produce by accident, so it is the only
    thing this checks for.
    """
    lowered = str(exc).lower()
    return "service account" in lowered and "does not exist" in lowered


def _retry_on_propagation_lag[T](
    operation: Callable[[], T], *, on_attempt: Callable[[int, int], None] | None = None
) -> T:
    """Call `operation`, retrying only on `_looks_like_propagation_lag`.

    `on_attempt(attempt, total)` fires before each retry's sleep (never
    before the first attempt, which is not a retry) so a caller with a
    progress display can say what it is waiting for instead of going quiet
    for up to 30 seconds, which reads as a hang.
    """
    for attempt in range(1, _PROPAGATION_RETRY_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            if not _looks_like_propagation_lag(exc) or attempt == _PROPAGATION_RETRY_ATTEMPTS:
                raise
            if on_attempt is not None:
                on_attempt(attempt, _PROPAGATION_RETRY_ATTEMPTS)
            time.sleep(_PROPAGATION_RETRY_DELAY_S)
    raise AssertionError("unreachable: the loop above always returns or raises")


def describe_credentials(credentials: Credentials) -> str:
    """A human-readable label for a live credential object — used by
    `gcs-auth status` and the cutover preflight row, which both need to say
    *who* a request would run as, not just whether one succeeded.

    `service_account_email` is present on both `service_account.Credentials`
    (a key file) and `impersonated_credentials.Credentials` (impersonation),
    so this one attribute check covers both non-human paths; plain user
    credentials have no such attribute, which is the "otherwise" case.
    """
    email = getattr(credentials, "service_account_email", None)
    return str(email) if email else "the operator's own ADC identity"


@contextlib.contextmanager
def _quiet_default_diagnostics() -> Iterator[None]:
    """`google.auth.default()` narrates the same "no quota project" fact two
    different ways whenever the ADC it found is a personal account with no
    quota project set — the common case right after a plain `gcloud auth
    application-default login`, which does not set one by itself:

    1. A `UserWarning` ("...authenticated using end user credentials from
       Google Cloud SDK without a quota project...") raised on *every* call.
    2. A `logging.warning()` ("No project ID could be determined...") raised
       specifically when it also could not resolve a project at all.

    Left alone, both print raw, above whichever `rich` table this module's
    caller is about to render. Callers translate the same fact
    (`project is None`) into their own actionable row/message instead
    (`OperatorContext.project_source`, `status`'s "project" row), so this
    silences both — the warning by category and message, the logger by name —
    around the one call that can emit either, and restores both immediately
    after.
    """
    logger = logging.getLogger("google.auth._default")
    previous_level = logger.level
    logger.setLevel(logging.ERROR)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message=r".*without a quota project.*", category=UserWarning
            )
            yield
    finally:
        logger.setLevel(previous_level)


def user_credentials(scopes: Sequence[str]) -> tuple[Credentials, str | None]:
    """Application Default Credentials, scoped to exactly `scopes`.

    This is the one place `google.auth.default` is called in this module —
    every other credential path either calls this directly (the ADC fallback
    in `export_credentials`, `resolve_operator_context`) or uses its result as
    the *source* credential for an impersonation (`export_credentials`'s
    service-account path, `provision_export_account`, `revoke_export_account`).
    A test can monkeypatch `google.auth.default` once, the same single seam
    `source.py::TestReadOnlyEnforcement` already patches, and cover every path
    that depends on it.
    """
    try:
        with _quiet_default_diagnostics():
            credentials, project = google.auth.default(scopes=list(scopes))
    except DefaultCredentialsError as exc:
        raise GcsAuthError(
            "no Google credentials found for this operator. Run "
            "`gcloud auth application-default login`, authenticate with the Google "
            "account that has access to v1's bucket, and re-run this command."
        ) from exc
    return credentials, project


def _fetch_userinfo(http: HttpClient) -> tuple[str | None, str | None]:
    """`(email, diagnosis)`. `email` is `None` on any failure; `diagnosis` is
    only set when that failure matches the quota-project 403 shape
    `diagnose_google_error` recognises, so a caller (`resolve_operator_context`)
    can surface *that* specifically instead of a bare "could not resolve" —
    the whole point being that a quota-project mismatch is fixable in a way a
    generic failure is not.
    """
    try:
        response = http.get(_USERINFO_URL)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - best-effort identity lookup, never fatal
        diagnosis = diagnose_google_error(exc)
        return None, (diagnosis if diagnosis != str(exc) else None)
    email = response.json().get("email")
    return (str(email) if email else None), None


def resolve_operator_email(http: HttpClient) -> str | None:
    """Best-effort "who am I" for the operator's own credential, via the
    standard OAuth2 userinfo endpoint.

    This is the one call that can answer the question at all for
    `authorized_user`-type ADC (a plain `gcloud auth application-default
    login`): its file on disk is a refresh token plus an OAuth client id, with
    no email anywhere in it — see `describe_adc`. Never raises: a network
    failure, or a token that was scoped without `USERINFO_EMAIL_SCOPE`, both
    mean "could not resolve", which every caller treats as "fall back to
    asking the operator explicitly", never as an error worth failing a
    read-only `status` call or aborting a `provision` over.
    """
    email, _ = _fetch_userinfo(http)
    return email


@dataclass(frozen=True, slots=True)
class OperatorContext:
    """Everything both `gcs-auth status` and `gcs-auth provision` need to know
    about the human running this — resolved together because both facts come
    from one ADC credential fetch, so a caller that needs both never
    authenticates twice.
    """

    #: The operator's own email, or `None` if it could not be resolved at all
    #: (no ADC, or an ADC whose scope does not cover the userinfo lookup).
    email: str | None
    #: Where `email` came from, or — when `email` is `None` — why not. Always
    #: set, so a caller can explain an unresolved identity instead of leaving
    #: a bare "unknown".
    email_source: str
    #: The GCP project to create a service account in, or `None` if none is
    #: configured anywhere this function looked.
    project: str | None
    project_source: str
    #: The live credential behind `email`/`project`, so a caller that goes on
    #: to provision does not have to authenticate a second time. `None` iff
    #: `error` is set.
    credentials: Credentials | None
    #: Set only when ADC itself could not be loaded at all — the one case
    #: where neither `email` nor `project` could possibly be resolved.
    error: str | None


def resolve_operator_context(*, explicit_project: str | None = None) -> OperatorContext:
    """Resolve the operator's email and default project from Application
    Default Credentials, at `CLOUD_PLATFORM_SCOPE` + `USERINFO_EMAIL_SCOPE` —
    broad enough both to answer "who is this" and, for a caller that goes on
    to call `provision_export_account`, to actually do the provisioning.

    `email` prefers the ADC file's own `client_email` (a service-account key
    names itself, no network needed) and falls back to one `userinfo` call
    (`resolve_operator_email`) for a personal account, which never carries an
    email in its ADC file. `project` prefers `explicit_project` (a `--project`
    flag), then whatever `google.auth.default()` itself resolved; `None` is a
    normal outcome, not a bug — a plain `gcloud auth application-default
    login` sets no quota project by default — and it is on the caller to
    decide whether that is acceptable (`status`, read-only) or fatal
    (`provision`, which needs one to create an account in).
    """
    adc = describe_adc()
    if adc is not None and adc.email:
        file_email: str | None = adc.email
        file_email_source = f"ADC file ({adc.path})"
    else:
        file_email = None
        file_email_source = "not resolved"

    try:
        credentials, adc_project = user_credentials([CLOUD_PLATFORM_SCOPE, USERINFO_EMAIL_SCOPE])
    except GcsAuthError as exc:
        return OperatorContext(
            email=file_email,
            email_source=file_email_source,
            project=explicit_project,
            project_source="--project" if explicit_project else "unset",
            credentials=None,
            error=str(exc),
        )

    if file_email:
        email, email_source = file_email, file_email_source
    else:
        resolved, diagnosis = _fetch_userinfo(default_http_client(credentials))
        if resolved:
            email, email_source = resolved, "userinfo endpoint"
        elif diagnosis:
            # A quota-project 403 specifically — surface the diagnosed reason
            # rather than the generic "could not be resolved" below, since
            # this one is actionable (`diagnose_google_error`) and the other
            # is not.
            email, email_source = None, diagnosis
        else:
            email, email_source = (
                None,
                "ADC found but the email could not be resolved (pass --operator explicitly)",
            )

    if explicit_project:
        project, project_source = explicit_project, "--project"
    elif adc_project:
        project, project_source = adc_project, "ADC"
    else:
        # `--project` first, deliberately: it costs nothing and cannot break
        # anything. `gcloud config set project` second. `set-quota-project`
        # is not offered here at all — it changes what every subsequent
        # request from this ADC carries (`x-goog-user-project`), which needs
        # `roles/serviceusage.serviceUsageConsumer` on that project and can
        # turn a working credential into one that 403s on everything
        # (`diagnose_google_error` exists because this happened for real).
        project, project_source = (
            None,
            "unset — pass --project explicitly, or run `gcloud config set project <PROJECT_ID>`",
        )

    return OperatorContext(
        email=email,
        email_source=email_source,
        project=project,
        project_source=project_source,
        credentials=credentials,
        error=None,
    )


# ------------------------------------------------------------- export credential


def export_credentials(
    *, service_account_email: str | None = None, key_file: str | None = None
) -> tuple[Credentials, str | None]:
    """The credential `source.open_source` actually reads the bucket with.

    Three paths, tried in this order, and every one of them ends up scoped to
    exactly `READ_ONLY_SCOPE` — never broader — because a write-capable scope
    on the *source* credential would undo the guarantee `source.py`'s module
    docstring is built around, regardless of which path produced it:

    1. `service_account_email` (normally `CB_GCS_EXPORT_SERVICE_ACCOUNT`) —
       impersonate that service account. This is the path `provision_export_account`
       sets up, and it is the *preferred* one: no long-lived key ever touches
       disk, the token this returns is minted fresh and expires on its own,
       and `impersonated_credentials.Credentials(target_scopes=[READ_ONLY_SCOPE])`
       is what pins the scope here regardless of what the operator's own
       source credential (used only to *call* the impersonation) can do.
    2. `key_file` (normally `GOOGLE_APPLICATION_CREDENTIALS`) — a standing
       service-account key on disk. This is the path to avoid, not the
       default: a key outlives this process and has to be rotated and deleted
       by hand. It exists for the one case that cannot impersonate at all (no
       `gcloud` session, a CI runner holding only a key secret) — see
       `create_export_key`.
    3. Neither set — the operator's own ADC, re-scoped to read-only. What a
       human running this from their own authenticated shell falls back to.
    """
    sa_email = service_account_email or os.environ.get(SERVICE_ACCOUNT_ENV, "").strip()
    if sa_email:
        source_credentials, _ = user_credentials([CLOUD_PLATFORM_SCOPE])
        impersonated = impersonated_credentials.Credentials(
            source_credentials=source_credentials,
            target_principal=sa_email,
            target_scopes=[READ_ONLY_SCOPE],
            lifetime=3600,
        )
        return impersonated, None

    key_path = key_file or os.environ.get(KEY_FILE_ENV, "").strip()
    if key_path:
        try:
            credentials = service_account.Credentials.from_service_account_file(
                key_path, scopes=[READ_ONLY_SCOPE]
            )
        except (OSError, ValueError) as exc:
            raise GcsAuthError(
                f"could not load the service-account key at {key_path!r}: {exc}"
            ) from exc
        return credentials, credentials.project_id

    return user_credentials([READ_ONLY_SCOPE])


# -------------------------------------------------------------------- HTTP seam


class HttpResponse(Protocol):
    """The subset of `requests.Response` this module reads — narrow so a test
    fake needs to implement three things, not the whole `requests` surface."""

    status_code: int

    def json(self) -> Mapping[str, object]: ...

    def raise_for_status(self) -> None: ...


class HttpClient(Protocol):
    """The subset of an authenticated HTTP session this module needs, against
    two different hosts: `https://iam.googleapis.com/v1/...` (service-account
    provisioning) and `https://www.googleapis.com/oauth2/v3/userinfo`
    (`resolve_operator_email`). Every function below that talks to either
    takes one of these as a parameter rather than building it internally,
    which is what lets a test substitute an in-memory fake with no network and
    no real credentials.
    """

    def get(self, url: str, *, params: Mapping[str, str] | None = None) -> HttpResponse: ...

    def post(self, url: str, *, json: Mapping[str, object] | None = None) -> HttpResponse: ...

    def delete(self, url: str) -> HttpResponse: ...


class _SessionHttpClient:
    """Adapts a `requests`-shaped session (`AuthorizedSession`, or plain
    `requests.Session`) to the narrow `HttpClient` protocol above.

    Not just a type-compatibility exercise: `requests.Session.post`'s own
    stub takes a positional `data` parameter ahead of `json` and a stricter
    recursive `JsonType` for it, so `AuthorizedSession` itself does not
    structurally satisfy `HttpClient` for mypy even though every call this
    module makes to `.post()` would work fine at runtime — this adapter is
    the one place that gap is bridged, with one explicit, narrow
    `type: ignore` rather than a looser protocol that would accept more than
    this module actually needs.
    """

    def __init__(self, session: AuthorizedSession) -> None:
        self._session = session

    def get(self, url: str, *, params: Mapping[str, str] | None = None) -> HttpResponse:
        return self._session.get(url, params=params)

    def post(self, url: str, *, json: Mapping[str, object] | None = None) -> HttpResponse:
        return self._session.post(url, json=json)  # type: ignore[arg-type]

    def delete(self, url: str) -> HttpResponse:
        return self._session.delete(url)


def default_http_client(credentials: Credentials) -> HttpClient:
    """The real `HttpClient`: an `AuthorizedSession` wrapping the operator's
    own (cloud-platform-scoped) credential, adapted to this module's narrow
    interface. The same authenticated session is used against both hosts
    `HttpClient` documents — both are just HTTPS calls the operator's own
    credential is allowed to make."""
    return _SessionHttpClient(AuthorizedSession(credentials))


class BucketIam(Protocol):
    """The subset of `google.cloud.storage.Bucket` this module touches — its
    IAM policy, never an object inside it. A fake standing in for this in
    tests never has to model a single blob, which is also a small proof by
    construction that provisioning cannot read or write bucket *contents*."""

    def get_iam_policy(self, requested_policy_version: int) -> Policy: ...

    def set_iam_policy(self, policy: Policy) -> Policy: ...


def default_bucket_iam(
    bucket_name: str, credentials: Credentials, project: str | None
) -> BucketIam:
    """The real `BucketIam`: a `google.cloud.storage.Bucket` handle built from
    the operator's own credential — never the read-only export credential,
    which by construction (`READ_ONLY_SCOPE`) cannot call an IAM-policy
    endpoint at all."""
    client = storage.Client(credentials=credentials, project=project)
    return client.bucket(bucket_name)


#: Every function below builds its `serviceAccounts` URL with an explicit
#: `project`, never with IAM's documented `projects/-` wildcard — on purpose,
#: after getting this wrong once. `projects/-` *is* real and *is* accepted by
#: some IAM `serviceAccounts` lookups (a plain GET by email can use it to mean
#: "find this email in whichever project it lives in"), which is exactly why
#: it is the natural thing to reach for. It does not work for the calls this
#: module makes seconds after creating an account: `getIamPolicy`/
#: `setIamPolicy`/`delete` against `projects/-/serviceAccounts/<email>` 404 on
#: a freshly created account even though the identical request against
#: `projects/<real-project>/serviceAccounts/<email>` succeeds — proven
#: against a real account, both requests seconds apart, against a real
#: project. `project` is exactly what `provision_export_account` already has
#: in hand (it just created the account there), so there is no reason to fall
#: back to the wildcard at all.


def _create_service_account(
    http: HttpClient, project: str, account_id: str, display_name: str
) -> str:
    """POST .../v1/projects/{project}/serviceAccounts -> the new account's
    email. One call, no retry loop: `provision_export_account` either gets a
    usable account back or the whole provision fails loudly."""
    response = http.post(
        f"{_IAM_BASE}/projects/{project}/serviceAccounts",
        json={"accountId": account_id, "serviceAccount": {"displayName": display_name}},
    )
    response.raise_for_status()
    email = response.json()["email"]
    return str(email)


def _delete_service_account(http: HttpClient, project: str, email: str) -> bool:
    """DELETE .../v1/projects/{project}/serviceAccounts/{email}.

    Returns whether the account actually existed. A 404 means it is already
    gone — a prior, partial `revoke` or a manual cleanup — and
    `revoke_export_account` treats that as success, not failure: "already
    revoked" is the state this call is trying to reach, not a special case to
    detect first.
    """
    response = http.delete(f"{_IAM_BASE}/projects/{project}/serviceAccounts/{email}")
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


def _get_service_account_policy(
    http: HttpClient, project: str, email: str
) -> MutableMapping[str, object]:
    """POST .../v1/projects/{project}/serviceAccounts/{email}:getIamPolicy.

    `POST`, not `GET`, despite the name — this cost real debugging time to
    notice, because "getIamPolicy" reads like a getter and every instinct
    says `GET`. On `iam.googleapis.com`, `:getIamPolicy`/`:setIamPolicy`/
    `:testIamPermissions` are all POST-only custom methods (a `GET` on the
    same URL 404s permanently, not intermittently — verified against a real
    account: the same URL, same credential, same second, 404 on `GET` and 200
    on `POST`). Only the plain resource reads
    (`GET .../serviceAccounts`, `GET .../serviceAccounts/<email>`) are `GET`.
    """
    response = http.post(f"{_IAM_BASE}/projects/{project}/serviceAccounts/{email}:getIamPolicy")
    response.raise_for_status()
    policy = dict(response.json())
    policy.setdefault("bindings", [])
    return policy


def _set_service_account_policy(
    http: HttpClient, project: str, email: str, policy: Mapping[str, object]
) -> None:
    response = http.post(
        f"{_IAM_BASE}/projects/{project}/serviceAccounts/{email}:setIamPolicy",
        json={"policy": dict(policy)},
    )
    response.raise_for_status()


def _add_member(bindings: list[MutableMapping[str, object]], role: str, member: str) -> None:
    """Add `member` to `role`'s binding, creating the binding if it does not
    exist yet. Every *other* binding in the list is untouched — this is what
    "add one binding, do not clobber the rest" means for the plain-JSON IAM
    policy shape the IAM REST API uses (as opposed to the bucket's
    `google.api_core.iam.Policy` object, which needs its own helper below
    because it represents members as a `set`, not a `list`).
    """
    for binding in bindings:
        if binding.get("role") == role:
            members = binding.setdefault("members", [])
            assert isinstance(members, list)
            if member not in members:
                members.append(member)
            return
    bindings.append({"role": role, "members": [member]})


# ------------------------------------------------------------- bucket IAM helpers


def _add_bucket_binding(policy: Policy, role: str, member: str) -> None:
    """Add `member` to `role` on a bucket-level `Policy`, in place.

    Reads `policy.bindings` (a plain list `google.api_core.iam.Policy` exposes
    for version-3 policies, since the dict-style `policy[role]` accessor
    raises for exactly this version — see that class's own docstring) and
    writes the whole list back with one binding changed, so a binding this
    call did not add — the project owner's `roles/owner`, another team's prior
    grant — survives untouched.
    """
    bindings = [dict(b) for b in policy.bindings]
    for binding in bindings:
        if binding.get("role") == role and binding.get("condition") is None:
            binding["members"] = set(binding.get("members", set())) | {member}
            policy.bindings = bindings
            return
    bindings.append({"role": role, "members": {member}})
    policy.bindings = bindings


def _remove_bucket_binding(policy: Policy, role: str, member: str) -> bool:
    """The inverse of `_add_bucket_binding`. Returns whether `member` was
    actually present, so `revoke_export_account` can report exactly what it
    removed versus what was already gone, and so it can skip the
    `set_iam_policy` write entirely when there is nothing to change (see that
    function's own docstring on the "safe to run twice" contract).

    A binding for `role` that becomes empty once `member` is removed is
    dropped from the list rather than left behind with no members — a
    `google.api_core.iam.Policy` created that binding for this export account
    alone, so there is nothing else it could still be needed for.
    """
    changed = False
    kept: list[dict[str, object]] = []
    for binding in policy.bindings:
        if binding.get("role") == role and binding.get("condition") is None:
            members = set(binding.get("members", set()))
            if member in members:
                members.discard(member)
                changed = True
            if members:
                kept.append({**binding, "members": members})
        else:
            kept.append(dict(binding))
    if changed:
        policy.bindings = kept
    return changed


# ------------------------------------------------------------- naming


def export_account_id(stamp: str) -> str:
    """`cb-bucket-export-<stamp>` — a GCP service-account id must be 6-30
    characters of lowercase letters, digits and hyphens, starting with a
    letter. The `cb-bucket-export-` prefix is 17 of those, so `stamp` gets 13
    at most; callers derive one short, sortable stamp once (e.g.
    `datetime.now(UTC).strftime("%y%m%d%H%M%S")`, 12 characters) and pass it
    in — this function never touches the clock itself, so it stays a pure,
    trivially-testable string transform.
    """
    account_id = f"cb-bucket-export-{stamp}"
    if not re.fullmatch(r"[a-z][a-z0-9-]{5,29}", account_id):
        raise ValueError(
            f"stamp {stamp!r} produces an invalid service-account id {account_id!r} "
            "(need 6-30 lowercase letters/digits/hyphens, starting with a letter)"
        )
    return account_id


def export_account_email(account_id: str, project: str) -> str:
    return f"{account_id}@{project}.iam.gserviceaccount.com"


# ------------------------------------------------------------- provision / revoke


@dataclass(frozen=True, slots=True)
class ExportAccountRecord:
    """What `provision_export_account` created, verbatim — printed by the CLI
    and the only input `revoke_export_account` needs besides the live
    handles."""

    service_account_email: str
    project: str
    bucket: str
    bucket_role: str
    token_creator_role: str
    operator_principal: str
    stamp: str


def provision_export_account(
    *,
    project: str,
    bucket_name: str,
    operator_principal: str,
    stamp: str,
    http: HttpClient,
    bucket: BucketIam,
    on_retry: Callable[[str, int, int], None] | None = None,
) -> ExportAccountRecord:
    """Create the temporary export service account and grant it exactly two
    things, both resource-scoped, neither project-wide:

    * `roles/storage.objectViewer` **on `bucket_name` only** — read, not the
      `roles/storage.objectAdmin` a project-level grant would tempt someone
      into reaching for, and not the project either.
    * `roles/iam.serviceAccountTokenCreator` **on the new account itself**,
      to `operator_principal` — the grant that makes impersonation possible;
      without it `export_credentials`'s impersonation path gets a permission
      error, not a scope violation, the first time it tries to mint a token.
      This is not an optional hardening step: impersonating a freshly
      created account with no grant at all was tested directly against a
      real project and produced `403 Permission 'iam.serviceAccounts.getAccessToken'
      denied on resource`, from an operator who otherwise owns the project —
      there is no privilege level that skips this binding.

    Both grants read the current policy of their resource, add one binding
    (or one member on an existing binding for that exact role), and write the
    whole policy back — see `_add_bucket_binding` and `_add_member` — so nothing
    already on either policy is ever dropped.

    Only the bucket grant is wrapped in `_retry_on_propagation_lag`: the
    account was just created, and Cloud Storage's bucket `setIamPolicy` can
    fail on a binding that names it seconds later purely because it has not
    replicated yet (verified against a real 400 of exactly this shape).
    `on_retry`, when given, is told which attempt it is on, so a caller with
    a progress display can say so instead of going quiet. The token-creator
    grant is *not* retried the same way — see `_get_service_account_policy`'s
    own docstring for why a naive "IAM 404s too, so retry it the same way"
    reading of an earlier failure here was itself the bug.

    If the account was created but a grant still fails after every retry,
    this raises `PartialProvisionError` naming the account rather than
    silently leaving an ungrantable, unmentioned service account behind —
    see that exception's own docstring for why it is not auto-deleted here.
    """
    account_id = export_account_id(stamp)
    _create_service_account(
        http, project, account_id, display_name=f"cutover bucket export ({stamp})"
    )
    email = export_account_email(account_id, project)

    def _grant_bucket_role() -> None:
        policy = bucket.get_iam_policy(requested_policy_version=3)
        policy.version = 3
        _add_bucket_binding(policy, BUCKET_ROLE, f"serviceAccount:{email}")
        bucket.set_iam_policy(policy)

    def _grant_token_creator() -> None:
        sa_policy = _get_service_account_policy(http, project, email)
        bindings = sa_policy["bindings"]
        assert isinstance(bindings, list)
        _add_member(bindings, TOKEN_CREATOR_ROLE, operator_principal)
        _set_service_account_policy(http, project, email, sa_policy)

    try:
        _retry_on_propagation_lag(
            _grant_bucket_role,
            on_attempt=(lambda attempt, total: on_retry("bucket role", attempt, total))
            if on_retry is not None
            else None,
        )
        # `_grant_token_creator` is not wrapped in the same retry: the 404 it
        # used to hit here was the `GET`-instead-of-`POST` bug
        # `_get_service_account_policy`'s docstring describes, not
        # propagation lag, and now that the method is fixed there is no
        # known failure mode on this call worth retrying blindly.
        _grant_token_creator()
    except Exception as exc:
        log.warning(
            "bucket_export.gcp_auth.partial_provision",
            service_account=email,
            bucket=bucket_name,
            error=str(exc),
        )
        raise PartialProvisionError(
            f"service account {email} was created but a grant failed: {exc}",
            service_account_email=email,
        ) from exc

    log.info(
        "bucket_export.gcp_auth.provisioned",
        service_account=email,
        bucket=bucket_name,
        operator=operator_principal,
    )
    return ExportAccountRecord(
        service_account_email=email,
        project=project,
        bucket=bucket_name,
        bucket_role=BUCKET_ROLE,
        token_creator_role=TOKEN_CREATOR_ROLE,
        operator_principal=operator_principal,
        stamp=stamp,
    )


#: How long, and how far apart, `verify_impersonation` polls after
#: `provision_export_account` returns — the `serviceAccountTokenCreator`
#: grant it just made does not take effect the instant the API call granting
#: it returns 200. Measured against a real project, across more than one run:
#: impersonation failed immediately after the grant call returned, and later
#: started succeeding — 22 seconds later on one run, ~45 seconds later on
#: another. `_IMPERSONATION_RETRY_ATTEMPTS` tries, `_IMPERSONATION_RETRY_DELAY_S`
#: apart, budgets about 90 seconds so the common case confirms inside this
#: call rather than handing the operator a warning to act on themselves.
_IMPERSONATION_RETRY_ATTEMPTS = 10
_IMPERSONATION_RETRY_DELAY_S = 10.0


def default_impersonation_probe(
    credentials: Credentials, target_principal: str
) -> Callable[[], None]:
    """The real probe `verify_impersonation` polls: mint one impersonated,
    read-only-scoped token for `target_principal` and discard it — the
    cheapest real proof that the grant actually works, not just that the API
    call granting it returned success. Returned as a zero-argument callable
    (rather than doing the work directly) so `verify_impersonation`'s retry
    loop has one thing to call regardless of where the credential came from,
    the same shape `_retry_on_propagation_lag` takes for the IAM grants
    themselves.
    """
    request = Request()

    def _attempt() -> None:
        impersonated_credentials.Credentials(
            source_credentials=credentials,
            target_principal=target_principal,
            target_scopes=[READ_ONLY_SCOPE],
            lifetime=60,
        ).refresh(request)

    return _attempt


def verify_impersonation(
    attempt: Callable[[], None], *, on_attempt: Callable[[int, int], None] | None = None
) -> None:
    """Poll `attempt` (normally `default_impersonation_probe`'s return value)
    until it stops raising, up to `_IMPERSONATION_RETRY_ATTEMPTS` tries
    `_IMPERSONATION_RETRY_DELAY_S` apart.

    Every exception is retried here, unlike `_retry_on_propagation_lag`'s
    single specific phrase: the failure this polls past (`iam.serviceAccounts
    .getAccessToken` denied, or any shape it takes) is itself the evidence
    that the grant has not taken effect yet, not a signal that needs
    disambiguating from an unrelated failure the way a bucket 400 does. If it
    never succeeds within budget, the raised `GcsAuthError` says so plainly —
    `provision` should not report success on the grant API call alone and
    hand back a service account the operator's very next command then fails
    to use, which reads as the tool lying about what it just did.
    """
    for attempt_number in range(1, _IMPERSONATION_RETRY_ATTEMPTS + 1):
        try:
            attempt()
            return
        except Exception as exc:
            if attempt_number == _IMPERSONATION_RETRY_ATTEMPTS:
                raise GcsAuthError(
                    "the service account was created and granted, but impersonating it still "
                    f"fails after {attempt_number} attempts: {diagnose_google_error(exc)}"
                ) from exc
            if on_attempt is not None:
                on_attempt(attempt_number, _IMPERSONATION_RETRY_ATTEMPTS)
            time.sleep(_IMPERSONATION_RETRY_DELAY_S)


@dataclass(frozen=True, slots=True)
class RevokeResult:
    """What `revoke_export_account` actually did, so the CLI can say what was
    removed versus what was already gone rather than just "done"."""

    service_account_deleted: bool
    bucket_binding_removed: bool


def revoke_export_account(
    *,
    service_account_email: str,
    project: str,
    bucket_name: str,
    http: HttpClient,
    bucket: BucketIam,
) -> RevokeResult:
    """The inverse of `provision_export_account` — safe to run twice, or after
    someone already cleaned up half of it by hand.

    `project` is required, not defaulted to IAM's `projects/-` wildcard —
    see the note above `_create_service_account` for why that wildcard 404s
    on the exact calls this function makes. A caller that does not know which
    project the account was created in has to find out (`--project`, or the
    operator's own ADC project) rather than this function guessing via `-`.

    Deleting the service account is enough to revoke the
    `serviceAccountTokenCreator` grant made *on* it: that grant lives on the
    account's own IAM policy, and the policy goes away with the resource, so
    there is no separate call for it here. The bucket binding is a grant made
    on a *different* resource (the bucket), so it needs its own removal, and
    it is removed by exact member-string match regardless of whether the
    service account itself still exists — a stale `serviceAccount:...member`
    on the bucket's policy after the account is gone is exactly the leftover
    this call exists to clean up.
    """
    policy = bucket.get_iam_policy(requested_policy_version=3)
    bucket_removed = _remove_bucket_binding(
        policy, BUCKET_ROLE, f"serviceAccount:{service_account_email}"
    )
    if bucket_removed:
        bucket.set_iam_policy(policy)

    deleted = _delete_service_account(http, project, service_account_email)

    log.info(
        "bucket_export.gcp_auth.revoked",
        service_account=service_account_email,
        bucket=bucket_name,
        deleted=deleted,
        bucket_binding_removed=bucket_removed,
    )
    return RevokeResult(service_account_deleted=deleted, bucket_binding_removed=bucket_removed)


def create_export_key(
    *, service_account_email: str, project: str, destination: Path, http: HttpClient
) -> Path:
    """A last-resort standing key, for the one environment that cannot
    impersonate at all (no `gcloud` session to impersonate *from*, a CI runner
    holding only a key secret) — never the default `provision` reaches for,
    and the CLI is responsible for the loud warning that accompanies calling
    this; this function's only job is getting the bytes onto disk safely.

    `project` is required for the same reason `revoke_export_account` needs
    one: the wildcard `projects/-/serviceAccounts/...` 404s on an account
    this recently created (see the note above `_create_service_account`).

    Writes with mode `0600` before anything else touches the path, so the key
    is never briefly group- or world-readable between the write and the
    permission change.
    """
    response = http.post(
        f"{_IAM_BASE}/projects/{project}/serviceAccounts/{service_account_email}/keys",
        json={"privateKeyType": "TYPE_GOOGLE_CREDENTIALS_FILE"},
    )
    response.raise_for_status()
    body = response.json()
    key_bytes = base64.b64decode(str(body["privateKeyData"]))

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.touch(mode=0o600, exist_ok=True)
    os.chmod(destination, stat.S_IRUSR | stat.S_IWUSR)
    destination.write_bytes(key_bytes)

    log.warning(
        "bucket_export.gcp_auth.key_created",
        service_account=service_account_email,
        path=str(destination),
    )
    return destination


__all__ = [
    "BUCKET_ROLE",
    "CLOUD_PLATFORM_SCOPE",
    "KEY_FILE_ENV",
    "READ_ONLY_SCOPE",
    "SERVICE_ACCOUNT_ENV",
    "TOKEN_CREATOR_ROLE",
    "USERINFO_EMAIL_SCOPE",
    "AdcIdentity",
    "BucketIam",
    "ExportAccountRecord",
    "GcsAuthError",
    "HttpClient",
    "HttpResponse",
    "OperatorContext",
    "PartialProvisionError",
    "RevokeResult",
    "create_export_key",
    "default_adc_path",
    "default_bucket_iam",
    "default_http_client",
    "default_impersonation_probe",
    "describe_adc",
    "describe_credentials",
    "diagnose_google_error",
    "export_account_email",
    "export_account_id",
    "export_credentials",
    "provision_export_account",
    "resolve_operator_context",
    "resolve_operator_email",
    "revoke_export_account",
    "user_credentials",
    "verify_impersonation",
]
