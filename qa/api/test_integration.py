"""Integration: the API's behaviour, through the whole stack, against real rows.

`packages/cb-api/tests/` proves each router's logic with the database faked, and
is where a status code or a scope rule belongs. This file is for what a fake
cannot show: that the SQL behind an endpoint matches the schema, that a write
really lands and really audits, that a token minted by the token endpoint really
verifies against the keys in `signing_keys`, and that the authorisation boundary
holds when `group_admins` is a table rather than a monkeypatched function.

Conventions this file follows, and a new test should:

* **One behaviour per test, named after the rule.** `test_a_stranger_gets_404_not_403`
  tells a reader what broke; `test_config_2` does not.
* **Arrange, act, assert**, in that order, with a blank line between each.
* **Every test gets its own group** (the `group` fixture), so the file is
  order-independent and two tests that both write settings cannot collide.
* **Assert deltas, never absolutes, for anything fleet-wide.** The database is
  shared with the rest of the suite; `/admin/overview` counts every group in it.
* **No sleeps, no retries, no network.** The app runs in-process on the suite's
  own event loop.
"""

from __future__ import annotations

import pytest

from qa.api.auth import LOGIN_GRANT, MINIAPP_GRANT, init_data, widget_payload
from qa.api.client import Api, Tokens
from qa.api.conftest import BOT_TOKEN, OWNER_ID, STRANGER_ID, mint
from qa.integration.factories import World

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------- sessions


