"""`cb_core.skins` — the behavioural half of core_botskins.

`cb_gateway.bots.BotRegistry` already replaced v1's one-process-per-persona
model; what this module adds is the part `.specs/features/core_botskins/spec.md`
called out as missing — "the 'skin' is currently only a token and a display
name, not an experience". These assert the two places v1 keys behaviour on
`is_alternate_bot` and the asset lookup a brand needs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cb_core import assets, skins


def test_the_flagship_is_the_default_tenant() -> None:
    """v1's `is_alternate_bot == 0` (`universal_funcs.py:41-42`)."""
    assert skins.is_primary(skins.PRIMARY_SKIN)


@pytest.mark.parametrize("skin", ["bombot", "pawstralbot", "tarinbot", "connectbot"])
def test_every_other_v1_persona_is_an_alternate(skin: str) -> None:
    assert not skins.is_primary(skin)


def test_an_unknown_skin_is_treated_as_an_alternate() -> None:
    """The safe direction: an unrecognised brand must not inherit the
    flagship's join announcement."""
    assert not skins.is_primary("brand-nobody-configured")


# ------------------------------------------------- v1's two behavioural forks


def test_only_the_flagship_announces_itself_on_joining() -> None:
    """v1 `COOKIEBOT.py:130`: `if not is_alternate_bot:` around the animation."""
    assert skins.posts_intro_animation(skins.PRIMARY_SKIN)
    assert not skins.posts_intro_animation("bombot")


def test_the_flagship_respects_the_groups_fun_switch_for_the_flair() -> None:
    """v1 `COOKIEBOT.py:143`: `funfunctions or is_alternate_bot`."""
    assert skins.scammer_photo_allowed(skins.PRIMARY_SKIN, fun_enabled=True)
    assert not skins.scammer_photo_allowed(skins.PRIMARY_SKIN, fun_enabled=False)


def test_an_event_skin_posts_the_flair_regardless_of_the_fun_switch() -> None:
    """The half that reads backwards until you notice what an event skin is
    for — see `skins.scammer_photo_allowed`'s docstring."""
    assert skins.scammer_photo_allowed("bombot", fun_enabled=False)
    assert skins.scammer_photo_allowed("bombot", fun_enabled=True)


# ------------------------------------------------------------------- assets


def test_a_skin_without_an_override_gets_the_shared_asset() -> None:
    """The whole point of the fallback: a brand ships only what it rebrands."""
    resolved = skins.asset("bombot", "doomlist", "silence_scammer.jpg")
    assert resolved == assets.path("doomlist", "silence_scammer.jpg")
    assert resolved.is_file()


def test_v1s_flair_asset_shipped_byte_identically() -> None:
    v1 = Path("../COOKIEBOT-Telegram-Group-Bot/Bot/Static/silence_scammer.jpg")
    if not v1.is_file():  # pragma: no cover - the reference checkout is optional
        pytest.skip("no v1 checkout next to this repo")
    assert skins.asset(skins.PRIMARY_SKIN, "doomlist", "silence_scammer.jpg").read_bytes() == (
        v1.read_bytes()
    )


def test_an_override_wins_when_one_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No brand has supplied artwork yet, so the override branch has no real
    file to exercise — point `assets.path` at a temporary tree instead of
    inventing one in the package."""
    override = tmp_path / skins.SKIN_PACK_DIR / "bombot" / "doomlist" / "silence_scammer.jpg"
    override.parent.mkdir(parents=True)
    override.write_bytes(b"bombot's own")

    monkeypatch.setattr(skins.assets, "path", lambda *parts: tmp_path.joinpath(*parts))
    assert skins.asset("bombot", "doomlist", "silence_scammer.jpg") == override
    # ...and a skin without one still falls through to the shared path.
    assert skins.asset("tarinbot", "doomlist", "silence_scammer.jpg") == (
        tmp_path / "doomlist" / "silence_scammer.jpg"
    )


def test_display_name_falls_back_to_the_skin_id_when_nothing_is_loaded() -> None:
    """Synchronous by design — a handler must not open a database connection
    for a label. `None` from the registry means "not loaded", and the id is a
    better answer than the flagship's name."""
    assert skins.display_name("a-skin-nobody-loaded") == "a-skin-nobody-loaded"
