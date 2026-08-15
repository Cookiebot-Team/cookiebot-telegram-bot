"""`cb_api.miniapp` — Telegram's `initData`, and every way it can be wrong.

The fixtures build `initData` from Telegram's published algorithm rather than
by calling the module under test, for the same reason `conftest.sign` does for
the login widget: a signer written in terms of the verifier agrees with it even
when both are wrong.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from cb_api import miniapp

BOT_TOKEN = "123456:AAH-fake-token-for-tests"
OTHER_TOKEN = "999999:BBH-some-other-bot"
USER_ID = 424243
AUTH_DATE = 1_754_000_000

InitDataFactory = Callable[..., str]


def _sign(fields: dict[str, str], token: str) -> str:
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def init_data() -> InitDataFactory:
    """Real-shaped `initData`: a JSON `user`, a `query_id`, an `auth_date`, and
    the hash over all of it."""

    def _build(
        *,
        token: str = BOT_TOKEN,
        user_id: int | None = USER_ID,
        auth_date: int = AUTH_DATE,
        extra: dict[str, str] | None = None,
        signature: str | None = None,
    ) -> str:
        fields: dict[str, str] = {
            "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
            "auth_date": str(auth_date),
            **(extra or {}),
        }
        if user_id is not None:
            fields["user"] = json.dumps(
                {
                    "id": user_id,
                    "first_name": "Tester",
                    "username": "tester",
                    "language_code": "pt",
                },
                separators=(",", ":"),
            )
        signed = dict(fields)
        signed["hash"] = _sign(fields, token)
        if signature is not None:
            # Bot API 7.10's Ed25519 signature: present in real payloads and
            # deliberately outside the HMAC.
            signed["signature"] = signature
        return urlencode(signed)

    return _build


def test_a_real_payload_verifies(init_data: InitDataFactory) -> None:
    fields = miniapp.parse_init_data(init_data())
    assert miniapp.validate_init_data(fields, BOT_TOKEN)


def test_another_bots_token_does_not_verify(init_data: InitDataFactory) -> None:
    fields = miniapp.parse_init_data(init_data())
    assert not miniapp.validate_init_data(fields, OTHER_TOKEN)


def test_a_tampered_field_does_not_verify(init_data: InitDataFactory) -> None:
    fields = miniapp.parse_init_data(init_data())
    fields["user"] = fields["user"].replace(str(USER_ID), "1")
    assert not miniapp.validate_init_data(fields, BOT_TOKEN)


def test_the_signature_field_is_not_part_of_the_hash(init_data: InitDataFactory) -> None:
    """A payload carrying Telegram's third-party signature still verifies —
    including it in the data-check string would break every real Mini App."""
    fields = miniapp.parse_init_data(init_data(signature="Zm9vYmFy"))
    assert miniapp.validate_init_data(fields, BOT_TOKEN)


def test_an_unknown_future_field_is_signed_and_kept(init_data: InitDataFactory) -> None:
    """Telegram may add fields. They are part of what it signed, so dropping
    one here would reject a valid payload."""
    fields = miniapp.parse_init_data(init_data(extra={"chat_type": "supergroup"}))
    assert miniapp.validate_init_data(fields, BOT_TOKEN)


def test_a_missing_hash_is_a_refusal_not_an_exception(init_data: InitDataFactory) -> None:
    fields = miniapp.parse_init_data(init_data())
    del fields["hash"]
    assert not miniapp.validate_init_data(fields, BOT_TOKEN)


def test_an_empty_token_is_a_refusal(init_data: InitDataFactory) -> None:
    """A deployment with an unconfigured skin loops over an empty token; it must
    not raise, because the next token in the loop might be the right one."""
    fields = miniapp.parse_init_data(init_data())
    assert not miniapp.validate_init_data(fields, "")


def test_the_user_id_is_read_out_of_the_json(init_data: InitDataFactory) -> None:
    fields = miniapp.parse_init_data(init_data())
    assert miniapp.user_id(fields) == USER_ID
    assert (miniapp.user(fields) or {})["username"] == "tester"


def test_a_payload_with_no_user_has_no_subject(init_data: InitDataFactory) -> None:
    """Telegram omits `user` when the Mini App was opened from a channel's
    inline keyboard. There is nobody to issue a token for."""
    fields = miniapp.parse_init_data(init_data(user_id=None))
    assert miniapp.user_id(fields) is None


def test_broken_user_json_is_not_an_exception(init_data: InitDataFactory) -> None:
    fields = miniapp.parse_init_data(init_data())
    fields["user"] = "{not json"
    assert miniapp.user(fields) is None
    assert miniapp.user_id(fields) is None


@pytest.mark.parametrize(
    ("age_seconds", "expected"),
    [(0, True), (60, True), (3600, True), (86_401, False)],
)
def test_freshness_window(init_data: InitDataFactory, age_seconds: int, expected: bool) -> None:
    fields = miniapp.parse_init_data(init_data())
    assert miniapp.is_fresh(fields, 86_400, now=AUTH_DATE + age_seconds) is expected


def test_a_window_of_zero_accepts_anything(init_data: InitDataFactory) -> None:
    fields = miniapp.parse_init_data(init_data())
    assert miniapp.is_fresh(fields, 0, now=AUTH_DATE + 10_000_000)


def test_a_payload_stamped_far_in_the_future_is_not_fresh(init_data: InitDataFactory) -> None:
    """Otherwise a caller could extend its own window by lying about when the
    payload was made."""
    fields = miniapp.parse_init_data(init_data())
    assert not miniapp.is_fresh(fields, 3600, now=AUTH_DATE - 3600)


def test_an_unparseable_auth_date_fails_a_window_that_is_on(
    init_data: InitDataFactory,
) -> None:
    fields = miniapp.parse_init_data(init_data())
    fields["auth_date"] = "yesterday"
    assert not miniapp.is_fresh(fields, 3600, now=AUTH_DATE)


def test_blank_values_survive_parsing(init_data: InitDataFactory) -> None:
    """`keep_blank_values`: Telegram signs `foo=` as `foo=`, and dropping the
    pair changes the digest."""
    fields = miniapp.parse_init_data(init_data(extra={"start_param": ""}))
    assert fields["start_param"] == ""
    assert miniapp.validate_init_data(fields, BOT_TOKEN)
