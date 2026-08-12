"""CLI entry point for GCS auth/provisioning: `python -m
cb_worker.bucket_export.gcp_auth_cli <status|provision|revoke> [...]`, wired to
`python scripts/cb.py gcs-auth`.

    python scripts/cb.py gcs-auth status
    python scripts/cb.py gcs-auth provision --bucket cookiebot-bucket --operator you@example.com
    python scripts/cb.py gcs-auth revoke --service-account cb-bucket-export-...@proj.iam.gserviceaccount.com --bucket cookiebot-bucket

Deliberately its own module rather than folded into `bucket_export/__main__.py`
(`python -m cb_worker.bucket_export`, which runs an export): that module's only
job is running against credentials `source.open_source` already resolved. This
one is the tool that gets an operator from "I have a Google account with
access to v1's bucket" to "there is a scoped credential `bucket-export` can
use" and back again — a different day, a different audience, a different
lifecycle. Argument parsing, env lookup and the confirmation prompt live here;
every decision that talks to Google lives in `gcp_auth.py`, unit-tested
without a process boundary or real credentials, the same split every other
`__main__.py` in this package keeps.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from google.cloud import storage
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.prompt import Confirm
from rich.table import Table

from cb_core.logging import configure_logging, get_logger
from cb_core.settings import get_settings
from cb_worker.bucket_export import gcp_auth

log = get_logger("cb.bucket_export.gcp_auth_cli")


# --------------------------------------------------------------------- status


@dataclass(frozen=True, slots=True)
class StatusRow:
    check: str
    value: str
    style: str | None = None


def collect_status(bucket_name: str, *, project: str = "") -> list[StatusRow]:
    """Every fact `status` reports, read-only. Never a mutating call: the ADC
    check is a file read, `resolve_operator_context` is one credential fetch
    plus (when the ADC file itself has no email, i.e. a personal account) one
    `userinfo` read, and the bucket check — the only call that can touch the
    v1 bucket itself — is a single `list_blobs(max_results=1)`, the same read
    `source.py` itself is scoped to make, never a write.
    """
    rows: list[StatusRow] = []

    adc = gcp_auth.describe_adc()
    if adc is None:
        rows.append(
            StatusRow(
                "ADC credential",
                "not found — run `gcloud auth application-default login`",
                "yellow",
            )
        )
    else:
        rows.append(StatusRow("ADC credential", f"{adc.credential_type} ({adc.path})", "green"))
        if adc.quota_project_id:
            # Its own row, deliberately: this is the field most likely to be
            # the cause when every other row looks fine — see
            # `AdcIdentity.quota_project_id` and `diagnose_google_error`.
            rows.append(
                StatusRow(
                    "ADC quota project",
                    f"{adc.quota_project_id} (adds x-goog-user-project to every request — needs "
                    "roles/serviceusage.serviceUsageConsumer there)",
                    "yellow",
                )
            )

    context = gcp_auth.resolve_operator_context(explicit_project=project.strip() or None)
    if context.error:
        rows.append(StatusRow("operator identity", f"unresolved — {context.error}", "yellow"))
        rows.append(StatusRow("project", "unresolved (no credential to resolve it from)", "yellow"))
    else:
        rows.append(
            StatusRow(
                "operator identity",
                context.email or f"unresolved ({context.email_source})",
                "green" if context.email else "yellow",
            )
        )
        rows.append(
            StatusRow(
                "project",
                f"{context.project} ({context.project_source})"
                if context.project
                else context.project_source,
                "green" if context.project else "yellow",
            )
        )

    configured_sa = os.environ.get(gcp_auth.SERVICE_ACCOUNT_ENV, "").strip()
    rows.append(
        StatusRow(
            "configured export service account",
            configured_sa or f"not set ({gcp_auth.SERVICE_ACCOUNT_ENV})",
            "green" if configured_sa else "yellow",
        )
    )

    if not bucket_name:
        rows.append(
            StatusRow(
                "bucket listable",
                "skip — no bucket configured (pass --bucket or set CB_BUCKET_EXPORT_SOURCE_BUCKET)",
                "yellow",
            )
        )
    else:
        try:
            credentials, _ = gcp_auth.export_credentials()
        except gcp_auth.GcsAuthError as exc:
            rows.append(StatusRow("bucket listable", f"skip — {exc}", "yellow"))
        else:
            identity_label = gcp_auth.describe_credentials(credentials)
            try:
                client = storage.Client(credentials=credentials, project=None)
                next(iter(client.bucket(bucket_name).list_blobs(max_results=1)), None)
            except Exception as exc:  # noqa: BLE001 - any GCS failure is a status row, not a crash
                # The one-line summary goes in the table; the raw error
                # (which can be a dozen wrapped lines of JSON — `"@type"`,
                # `troubleshooter_url`, an `error_info_id`) goes to the log
                # for whoever needs it, never into a table cell.
                log.warning("bucket_export.gcp_auth_cli.bucket_check_failed", error=str(exc))
                detail = gcp_auth.diagnose_google_error(exc)
                rows.append(StatusRow("bucket listable", f"no — {identity_label}: {detail}", "red"))
            else:
                rows.append(StatusRow("bucket listable", f"yes — as {identity_label}", "green"))

    rows.append(StatusRow("read-only scope", gcp_auth.READ_ONLY_SCOPE))
    return rows


def render_status(rows: Sequence[StatusRow]) -> Table:
    table = Table(title="gcs-auth status")
    table.add_column("check")
    table.add_column("value")
    for row in rows:
        table.add_row(row.check, row.value, style=row.style)
    return table


def _cmd_status(args: argparse.Namespace, console: Console) -> int:
    console.print(render_status(collect_status(args.bucket.strip(), project=args.project.strip())))
    return 0


# ------------------------------------------------------------------ provision


@dataclass(frozen=True, slots=True)
class ProvisionPlan:
    """What `provision` intends to do, computable and printable with zero
    Google calls — this is what makes `--dry-run` genuinely free of mutating
    calls: the CLI never even reaches the functions in `gcp_auth.py` that make
    one unless `--dry-run` is absent."""

    service_account_email: str
    project: str | None
    bucket: str
    operator: str | None
    key_file: str | None


def build_provision_plan(
    *, stamp: str, project: str | None, bucket: str, operator: str | None, key_file: str | None
) -> ProvisionPlan:
    account_id = gcp_auth.export_account_id(stamp)
    email = (
        gcp_auth.export_account_email(account_id, project)
        if project
        else f"{account_id}@<project>.iam.gserviceaccount.com"
    )
    return ProvisionPlan(
        service_account_email=email,
        project=project,
        bucket=bucket,
        operator=operator,
        key_file=key_file,
    )


def render_provision_plan(plan: ProvisionPlan) -> Table:
    table = Table(title="gcs-auth provision plan")
    table.add_column("what")
    table.add_column("value")
    table.add_row("service account (create)", plan.service_account_email)
    table.add_row("project", plan.project or "<unresolved — pass --project or authenticate first>")
    table.add_row("grant", f"{gcp_auth.BUCKET_ROLE} on bucket {plan.bucket!r} only")
    table.add_row(
        "grant",
        f"{gcp_auth.TOKEN_CREATOR_ROLE} on the service account, to "
        f"{plan.operator or '<unresolved — pass --operator you@example.com>'}",
    )
    if plan.key_file:
        table.add_row(
            "standing key (avoid)",
            f"{plan.key_file} — impersonation is the default path; only pass --key-file when "
            "this environment genuinely cannot impersonate",
        )
    return table


def render_provisioned(record: gcp_auth.ExportAccountRecord) -> Table:
    table = Table(title="gcs-auth provision result")
    table.add_column("what")
    table.add_column("value")
    table.add_row("service account", record.service_account_email)
    table.add_row("bucket role", f"{record.bucket_role} on {record.bucket}")
    table.add_row(
        "impersonation",
        f"{record.token_creator_role} on the service account, granted to {record.operator_principal}",
    )
    return table


def _normalise_operator(raw: str) -> str | None:
    operator = raw.strip()
    if not operator:
        return None
    if operator.startswith(("user:", "group:", "serviceAccount:")):
        return operator
    return f"user:{operator}"


def _cmd_provision(args: argparse.Namespace, console: Console) -> int:
    stamp = datetime.now(UTC).strftime("%y%m%d%H%M%S")

    # `--operator`/`$CB_GCS_EXPORT_OPERATOR` is an override, not a requirement:
    # `resolve_operator_context` derives both the project and (for a personal
    # account whose ADC file carries no email) the operator's own email via
    # one `userinfo` call, so an operator who omits `--operator` still gets a
    # plan naming a real principal, not a placeholder no one can impersonate.
    explicit_operator = _normalise_operator(args.operator)
    context = gcp_auth.resolve_operator_context(explicit_project=args.project.strip() or None)
    operator = explicit_operator or (f"user:{context.email}" if context.email else None)
    project = context.project
    project_error = context.error or (
        None
        if project
        else "no project resolved; pass --project explicitly, or run "
        "`gcloud config set project <PROJECT_ID>`"
    )

    plan = build_provision_plan(
        stamp=stamp,
        project=project,
        bucket=args.bucket,
        operator=operator,
        key_file=args.key_file or None,
    )
    console.print(render_provision_plan(plan))

    if args.dry_run:
        console.print("\n[dim]dry run: nothing was created, nothing was granted[/dim]")
        return 0

    if project_error:
        print(f"error: {project_error}", file=sys.stderr)
        return 2
    if operator is None:
        print(
            "error: no operator principal known; pass --operator you@example.com", file=sys.stderr
        )
        return 2
    if context.credentials is None:
        # Unreachable in practice: `project_error` above already returns
        # whenever `resolve_operator_context` failed to authenticate at all
        # (`context.error` is set exactly when `context.credentials` is not).
        # Kept as an explicit check rather than an `assert` so a future change
        # to that invariant fails loudly here instead of with a `None` crash
        # three lines down.
        print("error: no usable Google credentials", file=sys.stderr)
        return 2
    assert project is not None  # narrowed: project_error would have returned above otherwise

    if not args.yes and not Confirm.ask(
        "create this service account and grant these roles?", console=console, default=False
    ):
        print("aborted")
        return 2

    http = gcp_auth.default_http_client(context.credentials)
    bucket = gcp_auth.default_bucket_iam(args.bucket, context.credentials, project)

    # `record`/`provision_error` are set inside the `with Progress(...)` block
    # but only acted on after it exits — returning from *inside* an active
    # `rich.progress.Progress`/`Live` context (which owns the terminal's
    # cursor and redraw loop) interleaves badly with a plain `print()` to
    # `sys.stderr` and, empirically, can leave the console's output stream in
    # a broken state for whatever runs next. Letting the `with` block close
    # cleanly first, unconditionally, sidesteps that by construction.
    record: gcp_auth.ExportAccountRecord | None = None
    provision_error: Exception | None = None
    with Progress(
        TextColumn("[bold blue]{task.fields[step]}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("", step="creating account, granting roles", total=1)

        def _on_retry(grant: str, attempt: int, total: int) -> None:
            # A newly created service account is not immediately visible to
            # Cloud Storage's own IAM checks (`provision_export_account`'s
            # own docstring) — this is what keeps that short wait from
            # reading as a hang: the operator sees exactly what it is
            # waiting on, not a frozen progress bar.
            progress.update(
                task_id,
                step=f"waiting for the new service account to propagate ({grant}, attempt {attempt}/{total})",
            )

        try:
            record = gcp_auth.provision_export_account(
                project=project,
                bucket_name=args.bucket,
                operator_principal=operator,
                stamp=stamp,
                http=http,
                bucket=bucket,
                on_retry=_on_retry,
            )
        except Exception as exc:  # noqa: BLE001 - any IAM/storage failure is "the operation failed"
            provision_error = exc
        else:
            progress.update(task_id, advance=1)

    if provision_error is not None:
        log.warning(
            "bucket_export.gcp_auth_cli.provision_failed",
            bucket=args.bucket,
            error=str(provision_error),
        )
        print(
            f"error: provisioning failed: {gcp_auth.diagnose_google_error(provision_error)}",
            file=sys.stderr,
        )
        if isinstance(provision_error, gcp_auth.PartialProvisionError):
            # The account exists but is not (fully) granted — say so and give
            # the exact command to remove it, rather than leaving a silent
            # orphan an operator has to notice on their own later.
            leftover_email = provision_error.service_account_email
            revoke_cmd = f"python scripts/cb.py gcs-auth revoke --service-account {leftover_email} --bucket {args.bucket}"
            if project:
                revoke_cmd += f" --project {project}"
            print(
                f"a service account was created but not fully granted: {leftover_email}\n"
                f"clean it up with:\n  {revoke_cmd}",
                file=sys.stderr,
            )
        return 1
    assert record is not None  # narrowed: provision_error would have returned above otherwise

    # The tokenCreator grant just made does not take effect the instant the
    # API call granting it returned 200 (`gcp_auth.verify_impersonation`'s own
    # docstring — measured against a real project, not assumed). Waiting for
    # it here, before declaring success, is what keeps the very next command
    # the operator runs — the one this function's own output is about to
    # tell them to run — from failing the moment they try it.
    verify_error: Exception | None = None
    with Progress(
        TextColumn("[bold blue]{task.fields[step]}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task(
            "", step="waiting for the impersonation grant to take effect", total=1
        )

        def _on_verify_retry(attempt: int, total: int) -> None:
            progress.update(
                task_id,
                step=f"waiting for the impersonation grant to take effect (attempt {attempt}/{total})",
            )

        try:
            gcp_auth.verify_impersonation(
                gcp_auth.default_impersonation_probe(
                    context.credentials, record.service_account_email
                ),
                on_attempt=_on_verify_retry,
            )
        except Exception as exc:  # noqa: BLE001 - reported below, alongside the account it still names
            verify_error = exc
        else:
            progress.update(task_id, advance=1)

    if args.key_file:
        console.print(
            "[bold red]warning:[/bold red] writing a standing service-account key — prefer "
            "impersonation (the default). Delete this file and run `gcs-auth revoke` as soon as "
            "the export is done."
        )
        try:
            gcp_auth.create_export_key(
                service_account_email=record.service_account_email,
                project=project,
                destination=Path(args.key_file),
                http=http,
            )
        except Exception as exc:  # noqa: BLE001 - a key-creation failure is "the operation failed"
            log.warning(
                "bucket_export.gcp_auth_cli.key_creation_failed",
                service_account=record.service_account_email,
                error=str(exc),
            )
            print(
                f"error: key creation failed: {gcp_auth.diagnose_google_error(exc)}",
                file=sys.stderr,
            )
            return 1

    console.print(render_provisioned(record))
    console.print(f"\n{gcp_auth.SERVICE_ACCOUNT_ENV}={record.service_account_email}")

    if verify_error is not None:
        # The account and both grants genuinely exist — `render_provisioned`
        # above is accurate — but impersonation could not be confirmed
        # working within the wait budget, so this is not a clean success:
        # the exit code says so, and the message says what to do about it
        # rather than leaving the operator to guess why the export line they
        # were just handed might not work yet.
        log.warning(
            "bucket_export.gcp_auth_cli.impersonation_not_verified",
            service_account=record.service_account_email,
            error=str(verify_error),
        )
        status_cmd = f"python scripts/cb.py gcs-auth status --bucket {args.bucket}"
        if project:
            status_cmd += f" --project {project}"
        print(
            f"\nwarning: impersonation could not be confirmed working yet: "
            f"{gcp_auth.diagnose_google_error(verify_error)}\n"
            f"the account and its grants exist — this is usually the grant still taking effect, "
            f"not a real problem; re-check in a minute or two with:\n  {status_cmd}",
            file=sys.stderr,
        )
        return 1

    return 0


# --------------------------------------------------------------------- revoke


def render_revoked(result: gcp_auth.RevokeResult, *, service_account: str, bucket: str) -> Table:
    table = Table(title="gcs-auth revoke result")
    table.add_column("what")
    table.add_column("value")
    table.add_row(
        "bucket binding",
        f"removed from {bucket}"
        if result.bucket_binding_removed
        else f"already absent from {bucket}",
    )
    table.add_row(
        "service account",
        f"{service_account} deleted"
        if result.service_account_deleted
        else f"{service_account} already gone",
    )
    return table


def _cmd_revoke(args: argparse.Namespace, console: Console) -> int:
    table = Table(title="gcs-auth revoke plan")
    table.add_column("what")
    table.add_column("value")
    table.add_row("service account (delete)", args.service_account)
    table.add_row("bucket grant (remove)", f"{gcp_auth.BUCKET_ROLE} on {args.bucket}")
    console.print(table)

    if not args.yes and not Confirm.ask(
        "remove this grant and delete this service account?", console=console, default=False
    ):
        print("aborted")
        return 2

    # Same resolution `provision` uses (`--project` first, else the
    # operator's ADC project) — the natural next step after copy-pasting the
    # `gcs-auth provision --project ...` line that created this account is
    # copy-pasting the same `--project` into `revoke`, so it has to accept it.
    context = gcp_auth.resolve_operator_context(explicit_project=args.project.strip() or None)
    if context.error or context.credentials is None:
        print(f"error: {context.error or 'no usable Google credentials'}", file=sys.stderr)
        return 2
    if context.project is None:
        # Required, not defaulted to IAM's `-` wildcard — see the note above
        # `gcp_auth._create_service_account` for why that wildcard 404s on
        # the exact calls `revoke_export_account` makes.
        print("error: no project resolved; pass --project explicitly", file=sys.stderr)
        return 2

    http = gcp_auth.default_http_client(context.credentials)
    bucket = gcp_auth.default_bucket_iam(args.bucket, context.credentials, context.project)

    try:
        result = gcp_auth.revoke_export_account(
            service_account_email=args.service_account,
            project=context.project,
            bucket_name=args.bucket,
            http=http,
            bucket=bucket,
        )
    except Exception as exc:  # noqa: BLE001 - any IAM/storage failure is "the operation failed"
        log.warning(
            "bucket_export.gcp_auth_cli.revoke_failed",
            service_account=args.service_account,
            error=str(exc),
        )
        print(f"error: revoke failed: {gcp_auth.diagnose_google_error(exc)}", file=sys.stderr)
        return 1

    console.print(render_revoked(result, service_account=args.service_account, bucket=args.bucket))
    return 0


# ----------------------------------------------------------------------- CLI


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cb.py gcs-auth",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="read-only report: credentials found, bucket listable")
    status.add_argument(
        "--bucket",
        default=os.environ.get("CB_BUCKET_EXPORT_SOURCE_BUCKET", ""),
        help="bucket to test listability against (default: $CB_BUCKET_EXPORT_SOURCE_BUCKET)",
    )
    status.add_argument(
        "--project",
        default="",
        help="report this project instead of the operator's ADC-resolved one",
    )

    provision = sub.add_parser(
        "provision", help="create a temporary, bucket-scoped export service account"
    )
    provision.add_argument("--bucket", required=True, help="the v1 bucket to grant read access to")
    provision.add_argument(
        "--project",
        default="",
        help="GCP project to create the service account in (default: the operator's ADC project)",
    )
    provision.add_argument(
        "--operator",
        default=os.environ.get("CB_GCS_EXPORT_OPERATOR", ""),
        help="principal to grant impersonation to, e.g. you@example.com "
        "(default: $CB_GCS_EXPORT_OPERATOR)",
    )
    provision.add_argument(
        "--dry-run", action="store_true", help="print the plan; create and grant nothing"
    )
    provision.add_argument(
        "--yes", action="store_true", help="skip the confirmation prompt before a real run"
    )
    provision.add_argument(
        "--key-file",
        default="",
        help="also write a standing key to this path — avoid; impersonation is the default path",
    )

    revoke = sub.add_parser(
        "revoke", help="remove a temporary export service account and its bucket grant"
    )
    revoke.add_argument("--service-account", required=True, help="the email `provision` printed")
    revoke.add_argument("--bucket", required=True, help="the bucket the account was granted on")
    revoke.add_argument(
        "--project",
        default="",
        help="the project the account was created in (default: the operator's ADC project); "
        "accepted so the `provision --project ...` invocation that created an account can be "
        "copy-pasted straight into `revoke`",
    )
    revoke.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    settings.service_name = "cb-gcs-auth"
    configure_logging(settings)

    console = Console()
    if args.command == "status":
        return _cmd_status(args, console)
    if args.command == "provision":
        return _cmd_provision(args, console)
    if args.command == "revoke":
        return _cmd_revoke(args, console)
    raise AssertionError(
        f"unhandled gcs-auth command {args.command!r}"
    )  # argparse choices are exhaustive


if __name__ == "__main__":
    sys.exit(main())
