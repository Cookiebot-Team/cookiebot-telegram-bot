"""Unit tests for the importer's mapper layer — pure functions, no infrastructure.

Every case feeds a plain `dict` (a `Document`) into a `map_*` function and asserts
the exact row tuple(s) landed in a fresh `MappedRows`, or the exact `Skipped`
reason when the document cannot be mapped. No Mongo, no Postgres, no event loop.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from cb_core.group_config import DEFAULTS
from cb_worker.importer import Document, MappedRows
from cb_worker.importer.mappers import (
    MAPPERS,
    map_blacklist,
    map_configs,
    map_groups,
    map_randomdatabase,
    map_rules,
    map_stickerdatabase,
    map_users,
    map_welcomes,
)

# ---------------------------------------------------------------------- configs

_FULL_CONFIG_DOC: Document = {
    "_id": "-1001234567890",
    "furbots": False,
    "stickerSpamLimit": "7",
    "timeWithoutSendingImages": 900,
    "timeCaptcha": 400,
    "functionsFun": False,
    "functionsUtility": False,
    "sfw": False,
    "language": "pt",
    "publisherPost": True,
    "publisherAsk": False,
    "publisherMembersOnly": True,
    "threadPosts": "555",
    "maxPosts": 42,
}

_FULL_CONFIG_ROW = (
    -1001234567890,
    False,  # allow_furbots
    7,  # sticker_spam_limit (string "7" -> int)
    DEFAULTS.sticker_spam_window_s,  # v2-only, no v1 field
    900,  # media_restrict_seconds
    400,  # captcha_timeout_seconds
    False,  # functions_fun
    False,  # functions_utility
    False,  # sfw
    "pt",  # language, verbatim
    True,  # publisher_post
    False,  # publisher_ask
    True,  # publisher_members_only
    "555",  # thread_posts
    42,  # max_posts
    DEFAULTS.doomlist_enabled,  # v2-only, no v1 field
)


def test_map_configs_full_document() -> None:
    out = MappedRows()
    map_configs(_FULL_CONFIG_DOC, out)
    assert out.rows == {"group_configs": [_FULL_CONFIG_ROW]}
    assert out.skipped == []


def test_map_configs_empty_document_is_all_defaults() -> None:
    out = MappedRows()
    map_configs({"_id": "123"}, out)
    assert out.rows["group_configs"] == [
        (
            123,
            DEFAULTS.allow_furbots,
            DEFAULTS.sticker_spam_limit,
            DEFAULTS.sticker_spam_window_s,
            DEFAULTS.media_restrict_seconds,
            DEFAULTS.captcha_timeout_seconds,
            DEFAULTS.functions_fun,
            DEFAULTS.functions_utility,
            DEFAULTS.sfw,
            DEFAULTS.language,
            DEFAULTS.publisher_post,
            DEFAULTS.publisher_ask,
            DEFAULTS.publisher_members_only,
            DEFAULTS.thread_posts,
            DEFAULTS.max_posts,
            DEFAULTS.doomlist_enabled,
        )
    ]


@pytest.mark.parametrize(
    ("field", "value", "expected_index", "expected"),
    [
        # stickerSpamLimit: Config.java:23 types it as String; a null/absent
        # value falls back to v1's real default (Configurations.py:111).
        ("stickerSpamLimit", "9", 2, 9),
        ("stickerSpamLimit", None, 2, DEFAULTS.sticker_spam_limit),
        # a corrupt (non-numeric) string is treated the same as absent, not
        # propagated into a NOT NULL int column.
        ("stickerSpamLimit", "not-a-number", 2, DEFAULTS.sticker_spam_limit),
        ("timeWithoutSendingImages", None, 4, DEFAULTS.media_restrict_seconds),
        ("timeWithoutSendingImages", 120, 4, 120),
        ("timeCaptcha", None, 5, DEFAULTS.captcha_timeout_seconds),
        ("timeCaptcha", 60, 5, 60),
        ("maxPosts", None, 14, DEFAULTS.max_posts),
        ("maxPosts", 10, 14, 10),
        ("furbots", None, 1, DEFAULTS.allow_furbots),
        ("furbots", True, 1, True),
        ("functionsFun", None, 6, DEFAULTS.functions_fun),
        ("functionsUtility", None, 7, DEFAULTS.functions_utility),
        ("sfw", None, 8, DEFAULTS.sfw),
        ("language", None, 9, DEFAULTS.language),
        ("publisherPost", None, 10, DEFAULTS.publisher_post),
        ("publisherAsk", None, 11, DEFAULTS.publisher_ask),
        ("publisherMembersOnly", None, 12, DEFAULTS.publisher_members_only),
    ],
)
def test_map_configs_field_conversion(
    field: str, value: object, expected_index: int, expected: object
) -> None:
    doc: Document = {"_id": "1", field: value} if value is not None else {"_id": "1"}
    out = MappedRows()
    map_configs(doc, out)
    row = out.rows["group_configs"][0]
    assert row[expected_index] == expected


@pytest.mark.parametrize(
    ("thread_posts_value", "expected"),
    [
        ("9999", None),  # v1's "no topic" sentinel -> v2's NULL
        (None, DEFAULTS.thread_posts),  # absent -> same sentinel meaning
        ("42", "42"),  # a real topic id passes through as text, unmodified
        ("0", "0"),
    ],
)
def test_map_configs_thread_posts_sentinel(
    thread_posts_value: str | None, expected: str | None
) -> None:
    doc: Document = {"_id": "1"}
    if thread_posts_value is not None:
        doc = {"_id": "1", "threadPosts": thread_posts_value}
    out = MappedRows()
    map_configs(doc, out)
    assert out.rows["group_configs"][0][13] == expected


@pytest.mark.parametrize("literal", ["eng", "pt", "es"])
def test_map_configs_language_stored_verbatim(literal: str) -> None:
    """v1's literal strings ("eng"/"pt"/"es") are stored as-is — normalising here
    would make the imported row disagree with what `/config` writes at runtime.
    """
    out = MappedRows()
    map_configs({"_id": "1", "language": literal}, out)
    assert out.rows["group_configs"][0][9] == literal


def test_map_configs_unparseable_id_is_skipped() -> None:
    out = MappedRows()
    map_configs({"_id": "not-an-id", "furbots": True}, out)
    assert out.rows == {}
    assert len(out.skipped) == 1
    skipped = out.skipped[0]
    assert skipped.collection == "configs"
    assert skipped.document_id == "not-an-id"


def test_map_configs_empty_document_is_skipped() -> None:
    out = MappedRows()
    map_configs({}, out)
    assert out.rows == {}
    assert len(out.skipped) == 1
    assert out.skipped[0].document_id == ""


def test_map_configs_ignores_unexpected_extra_keys() -> None:
    out = MappedRows()
    map_configs({"_id": "1", "someFutureField": {"nested": True}, "another": [1, 2]}, out)
    assert out.rows["group_configs"][0][0] == 1
    assert out.skipped == []


# ------------------------------------------------------------------------- rules


def test_map_rules_valid_document() -> None:
    out = MappedRows()
    map_rules({"_id": "42", "rules": "Be nice."}, out)
    assert out.rows == {"group_rules": [(42, "Be nice.")]}
    assert out.skipped == []


def test_map_rules_missing_body_is_skipped() -> None:
    out = MappedRows()
    map_rules({"_id": "42"}, out)
    assert out.rows == {}
    assert out.skipped[0].collection == "rules"
    assert out.skipped[0].document_id == "42"


def test_map_rules_unparseable_id_is_skipped() -> None:
    out = MappedRows()
    map_rules({"_id": "abc", "rules": "text"}, out)
    assert out.rows == {}
    assert out.skipped[0].document_id == "abc"


def test_map_rules_empty_document_is_skipped() -> None:
    out = MappedRows()
    map_rules({}, out)
    assert out.rows == {}
    assert len(out.skipped) == 1


# --------------------------------------------------------------------- welcomes


def test_map_welcomes_valid_document() -> None:
    out = MappedRows()
    map_welcomes({"_id": "42", "message": "Welcome!"}, out)
    assert out.rows == {"group_welcomes": [(42, "Welcome!")]}


def test_map_welcomes_missing_body_is_skipped() -> None:
    out = MappedRows()
    map_welcomes({"_id": "42"}, out)
    assert out.rows == {}
    assert out.skipped[0].collection == "welcomes"


def test_map_welcomes_empty_document_is_skipped() -> None:
    out = MappedRows()
    map_welcomes({}, out)
    assert out.rows == {}
    assert len(out.skipped) == 1


# ------------------------------------------------------------------------- users

_BIRTHDATE_CASES: list[tuple[object, date | None]] = [
    (datetime(1990, 5, 17), date(1990, 5, 17)),  # BSON datetime (JSR-310 storage)
    (date(1990, 5, 17), date(1990, 5, 17)),  # already a date
    ("1990-05-17", date(1990, 5, 17)),  # ISO string (e.g. a raw JSON dump)
    (None, None),  # absent
    ("not-a-date", None),  # malformed -> treated as absent, not guessed at
    (12345, None),  # unexpected type -> treated as absent
]


@pytest.mark.parametrize(("raw", "expected"), _BIRTHDATE_CASES)
def test_map_users_birthdate_conversion(raw: object, expected: date | None) -> None:
    doc: Document = {"_id": "1"}
    if raw is not None:
        doc = {"_id": "1", "birthdate": raw}
    out = MappedRows()
    map_users(doc, out)
    assert out.rows["users"][0][5] == expected


def test_map_users_full_document() -> None:
    out = MappedRows()
    map_users(
        {
            "_id": "555",
            "username": "furrybean",
            "firstName": "Bean",
            "lastName": "Furry",
            "languageCode": "en",
            "birthdate": datetime(2000, 1, 2),
        },
        out,
    )
    assert out.rows == {"users": [(555, "furrybean", "Bean", "Furry", "en", date(2000, 1, 2))]}
    assert out.skipped == []


def test_map_users_optional_fields_absent() -> None:
    out = MappedRows()
    map_users({"_id": "555"}, out)
    assert out.rows == {"users": [(555, None, None, None, None, None)]}


def test_map_users_unparseable_id_is_skipped() -> None:
    out = MappedRows()
    map_users({"_id": "not-an-id"}, out)
    assert out.rows == {}
    assert out.skipped[0].collection == "users"


def test_map_users_empty_document_is_skipped() -> None:
    out = MappedRows()
    map_users({}, out)
    assert out.rows == {}
    assert len(out.skipped) == 1


# ---------------------------------------------------------------------- blacklist


@pytest.mark.parametrize(
    ("raw_id", "expected_id", "expected_kind"),
    [
        ("12345", 12345, "user"),  # positive -> user (Telegram user id convention)
        ("-100987654321", -100987654321, "chat"),  # negative -> chat/supergroup
        ("-4321", -4321, "chat"),  # negative -> plain group chat
    ],
)
def test_map_blacklist_kind_from_id_sign(raw_id: str, expected_id: int, expected_kind: str) -> None:
    out = MappedRows()
    map_blacklist({"_id": raw_id}, out)
    assert out.rows == {"blacklist": [(expected_id, expected_kind, None, "manual")]}


def test_map_blacklist_unparseable_id_is_skipped() -> None:
    out = MappedRows()
    map_blacklist({"_id": "abc"}, out)
    assert out.rows == {}
    assert out.skipped[0].collection == "blacklist"


def test_map_blacklist_empty_document_is_skipped() -> None:
    out = MappedRows()
    map_blacklist({}, out)
    assert out.rows == {}
    assert len(out.skipped) == 1


# ------------------------------------------------------------------------- groups


def test_map_groups_full_document_with_admins() -> None:
    out = MappedRows()
    map_groups(
        {
            "_id": "unrelated-mongo-object-id",
            "groupId": "-100111222333",
            "name": "Furry Friends",
            "imageUrl": "https://example.com/pic.jpg",
            "adminUsers": ["111", "222"],
        },
        out,
    )
    assert out.rows["groups"] == [(-100111222333, "Furry Friends", "https://example.com/pic.jpg")]
    assert sorted(out.rows["group_admins"]) == sorted(
        [
            (-100111222333, 111, "administrator", False),
            (-100111222333, 222, "administrator", False),
        ]
    )
    assert out.skipped == []


def test_map_groups_no_admins() -> None:
    out = MappedRows()
    map_groups({"groupId": "-1", "name": "n", "imageUrl": None}, out)
    assert out.rows == {"groups": [(-1, "n", None)]}
    assert "group_admins" not in out.rows


def test_map_groups_one_bad_admin_entry_is_skipped_not_the_whole_group() -> None:
    out = MappedRows()
    map_groups({"groupId": "-1", "name": "n", "adminUsers": ["111", "not-an-id"]}, out)
    assert out.rows["groups"] == [(-1, "n", None)]
    assert out.rows["group_admins"] == [(-1, 111, "administrator", False)]
    assert len(out.skipped) == 1
    assert out.skipped[0].collection == "groups"
    assert "not-an-id" in out.skipped[0].reason


def test_map_groups_unparseable_group_id_is_skipped() -> None:
    out = MappedRows()
    map_groups({"groupId": "not-an-id", "name": "n"}, out)
    assert out.rows == {}
    assert out.skipped[0].collection == "groups"


def test_map_groups_missing_group_id_is_skipped() -> None:
    """`Group.java` has no `@Id`; the real Mongo `_id` is an unrelated ObjectId,
    so a document missing `groupId` cannot be mapped at all.
    """
    out = MappedRows()
    map_groups({"_id": "some-object-id", "name": "n"}, out)
    assert out.rows == {}
    assert len(out.skipped) == 1


def test_map_groups_empty_document_is_skipped() -> None:
    out = MappedRows()
    map_groups({}, out)
    assert out.rows == {}
    assert len(out.skipped) == 1


# ------------------------------------------------------------------ randomdatabase


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"_id": "123", "idMessage": "456", "idMedia": "AgADBAAD"},
        {"_id": "123", "idMessage": "456", "idMedia": ""},
    ],
)
def test_map_randomdatabase_always_skipped(doc: Document) -> None:
    """No v1 document can be mapped: v2's `media_objects` requires a real
    `content_hash`/`blob_key`/`byte_size`, none of which v1 ever recorded — see
    the module docstring on `map_randomdatabase`.
    """
    out = MappedRows()
    map_randomdatabase(doc, out)
    assert out.rows == {}
    assert len(out.skipped) == 1
    assert out.skipped[0].collection == "randomdatabase"


# ------------------------------------------------------------------ stickerdatabase


@pytest.mark.parametrize("doc", [{}, {"_id": "AgADBAADsticker"}])
def test_map_stickerdatabase_always_skipped(doc: Document) -> None:
    out = MappedRows()
    map_stickerdatabase(doc, out)
    assert out.rows == {}
    assert len(out.skipped) == 1
    assert out.skipped[0].collection == "stickerdatabase"


# -------------------------------------------------------------------------- MAPPERS


def test_mappers_registry_covers_every_collection() -> None:
    assert set(MAPPERS) == {
        "configs",
        "rules",
        "welcomes",
        "users",
        "blacklist",
        "groups",
        "randomdatabase",
        "stickerdatabase",
    }
    assert MAPPERS["configs"] is map_configs
    assert MAPPERS["rules"] is map_rules
    assert MAPPERS["welcomes"] is map_welcomes
    assert MAPPERS["users"] is map_users
    assert MAPPERS["blacklist"] is map_blacklist
    assert MAPPERS["groups"] is map_groups
    assert MAPPERS["randomdatabase"] is map_randomdatabase
    assert MAPPERS["stickerdatabase"] is map_stickerdatabase
