"""the three v1 personas that were never configured

Migration `0003` seeded two of v1's five personas as tenants with the comment
"The five v1 personas become the first tenants" — but only listed `cookiebot`
and `bombot`. `.specs/features/core_botskins/spec.md` records the other three
as a gap: "`pawstralbot`, `tarinbot`, `connectbot` are not configured at all".

They are v1's own, from the five-way `match` in
`../COOKIEBOT-Telegram-Group-Bot/Bot/universal_funcs.py:39-52`
(`cookiebotTOKEN`, `bombotTOKEN`, `pawstralbotTOKEN`, `tarinbotTOKEN`,
`connectbotTOKEN`), so the ids here are v1's env-var names minus the `TOKEN`
suffix and nothing is invented.

**On `pawstralbot` vs "Pawsy".** `../Cookiebot-QA/features/core_botskins.feature`
calls the Pawstral skin "Pawsy" while v1's code calls the token
`pawstralbot` — a QA/v1 naming mismatch of exactly the kind
`docs/site/content/docs/feature-map.mdx` tracks elsewhere. Both are kept and
neither is chosen over the other: the *id* is v1's (`pawstralbot`, which is
what an operator sets a token for) and the *display name* is QA's ("Pawsy",
which is what a user sees). That is what `display_name` is for.

A tenant row is registry configuration, not a credential: a persona with no
entry in `CB_BOT_TOKENS` simply never receives an update. So seeding all five
costs nothing and stops the registry from answering `FALLBACK` — i.e. "you are
Cookiebot" — for a skin that is not.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_SKINS = (
    ("pawstralbot", "Pawsy"),
    ("tarinbot", "Tarinbot"),
    ("connectbot", "Connectbot"),
)


def upgrade() -> None:
    values = ", ".join(f"('{tenant_id}', '{name}')" for tenant_id, name in _SKINS)
    op.execute(
        f"INSERT INTO tenants (tenant_id, display_name) VALUES {values} "
        "ON CONFLICT (tenant_id) DO NOTHING"
    )


def downgrade() -> None:
    ids = ", ".join(f"'{tenant_id}'" for tenant_id, _ in _SKINS)
    # `bots` and `groups` both carry a `tenant_id` FK. Nothing points at these
    # rows in a fresh install, but a deployment that has already routed a group
    # to one of them must not have that row deleted out from under it.
    op.execute(
        f"DELETE FROM tenants WHERE tenant_id IN ({ids}) "
        "AND tenant_id NOT IN (SELECT tenant_id FROM bots) "
        "AND tenant_id NOT IN (SELECT tenant_id FROM groups)"
    )