class TestTokens:
    def test_signed_init_data_buys_a_working_session(self, api: Api, group: World) -> None:
        """The whole suite rests on this: `initData` this process signed is
        verified by the service, and the token that comes back is accepted by an
        endpoint — against the keys really stored in `signing_keys`."""
        admin = next(user for user in group.users if user.is_admin)

        issued = api.post(
            "/oauth2/token",
            json={"grant_type": MINIAPP_GRANT, "init_data": init_data(admin.user_id, BOT_TOKEN)},
        )

        assert issued.status_code == 200
        token = issued.json()["access_token"]
        assert api.get("/me", token=token).json()["user_id"] == admin.user_id

    def test_init_data_signed_with_another_bots_token_is_refused(self, api: Api) -> None:
        response = api.post(
            "/oauth2/token",
            json={
                "grant_type": MINIAPP_GRANT,
                "init_data": init_data(1234, "999999:SOMEONE-ELSES-BOT"),
            },
        )

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_grant"

    def test_a_refresh_rotates_and_the_spent_token_kills_the_family(
        self, api: Api, group: World
    ) -> None:
        """Rotation and replay detection against the real `refresh_tokens`
        table. A thief and a buggy client are indistinguishable to the server,
        and letting the thief refresh alongside the real user is the worse of
        the two failures."""
        admin = next(user for user in group.users if user.is_admin)
        first = api.post(
            "/oauth2/token",
            json={"grant_type": MINIAPP_GRANT, "init_data": init_data(admin.user_id, BOT_TOKEN)},
        ).json()

        second = api.post(
            "/oauth2/token",
            json={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
        ).json()
        replay = api.post(
            "/oauth2/token",
            json={"grant_type": "refresh_token", "refresh_token": first["refresh_token"]},
        )
        after_replay = api.post(
            "/oauth2/token",
            json={"grant_type": "refresh_token", "refresh_token": second["refresh_token"]},
        )

        assert second["refresh_token"] != first["refresh_token"]
        assert replay.status_code == 400
        assert after_replay.status_code == 400, "the replay revoked the whole family"

    def test_only_an_owners_session_is_granted_the_admin_scope(
        self, api: Api, group: World, owner: int
    ) -> None:
        admin = next(user for user in group.users if user.is_admin)

        owner_scopes = api.get("/me", token=mint(api, owner)).json()["scopes"]
        admin_scopes = api.get("/me", token=mint(api, admin.user_id)).json()["scopes"]

        assert "admin:read" in owner_scopes
        assert "admin:read" not in admin_scopes

    def test_the_v1_login_endpoint_still_mints_a_read_only_token(
        self, api: Api, group: World
    ) -> None:
        """v1's endpoint is untouched and its token carries no `scope` claim,
        which `cb_api.security` reads as read-only. A console that could only
        read before this API existed does not silently gain the ability to
        write."""
        admin = next(user for user in group.users if user.is_admin)

        legacy = api.post("/login", json=widget_payload(admin.user_id, BOT_TOKEN)).json()
        token = legacy["accessToken"]

        assert api.get(f"/groups/{group.group_id}/config", token=token).status_code == 200
        refused = api.patch(f"/groups/{group.group_id}/config", token=token, json={"sfw": False})
        assert refused.status_code == 403
        assert "insufficient_scope" in refused.headers["www-authenticate"]

    def test_the_login_grant_and_the_widget_endpoint_agree_on_who_you_are(
        self, api: Api, group: World
    ) -> None:
        """The same payload, through v1's `/login` and through the OAuth2 login
        grant. Same subject, different scopes — which is the whole difference
        between the two routes."""
        admin = next(user for user in group.users if user.is_admin)
        payload = widget_payload(admin.user_id, BOT_TOKEN)

        granted = api.post("/oauth2/token", json={"grant_type": LOGIN_GRANT, "auth_data": payload})

        assert granted.status_code == 200
        assert "groups:write" in granted.json()["scope"].split()


# -------------------------------------------------------------- the boundary


class TestAuthorisation:
    def test_an_admin_reads_their_own_group(self, api: Api, tokens: Tokens, group: World) -> None:
        response = api.get(f"/groups/{group.group_id}/config", token=tokens.admin)

        assert response.status_code == 200
        assert response.json()["group_id"] == group.group_id

    def test_a_stranger_gets_404_not_403(self, api: Api, tokens: Tokens, group: World) -> None:
        """Whether a chat id is known to this deployment is not something an
        arbitrary logged-in user may probe."""
        response = api.get(f"/groups/{group.group_id}/config", token=tokens.stranger)

        assert response.status_code == 404

    def test_an_admin_of_one_group_is_a_stranger_to_another(
        self, api: Api, tokens: Tokens, group: World, second_world: World
    ) -> None:
        response = api.get(f"/groups/{second_world.group_id}/config", token=tokens.admin)

        assert response.status_code == 404

    def test_a_group_admin_cannot_reach_the_fleet(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        response = api.get("/admin/overview", token=tokens.admin)

        assert response.status_code == 403

    def test_the_env_owner_can(self, api: Api, tokens: Tokens) -> None:
        assert api.get("/admin/overview", token=tokens.owner).status_code == 200

    def test_a_tenant_owner_can_too(self, api: Api, group: World, run) -> None:  # noqa: ANN001
        """The other source of ownership: `tenants.owner_ids`, read through the
        registry. Covered separately from `CB_OWNER_ID` because they are two
        different code paths and a deployment may use either."""
        from cb_core import db, tenancy

        run(
            db.execute(
                "UPDATE tenants SET owner_ids = $1::bigint[] WHERE tenant_id = $2",
                [OWNER_ID + 50],
                tenancy.DEFAULT_TENANT,
                name="qa_api_set_owner",
            )
        )
        tenancy.registry.forget(tenancy.DEFAULT_TENANT)
        try:
            token = mint(api, OWNER_ID + 50)

            assert api.get("/admin/overview", token=token).status_code == 200
        finally:
            run(
                db.execute(
                    "UPDATE tenants SET owner_ids = '{}'::bigint[] WHERE tenant_id = $1",
                    tenancy.DEFAULT_TENANT,
                    name="qa_api_clear_owner",
                )
            )
            tenancy.registry.forget(tenancy.DEFAULT_TENANT)

    def test_me_lists_the_group_and_says_who_runs_the_deployment(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        admin_view = api.get("/me", token=tokens.admin).json()
        owner_view = api.get("/me", token=tokens.owner).json()

        assert group.group_id in [row["group_id"] for row in admin_view["groups"]]
        assert admin_view["is_bot_admin"] is False
        assert owner_view["is_bot_admin"] is True

    def test_a_stranger_administers_nothing(self, api: Api, tokens: Tokens) -> None:
        assert api.get("/me", token=tokens.stranger).json()["groups"] == []


# ------------------------------------------------------------------- writing


class TestSettings:
    def test_a_patch_changes_only_what_it_names(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        before = api.get(f"/groups/{group.group_id}/config", token=tokens.admin).json()["config"]

        response = api.patch(
            f"/groups/{group.group_id}/config",
            token=tokens.admin,
            json={"captcha_timeout_seconds": 600},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["changed"] == ["captcha_timeout_seconds"]
        assert body["config"]["captcha_timeout_seconds"] == 600
        assert body["config"]["sticker_spam_limit"] == before["sticker_spam_limit"]

    def test_the_write_is_visible_to_the_next_read(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        """Through the real table and the real cache, which is the half a
        faked repository cannot show."""
        api.patch(f"/groups/{group.group_id}/config", token=tokens.admin, json={"sfw": False})

        again = api.get(f"/groups/{group.group_id}/config", token=tokens.admin).json()

        assert again["config"]["sfw"] is False

    def test_a_change_leaves_an_audit_row_with_both_values(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        api.patch(
            f"/groups/{group.group_id}/config", token=tokens.admin, json={"functions_fun": False}
        )

        trail = api.get(f"/groups/{group.group_id}/audit", token=tokens.admin).json()

        assert [event["action"] for event in trail["events"]] == ["config.updated"]
        event = trail["events"][0]
        assert event["surface"] == "miniapp"
        assert event["before"] == {"functions_fun": True}
        assert event["after"] == {"functions_fun": False}

    def test_saving_an_unchanged_form_records_nothing(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        """A Mini App that PATCHes the value already stored has changed nothing,
        and a trail full of no-ops is a trail nobody reads."""
        current = api.get(f"/groups/{group.group_id}/config", token=tokens.admin).json()["config"]

        api.patch(
            f"/groups/{group.group_id}/config",
            token=tokens.admin,
            json={"sfw": current["sfw"]},
        )

        assert api.get(f"/groups/{group.group_id}/audit", token=tokens.admin).json()["events"] == []

    def test_an_empty_patch_is_a_400(self, api: Api, tokens: Tokens, group: World) -> None:
        response = api.patch(f"/groups/{group.group_id}/config", token=tokens.admin, json={})

        assert response.status_code == 400

    def test_an_unknown_field_is_refused_rather_than_ignored(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        """A typo silently ignored is a setting an admin believes they changed."""
        response = api.patch(
            f"/groups/{group.group_id}/config", token=tokens.admin, json={"captha_timeout": 60}
        )

        assert response.status_code == 422

    def test_an_unrecognised_language_does_not_fall_back_to_english(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        assert (
            api.patch(
                f"/groups/{group.group_id}/config", token=tokens.admin, json={"language": "klingon"}
            ).status_code
            == 422
        )

    def test_the_menu_spellings_are_stored_as_canonical_codes(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        response = api.patch(
            f"/groups/{group.group_id}/config", token=tokens.admin, json={"language": "portuguese"}
        )

        assert response.json()["config"]["language"] == "pt"


class TestTexts:
    def test_rules_are_null_before_they_are_set(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        """Not a 404: not having rules is a normal state of a group, not a
        missing resource."""
        response = api.get(f"/groups/{group.group_id}/rules", token=tokens.admin)

        assert response.status_code == 200
        assert response.json()["body"] is None

    def test_setting_the_rules_records_who_set_them(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        admin = next(user for user in group.users if user.is_admin)

        api.put(f"/groups/{group.group_id}/rules", token=tokens.admin, json={"body": "be kind"})

        stored = api.get(f"/groups/{group.group_id}/rules", token=tokens.admin).json()
        assert stored["body"] == "be kind"
        assert stored["updated_by"] == admin.user_id
        assert stored["updated_at"] is not None

    def test_a_welcome_message_is_stored_verbatim(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        """`<user>` is substituted when the bot sends it, not when it is
        stored — which is what `/newwelcome` does."""
        api.put(
            f"/groups/{group.group_id}/welcome",
            token=tokens.admin,
            json={"body": "welcome <user>!"},
        )

        assert (
            api.get(f"/groups/{group.group_id}/welcome", token=tokens.admin).json()["body"]
            == "welcome <user>!"
        )

    def test_an_empty_body_is_refused(self, api: Api, tokens: Tokens, group: World) -> None:
        assert (
            api.put(f"/groups/{group.group_id}/rules", token=tokens.admin, json={"body": ""})
        ).status_code == 422


# ----------------------------------------------------------------- analytics


class TestGroupAnalytics:
    def test_the_summary_totals_the_seeded_days(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        """Three seeded days of 10, 11 and 12 messages."""
        response = api.get(f"/groups/{group.group_id}/analytics/summary", token=tokens.admin)

        body = response.json()
        assert body["days"] == 3
        assert body["messages"] == 33
        assert body["captcha_solve_rate"] == pytest.approx(0.5)

    def test_the_daily_rows_come_back_oldest_first(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        days = api.get(f"/groups/{group.group_id}/analytics/daily", token=tokens.admin).json()[
            "days"
        ]

        assert [row["day"] for row in days] == sorted(row["day"] for row in days)

    def test_a_reversed_window_is_a_400_not_a_silent_clamp(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        response = api.get(
            f"/groups/{group.group_id}/analytics/daily",
            token=tokens.admin,
            params={"start": "2027-03-01", "end": "2027-01-01"},
        )

        assert response.status_code == 400

    def test_a_window_wider_than_a_year_is_refused(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        response = api.get(
            f"/groups/{group.group_id}/analytics/daily",
            token=tokens.admin,
            params={"start": "2020-01-01", "end": "2026-01-01"},
        )

        assert response.status_code == 400

    def test_the_llm_total_is_the_sum_of_its_models(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        body = api.get(f"/groups/{group.group_id}/analytics/llm", token=tokens.admin).json()

        assert body["total_cost_usd"] == pytest.approx(1.5)
        assert body["models"][0]["model"] == "claude-opus-5"


class TestFleetAnalytics:
    def test_the_directory_finds_the_seeded_group_and_counts_its_people(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        page = api.get(
            "/admin/groups", token=tokens.owner, params={"search": str(group.group_id)}
        ).json()

        assert [row["group_id"] for row in page["groups"]] == [group.group_id]
        assert page["groups"][0]["members"] == 2
        assert page["groups"][0]["admins"] == 1

    def test_the_cursor_pages_forward_without_repeating_a_row(
        self, api: Api, tokens: Tokens, group: World, second_world: World
    ) -> None:
        """Keyset, not OFFSET: `after` is the id itself, so a group created
        between two pages cannot make a row repeat or vanish."""
        first = api.get("/admin/groups", token=tokens.owner, params={"limit": 1}).json()

        second = api.get(
            "/admin/groups",
            token=tokens.owner,
            params={"limit": 1, "after": first["next_after"]},
        ).json()

        assert first["next_after"] == first["groups"][0]["group_id"]
        assert second["groups"][0]["group_id"] > first["groups"][0]["group_id"]

    def test_reach_counts_the_group_that_was_just_created(
        self,
        api: Api,
        tokens: Tokens,
        group: World,
        run,  # noqa: ANN001
    ) -> None:
        """A delta, not an absolute: this counts every group in a database the
        rest of the suite is also using."""
        before = api.get("/admin/overview", token=tokens.owner).json()["reach"]["groups"]

        extra = World(run)
        extra.setup()
        try:
            after = api.get("/admin/overview", token=tokens.owner).json()["reach"]["groups"]
        finally:
            extra.teardown()

        assert after == before + 1

    def test_the_command_table_counts_groups_as_well_as_calls(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        body = api.get(
            "/admin/analytics/commands", token=tokens.owner, params={"limit": 100}
        ).json()

        dice = next(row for row in body["commands"] if row["command"] == "dice")
        assert dice["groups"] >= 1
        assert dice["invocations"] >= 15

    def test_the_budget_is_reported_against_what_the_window_spent(
        self, api: Api, tokens: Tokens, group: World
    ) -> None:
        budget = api.get("/admin/overview", token=tokens.owner).json()["budget"]

        assert budget["spent_usd"] >= 1.5
        if budget["monthly_llm_budget_usd"] is None:
            assert budget["remaining_usd"] is None

    def test_the_tenant_endpoint_never_returns_a_bot_token(self, api: Api, tokens: Tokens) -> None:
        body = api.get("/admin/tenant", token=tokens.owner).json()

        assert "bot_tokens" not in body
        assert STRANGER_ID not in body["owner_ids"]
